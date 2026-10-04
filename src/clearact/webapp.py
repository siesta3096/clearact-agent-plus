from __future__ import annotations

import asyncio
import base64
import binascii
import ctypes
import difflib
import hashlib
import json
import os
import tempfile
import threading
import webbrowser
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from clearact import cli
from clearact.domain.enums import RiskLevel, RunStatus
from clearact.domain.models import (
    Action,
    ChatMessage,
    RiskAssessment,
    Run,
    RunEvent,
    RunExecutionSettings,
    UserPolicy,
    WorkflowRevision,
    new_id,
)
from clearact.runtime.model_retry import ModelRequestError
from clearact.runtime.prompting import system_prompt
from clearact.settings import load_settings
from clearact.storage.run_store import RunStore
from clearact.storage.snapshots import SnapshotStore
from clearact.tools.computer_use import BrowserSessionManager
from clearact.tools.mcp import MCPManager

_ASSET_DIR = Path(__file__).with_name("web")

app = FastAPI(title="ClearAct Console")
_active_tasks: dict[str, asyncio.Task] = {}
_active_runs: dict[str, Run] = {}
_pending_approvals: dict[str, dict[str, Any]] = {}
_computer_use_sessions = BrowserSessionManager()


class WebApprovalGate:
    """Pause a web run until its local console records the user's decision."""

    def __init__(self, run_id: str) -> None:
        self._run_id = run_id

    async def request(self, action: Action, assessment: RiskAssessment) -> bool:
        future = asyncio.get_running_loop().create_future()
        pending = {"action": action, "assessment": assessment, "future": future}
        _pending_approvals[self._run_id] = pending
        try:
            return bool(await future)
        finally:
            if _pending_approvals.get(self._run_id) is pending:
                _pending_approvals.pop(self._run_id, None)


class StartRunRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=20_000)
    run_id: str | None = None
    rewind_action_id: str | None = None
    rewind_step_id: str | None = None
    rewind_base_updated_at: str | None = Field(default=None, max_length=64)
    rewind_preview_token: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    interface_language: str | None = Field(default=None, pattern="^(zh|en)$")
    profile: str | None = None
    workdir: str | None = None
    autonomy: RiskLevel | None = None
    max_iterations: int | None = Field(default=None, ge=1, le=10_000)
    max_tool_calls: int | None = Field(default=None, ge=1, le=100_000)
    attachments: list[AttachmentReference] = Field(default_factory=list, max_length=8)
    browser_session_id: str | None = Field(default=None, max_length=128)


class BrowserSessionRequest(BaseModel):
    run_id: str | None = Field(default=None, max_length=80)


class BrowserControlRequest(BaseModel):
    action: str = Field(pattern="^(navigate|back|reload|click|type|press|scroll|resize)$")
    url: str | None = Field(default=None, max_length=4000)
    text: str | None = Field(default=None, max_length=1000)
    key: str | None = Field(default=None, max_length=32)
    x: float | None = None
    y: float | None = None
    dx: int = Field(default=0, ge=-1000, le=1000)
    dy: int = Field(default=600, ge=-1000, le=1000)
    width: int = Field(default=1280, ge=800, le=1920)
    height: int = Field(default=820, ge=700, le=1800)


class AttachmentReference(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    path: str = Field(min_length=1, max_length=1000)
    media_type: str = Field(default="application/octet-stream", max_length=100)


class UploadItem(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    media_type: str = Field(default="application/octet-stream", max_length=100)
    data_base64: str = Field(min_length=1)


class UploadRequest(BaseModel):
    workdir: str | None = None
    files: list[UploadItem] = Field(min_length=1, max_length=8)


class WorkspaceRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4000)


class RenameRunRequest(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    workdir: str | None = None
    autonomy: RiskLevel = RiskLevel.RED
    max_iterations: int | None = Field(default=None, ge=1, le=10_000)
    max_tool_calls: int | None = Field(default=None, ge=1, le=100_000)


class SettingsRequest(BaseModel):
    default_autonomy: RiskLevel
    interface_language: str = Field(default="zh", pattern="^(zh|en)$")
    max_iterations: int = Field(ge=1, le=10_000)
    max_tool_calls: int = Field(ge=1, le=100_000)
    default_profile: str
    profiles: dict[str, dict[str, Any]]
    capability_rules: dict[str, str] = Field(default_factory=dict)


class MCPImportRequest(BaseModel):
    config: dict[str, Any]


class MCPServerRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    transport: str = Field(pattern="^(stdio|streamable_http)$")
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    enabled: bool = True
    secret_kind: str | None = Field(default=None, pattern="^(env|header)$")
    secret_name: str | None = Field(default=None, max_length=200)
    secret_value: str | None = Field(default=None, max_length=8000)


class ApprovalDecisionRequest(BaseModel):
    action_id: str
    approved: bool


def _effective_autonomy(
    request: StartRunRequest, default: RiskLevel, run: Run | None = None
) -> RiskLevel:
    if request.autonomy is not None:
        return request.autonomy
    if run is not None:
        return run.policy.autonomy_threshold
    return default


def _current_run(run_id: str, saved: Run) -> Run:
    task = _active_tasks.get(run_id)
    if task is not None and not task.done():
        return _active_runs.get(run_id, saved)
    return saved


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    """Write configuration atomically so an interrupted save cannot corrupt it."""
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary).replace(path)
    except Exception:
        with suppress(FileNotFoundError):
            Path(temporary).unlink()
        raise


_MAX_ATTACHMENT_BYTES = 12 * 1024 * 1024
_MAX_ATTACHMENTS_TOTAL_BYTES = 32 * 1024 * 1024


def _safe_attachment_name(value: str) -> str:
    name = Path(value.replace("\\", "/")).name.strip().strip(".")
    name = "".join("_" if char in '<>:"/\\|?*' or ord(char) < 32 else char for char in name)
    return name[:180] or "attachment"


def _image_media_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _attachment_workdir(raw_workdir: str | None, default: Path, project_root: Path) -> Path:
    workdir = Path(raw_workdir or default).expanduser()
    if not workdir.is_absolute():
        workdir = project_root / workdir
    workdir = workdir.resolve()
    if not workdir.is_dir():
        raise HTTPException(status_code=422, detail="The selected working directory must already exist.")
    return workdir


def _validated_attachments(references: list[AttachmentReference], workdir: Path) -> list[dict[str, Any]]:
    upload_root = (workdir / ".clearact" / "attachments").resolve()
    validated: list[dict[str, Any]] = []
    for reference in references:
        target = (workdir / reference.path).resolve()
        try:
            target.relative_to(upload_root)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Attachment is outside the workspace upload area.") from exc
        if not target.is_file():
            raise HTTPException(status_code=422, detail=f"Attachment no longer exists: {reference.name}")
        size = target.stat().st_size
        if size > _MAX_ATTACHMENT_BYTES:
            raise HTTPException(status_code=413, detail=f"Attachment is too large: {reference.name}")
        detected_image_type = _image_media_type(target.read_bytes()[:16])
        validated.append(
            {
                "name": _safe_attachment_name(reference.name),
                "path": target.relative_to(workdir).as_posix(),
                "storage_path": str(target),
                "media_type": detected_image_type or reference.media_type,
                "kind": "image" if detected_image_type else "file",
                "size": size,
            }
        )
    if sum(item["size"] for item in validated) > _MAX_ATTACHMENTS_TOTAL_BYTES:
        raise HTTPException(status_code=413, detail="Attachments exceed the 32 MB total limit.")
    return validated


def _public_mcp_servers(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return editable connection metadata without leaking headers or env values."""
    servers = raw.get("mcp", {}).get("servers", raw.get("mcpServers", {})) or {}
    public: dict[str, dict[str, Any]] = {}
    for name, value in servers.items():
        if not isinstance(value, dict):
            continue
        public[str(name)] = {
            key: value[key] for key in ("transport", "command", "args", "url", "enabled") if key in value
        }
        if "env" in value:
            public[str(name)]["envKeys"] = sorted(value["env"])
        if "headers" in value:
            public[str(name)]["headerKeys"] = sorted(value["headers"])
    return public


def _mcp_server_config(request: MCPServerRequest, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalize the friendly MCP form into the canonical local config."""
    if any(char in request.name for char in "\\/"):
        raise HTTPException(status_code=422, detail="MCP server name cannot contain slashes.")
    if request.transport == "stdio":
        if not request.command or not request.command.strip():
            raise HTTPException(status_code=422, detail="Local MCP service needs a command.")
        if len(request.args) > 100 or not all(len(item) <= 2000 for item in request.args):
            raise HTTPException(status_code=422, detail="MCP arguments exceed the safety limits.")
        value: dict[str, Any] = {
            "transport": "stdio",
            "command": request.command.strip(),
            "args": request.args,
            "enabled": request.enabled,
        }
    else:
        if not request.url or not request.url.startswith(("http://", "https://")):
            raise HTTPException(status_code=422, detail="Remote MCP service needs an HTTP(S) URL.")
        value = {"transport": "streamable_http", "url": request.url, "enabled": request.enabled}
    previous = previous or {}
    preserved_field = "env" if request.transport == "stdio" else "headers"
    if isinstance(previous.get(preserved_field), dict):
        value[preserved_field] = dict(previous[preserved_field])
    if request.secret_value and request.secret_name:
        field = "env" if request.secret_kind == "env" else "headers"
        value.setdefault(field, {})[request.secret_name] = request.secret_value
    return value


def _rollback_discarded_effects(
    run: Run, cutoff: int, snapshot_store: SnapshotStore, *, dry_run: bool = False
) -> tuple[list[str], list[str]]:
    """Undo local writes only when the file still matches the agent's last write."""
    actions = {
        action.id: action
        for message in run.messages[cutoff:]
        for action in message.tool_calls
    }
    restored: list[str] = []
    warnings: list[str] = []
    virtual_files: dict[Path, str | None] = {}
    for message in reversed(run.messages[cutoff:]):
        if message.role != "tool" or message.metadata.get("status") != "succeeded":
            continue
        action = actions.get(message.tool_call_id or "")
        snapshot_id = message.metadata.get("snapshot_id")
        if isinstance(snapshot_id, str) and snapshot_id:
            try:
                target, existed, before = snapshot_store.load_before_write(snapshot_id)
                requested_path = action.arguments.get("path") if action and action.tool_name == "write_file" else None
                if not isinstance(requested_path, str) or snapshot_store.workspace_root is None:
                    warnings.append(f"Could not verify the write target of {snapshot_id}; left it unchanged")
                    continue
                candidate = Path(requested_path).expanduser()
                expected_path = (
                    candidate.resolve() if candidate.is_absolute()
                    else (snapshot_store.workspace_root / candidate).resolve()
                )
                recorded_path = message.metadata.get("path")
                recorded_target = None
                if isinstance(recorded_path, str):
                    recorded_candidate = Path(recorded_path).expanduser()
                    recorded_target = (
                        recorded_candidate.resolve() if recorded_candidate.is_absolute()
                        else (snapshot_store.workspace_root / recorded_candidate).resolve()
                    )
                if expected_path != target or (
                    recorded_target is not None and recorded_target != target
                ):
                    warnings.append(f"Snapshot path does not match the write action; left unchanged: {target}")
                    continue
                expected = action.arguments.get("content") if action and action.tool_name == "write_file" else None
                if not isinstance(expected, str):
                    warnings.append(f"Could not verify the current content of {target}; left it unchanged")
                    continue
                current = virtual_files.get(target) if target in virtual_files else (
                    target.read_text(encoding="utf-8") if target.is_file() else None
                )
                if current != expected:
                    warnings.append(f"File changed after this action; left it unchanged: {target}")
                    continue
                if not dry_run:
                    snapshot_store.restore(snapshot_id)
                virtual_files[target] = before if existed else None
                restored.append(snapshot_id)
            except (FileNotFoundError, OSError, UnicodeError, ValueError, KeyError) as exc:
                warnings.append(f"Could not restore {snapshot_id}: {exc}")
        elif action and (
            action.tool_name.startswith("mcp__")
            or action.tool_name == "write_file"
            or (action.tool_name == "computer_use" and action.arguments.get("action") in {"click", "type", "press"})
        ):
            warnings.append(f"External effect may remain: {action.tool_name} ({action.id})")
    return restored, warnings


def _resolve_rewind_boundary(
    run: Run, step_id: str | None, action_id: str | None
) -> tuple[int, int, str]:
    """Find the same message and phase boundary for preview and execution."""
    if step_id:
        step_index = next((index for index, step in enumerate(run.workflow_steps) if step.id == step_id), None)
        if step_index is None:
            raise HTTPException(status_code=422, detail="The selected workflow step is no longer available.")
        step = run.workflow_steps[step_index]
        cutoff = step.start_message_index
        if step.id == "understand":
            cutoff = next(
                (index for index, message in enumerate(run.messages) if message.role == "assistant"),
                len(run.messages),
            )
        if cutoff is None or not 0 <= cutoff <= len(run.messages):
            raise HTTPException(status_code=422, detail="The selected workflow step has no rewind boundary.")
        return cutoff, step_index, step.id
    cutoff = next(
        (
            index
            for index, message in enumerate(run.messages)
            if any(action.id == action_id for action in message.tool_calls)
        ),
        None,
    )
    if cutoff is None:
        raise HTTPException(status_code=422, detail="The selected action is no longer available.")
    step_index = next(
        (index for index, step in enumerate(run.workflow_steps) if action_id in step.action_ids),
        len(run.workflow_steps),
    )
    target_id = run.workflow_steps[step_index].id if step_index < len(run.workflow_steps) else str(action_id)
    return cutoff, step_index, target_id


def _restored_file_contents(snapshot_store: SnapshotStore, snapshot_ids: list[str]) -> dict[Path, str | None]:
    """Predict the final file contents after reversing discarded writes."""
    contents: dict[Path, str | None] = {}
    for snapshot_id in snapshot_ids:
        target, existed, before = snapshot_store.load_before_write(snapshot_id)
        contents[target] = before if existed else None
    return contents


def _recorded_file_version(message: ChatMessage) -> str | None:
    read_range = message.metadata.get("read_file_range")
    version = read_range.get("version") if isinstance(read_range, dict) else None
    if isinstance(version, str):
        return version
    signature = message.metadata.get("read_only_signature")
    if not isinstance(signature, str):
        return None
    try:
        prefix, _ = signature.rsplit(":{", 1)
        _, recorded_mtime, recorded_size = prefix.rsplit(":", 2)
        return f"{int(recorded_mtime)}:{int(recorded_size)}"
    except ValueError:
        return None


def _rewind_preview_token(snapshot_ids: list[str], warnings: list[str]) -> str:
    """Bind a confirmation to the file effects shown in the rewind preview."""
    state = json.dumps({"snapshots": snapshot_ids, "warnings": warnings}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def _retained_file_read_issues(
    run: Run, cutoff: int, restored_contents: dict[Path, str | None]
) -> dict[str, str]:
    """Identify changed or unverifiable local evidence in the retained branch."""
    issues: dict[str, str] = {}
    retained = run.messages[:cutoff]
    for index, message in enumerate(retained):
        if message.role != "tool" or message.name not in {"read_file", "read_pdf", "list_files"}:
            continue
        if message.metadata.get("status") != "succeeded":
            continue
        raw_path = message.metadata.get("path")
        if not isinstance(raw_path, str):
            continue  # Without a path there is no safe target to re-read.
        try:
            target = Path(raw_path).resolve()
        except (ValueError, OSError):
            continue
        recorded_version = _recorded_file_version(message)
        if recorded_version is None:
            issues[str(target)] = "unverified"
            continue
        later_retained_write = any(
            later.role == "tool" and later.name == "write_file"
            and later.metadata.get("status") == "succeeded"
            and later.metadata.get("path") == str(target)
            for later in retained[index + 1:]
        )
        if target in restored_contents and not later_retained_write:
            restored = restored_contents[target]
            read_range = message.metadata.get("read_file_range")
            offset = read_range.get("offset", 0) if isinstance(read_range, dict) else 0
            can_compare = (
                message.name == "read_file" and isinstance(restored, str)
                and type(offset) is int and offset >= 0
                and message.metadata.get("truncated") is False
                and isinstance(message.content, str)
            )
            if can_compare and message.content == restored[offset:]:
                continue
            issues[str(target)] = "changed" if can_compare or restored is None else "unverified"
            continue
        try:
            stat = target.stat()
            current_version = f"{stat.st_mtime_ns}:{stat.st_size}"
        except OSError:
            current_version = None
        if current_version != recorded_version:
            issues[str(target)] = "changed"
    return issues


def _retained_read_warnings(issues: dict[str, str]) -> list[str]:
    return [
        (f"此前读取的文件已变化，继续使用前需要重新读取：{path}"
         if issue == "changed" else
         f"旧记录缺少可核验的文件版本，继续使用前需要重新读取：{path}")
        for path, issue in issues.items()
    ]


@app.get("/")
async def index() -> Response:
    response = FileResponse(_ASSET_DIR / "index.html")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


@app.get("/assets/{filename}")
async def assets(filename: str) -> FileResponse:
    target = (_ASSET_DIR / filename).resolve()
    if target.parent != _ASSET_DIR.resolve() or not target.is_file():
        raise HTTPException(status_code=404, detail="Asset not found")
    response = FileResponse(target)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


@app.get("/api/config")
async def config() -> dict:
    root = cli._project_root()
    settings = load_settings(root)
    with (root / "clearact.json").open(encoding="utf-8") as handle:
        web = json.load(handle).get("web", {})
    return {
        "default_workdir": str(settings.workspace_root),
        "profiles": sorted(settings.models["profiles"]),
        "default_profile": settings.models["default_profile"],
        "default_autonomy": settings.default_autonomy.value,
        "capability_rules": getattr(settings, "default_capability_rules", {}),
        "interface_language": web.get("interfaceLanguage", "zh"),
        "defaults": settings.agent.model_dump(),
        "allow_localhost": settings.network.allow_localhost,
        "mcp_servers": _public_mcp_servers(json.loads((root / "clearact.json").read_text(encoding="utf-8"))),
        "security": {"local_only": True, "api_keys_exposed": False},
    }


@app.post("/api/computer-use/sessions", status_code=201)
async def create_computer_use_session(request: BrowserSessionRequest) -> dict[str, Any]:
    root = cli._project_root()
    settings = load_settings(root)
    owner_run_id = request.run_id
    if owner_run_id:
        try:
            RunStore(settings.data_root).load_run(owner_run_id)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Topic not found.") from exc
    try:
        session = await _computer_use_sessions.create(settings.network.allow_localhost, owner_run_id)
        state = await session.state()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"id": session.id, **state, "width": 1280, "height": 820}


@app.get("/api/computer-use/sessions/{session_id}")
async def computer_use_session_state(session_id: str) -> dict[str, str]:
    session = _computer_use_sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Browser session expired.")
    try:
        return await session.state()
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"Browser page is unavailable: {exc}") from exc


@app.get("/api/computer-use/sessions/{session_id}/screenshot")
async def computer_use_screenshot(session_id: str) -> Response:
    session = _computer_use_sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Browser session expired.")
    try:
        image = await session.screenshot()
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"Browser screenshot unavailable: {exc}") from exc
    return Response(
        content=image,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "X-Content-Type-Options": "nosniff"},
    )


@app.post("/api/computer-use/sessions/{session_id}/control")
async def control_computer_use_session(session_id: str, request: BrowserControlRequest) -> dict[str, str]:
    session = _computer_use_sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Browser session expired.")
    try:
        if request.action == "navigate":
            if not request.url:
                raise ValueError("Enter a web address first.")
            state = await session.navigate(request.url)
        elif request.action in {"back", "reload"}:
            state = await session.control(request.action)
        elif request.action == "click":
            if request.x is None or request.y is None:
                raise ValueError("Click coordinates are required.")
            state = await session.control("click_point", x=request.x, y=request.y)
        elif request.action == "type":
            state = await session.control("type_point", text=request.text or "")
        elif request.action == "press":
            key = request.key or ""
            state = await session.control("press", key=key)
        elif request.action == "resize":
            state = await session.control("resize", width=request.width, height=request.height)
        else:
            state = await session.control("scroll", dx=request.dx, dy=request.dy)
        return state
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.delete("/api/computer-use/sessions/{session_id}")
async def close_computer_use_session(session_id: str) -> dict[str, bool]:
    if not await _computer_use_sessions.close(session_id):
        raise HTTPException(status_code=404, detail="Browser session already closed.")
    return {"closed": True}


@app.on_event("shutdown")
async def close_computer_use_sessions() -> None:
    await _computer_use_sessions.close_all()


@app.get("/api/settings")
async def get_settings() -> dict:
    """Return editable settings without ever exposing provider API keys to the browser."""
    root = cli._project_root()
    settings = load_settings(root)
    with (root / "clearact.json").open(encoding="utf-8") as handle:
        raw = json.load(handle)
    profiles = {
        name: {
            **{key: value for key, value in profile.items() if key not in {"apiKey", "apiKeyEnv"}},
            "hasApiKey": bool(profile.get("apiKey", "")),
        }
        for name, profile in raw["profiles"].items()
    }
    return {
        "default_autonomy": settings.default_autonomy.value,
        "capability_rules": getattr(settings, "default_capability_rules", {}),
        "max_iterations": settings.agent.max_iterations,
        "max_tool_calls": settings.agent.max_tool_calls_per_run,
        "default_profile": raw["defaultProfile"],
        "interface_language": raw.get("web", {}).get("interfaceLanguage", "zh"),
        "profiles": profiles,
        "mcp_servers": _public_mcp_servers(raw),
    }


@app.put("/api/settings")
async def update_settings(request: SettingsRequest) -> dict:
    root = cli._project_root()
    config_path = root / "clearact.json"
    with config_path.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    if request.default_profile not in raw["profiles"]:
        raise HTTPException(status_code=422, detail="Default profile must be an existing profile.")
    for name, update in request.profiles.items():
        if name not in raw["profiles"]:
            raise HTTPException(status_code=422, detail=f"Unknown profile: {name}")
        for key in ("provider", "model", "baseUrl", "contextWindow"):
            if key in update and update[key] is not None:
                raw["profiles"][name][key] = update[key]
        # API Key：非空则更新，空或缺失则保留原值
        if update.get("apiKey"):
            raw["profiles"][name]["apiKey"] = update["apiKey"]
    raw["defaultProfile"] = request.default_profile
    raw["policy"]["defaultAutonomy"] = request.default_autonomy.value
    allowed_capabilities = {
        "local_read",
        "web_read",
        "browser_interaction",
        "workspace_create",
        "workspace_modify",
        "mcp_read",
        "mcp_write",
        "outside_write",
        "destructive",
        "other",
    }
    if set(request.capability_rules) - allowed_capabilities or any(
        rule not in {"allow", "ask", "deny"} for rule in request.capability_rules.values()
    ):
        raise HTTPException(status_code=422, detail="Invalid capability permission rule.")
    raw["policy"]["defaultCapabilities"] = request.capability_rules
    raw.setdefault("web", {})["interfaceLanguage"] = request.interface_language
    raw["agent"]["maxIterations"] = request.max_iterations
    raw["agent"]["maxToolCallsPerRun"] = request.max_tool_calls
    _atomic_write_json(config_path, raw)
    return {"saved": True}


@app.get("/api/mcp/catalog")
async def search_mcp_catalog(q: str = "") -> dict[str, Any]:
    """Find concrete remote connections in the public, unvetted MCP Registry."""
    query = q.strip()
    if len(query) < 2 or len(query) > 80:
        raise HTTPException(status_code=422, detail="Search must contain 2–80 characters.")
    try:
        async with asyncio.timeout(7):
            async with httpx.AsyncClient(timeout=6.0, follow_redirects=False) as client:
                response = await client.get(
                    "https://registry.modelcontextprotocol.io/v0.1/servers",
                    params={"search": query, "version": "latest", "limit": 30},
                )
                response.raise_for_status()
                payload = response.json()
    except (httpx.HTTPError, ValueError, TimeoutError) as exc:
        raise HTTPException(
            status_code=502, detail="MCP Registry is unavailable. You can still add a service URL manually."
        ) from exc
    items = []
    for entry in payload.get("servers", []):
        if not isinstance(entry, dict):
            continue
        server = entry.get("server", entry)
        if not isinstance(server, dict):
            continue
        for remote in server.get("remotes") or []:
            if not isinstance(remote, dict):
                continue
            url = remote.get("url")
            if remote.get("type") != "streamable-http" or not isinstance(url, str):
                continue
            if not url.startswith("https://") or "{" in url or "}" in url:
                continue
            items.append({
                "name": str(server.get("name") or "")[:150],
                "title": str(server.get("title") or server.get("name") or "")[:120],
                "description": str(server.get("description") or "")[:250],
                "url": url[:1000],
                "needs_auth": bool(remote.get("headers") or remote.get("variables")),
            })
            break
        if len(items) >= 12:
            break
    return {"source": "official_mcp_registry", "servers": items}


@app.post("/api/mcp/import")
async def import_mcp_servers(request: MCPImportRequest) -> dict:
    """Import standard mcpServers JSON while retaining secrets locally, never in responses."""
    root = cli._project_root()
    config_path = root / "clearact.json"
    incoming = request.config.get("mcpServers", request.config.get("servers"))
    if not isinstance(incoming, dict):
        raise HTTPException(status_code=422, detail="Expected an object containing mcpServers.")
    normalized: dict[str, dict[str, Any]] = {}
    for name, server in incoming.items():
        if not isinstance(name, str) or not isinstance(server, dict):
            raise HTTPException(
                status_code=422, detail="Each MCP server must have a string name and object configuration."
            )
        transport = server.get("transport") or ("streamable_http" if "url" in server else "stdio")
        if transport not in {"stdio", "streamable_http"}:
            raise HTTPException(status_code=422, detail=f"Unsupported transport for {name}.")
        if transport == "stdio" and not isinstance(server.get("command"), str):
            raise HTTPException(status_code=422, detail=f"MCP stdio server {name} needs command.")
        if transport == "streamable_http" and not isinstance(server.get("url"), str):
            raise HTTPException(status_code=422, detail=f"MCP HTTP server {name} needs url.")
        if len(name) > 80 or len(normalized) >= 50:
            raise HTTPException(status_code=422, detail="MCP server names/count exceed the safety limits.")
        args = server.get("args", [])
        if not isinstance(args, list) or len(args) > 100 or not all(
            isinstance(item, str) and len(item) <= 2000 for item in args
        ):
            raise HTTPException(status_code=422, detail=f"Invalid or oversized args for MCP server {name}.")
        for field in ("env", "headers"):
            values = server.get(field, {})
            if values and (not isinstance(values, dict) or len(values) > 100):
                raise HTTPException(status_code=422, detail=f"Invalid or oversized {field} for MCP server {name}.")
            if values and not all(
                isinstance(k, str) and isinstance(v, str) and len(k) <= 200 and len(v) <= 8000
                for k, v in values.items()
            ):
                raise HTTPException(status_code=422, detail=f"Invalid {field} values for MCP server {name}.")
        normalized[name] = dict(server, transport=transport)
    with config_path.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    raw.setdefault("mcp", {})["servers"] = normalized
    # Remove the alias after import so there is one canonical persisted location.
    raw.pop("mcpServers", None)
    _atomic_write_json(config_path, raw)
    return {"imported": sorted(normalized), "servers": _public_mcp_servers(raw)}


@app.post("/api/mcp/servers")
async def save_mcp_server(request: MCPServerRequest) -> dict:
    root = cli._project_root()
    config_path = root / "clearact.json"
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    servers = raw.setdefault("mcp", {}).setdefault("servers", {})
    if request.name not in servers and len(servers) >= 50:
        raise HTTPException(status_code=422, detail="MCP server count exceeds the safety limit.")
    servers[request.name] = _mcp_server_config(request, servers.get(request.name))
    raw.pop("mcpServers", None)
    _atomic_write_json(config_path, raw)
    return {"saved": True, "server": _public_mcp_servers(raw)[request.name]}


@app.put("/api/mcp/servers/{server_name}/enabled")
async def set_mcp_server_enabled(server_name: str, enabled: bool) -> dict:
    root = cli._project_root()
    config_path = root / "clearact.json"
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    servers = raw.get("mcp", {}).get("servers", {})
    if server_name not in servers:
        raise HTTPException(status_code=404, detail="MCP server not found.")
    servers[server_name]["enabled"] = enabled
    _atomic_write_json(config_path, raw)
    return {"saved": True, "enabled": enabled}


@app.delete("/api/mcp/servers/{server_name}")
async def delete_mcp_server(server_name: str) -> dict:
    root = cli._project_root()
    config_path = root / "clearact.json"
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    servers = raw.get("mcp", {}).get("servers", {})
    if server_name not in servers:
        raise HTTPException(status_code=404, detail="MCP server not found.")
    del servers[server_name]
    _atomic_write_json(config_path, raw)
    return {"deleted": True}


@app.post("/api/mcp/servers/{server_name}/test")
async def test_mcp_server(server_name: str) -> dict:
    settings = load_settings(cli._project_root())
    server = settings.mcp_servers.get(server_name)
    if server is None:
        raise HTTPException(status_code=404, detail="MCP server not found.")
    manager = MCPManager(
        {server_name: {**server, "enabled": True}},
        allow_localhost=settings.network.allow_localhost,
    )
    try:
        await manager.connect()
        if manager.errors:
            raise HTTPException(status_code=422, detail=manager.errors[server_name])
        return {"ok": True, "tools": [definition.name for definition in manager.definitions()]}
    finally:
        await manager.close()


@app.get("/api/runs")
async def runs(limit: int = 30) -> list[dict]:
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 100.")
    store = RunStore(load_settings(cli._project_root()).data_root)
    return [
        {
            "id": run.id,
            "goal": run.goal,
            "title": run.title or run.goal[:28],
            "status": run.status.value,
            "needs_review": run.status == RunStatus.COMPLETED and (
                bool(run.stage_notes.get("completion_review"))
                or any(step.status == "needs_review" for step in run.workflow_steps)
            ),
            "updated_at": run.updated_at.isoformat(),
        }
        for run in store.list_runs(limit)
    ]


@app.post("/api/uploads", status_code=201)
async def upload_attachments(request: UploadRequest) -> dict:
    root = cli._project_root()
    settings = load_settings(root)
    workdir = _attachment_workdir(request.workdir, settings.workspace_root, root)
    decoded: list[tuple[UploadItem, bytes]] = []
    total = 0
    for item in request.files:
        encoded = item.data_base64.split(",", 1)[-1]
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=422, detail=f"Invalid attachment data: {item.name}") from exc
        if len(data) > _MAX_ATTACHMENT_BYTES:
            raise HTTPException(status_code=413, detail=f"Attachment is too large: {item.name}")
        total += len(data)
        if total > _MAX_ATTACHMENTS_TOTAL_BYTES:
            raise HTTPException(status_code=413, detail="Attachments exceed the 32 MB total limit.")
        decoded.append((item, data))

    destination = workdir / ".clearact" / "attachments" / new_id("upload")
    destination.mkdir(parents=True, exist_ok=False)
    attachments = []
    for index, (item, data) in enumerate(decoded, start=1):
        safe_name = _safe_attachment_name(item.name)
        target = destination / f"{index:02d}-{safe_name}"
        target.write_bytes(data)
        media_type = _image_media_type(data[:16]) or item.media_type
        attachments.append(
            {
                "name": safe_name,
                "path": target.relative_to(workdir).as_posix(),
                "media_type": media_type,
                "kind": "image" if media_type.startswith("image/") and _image_media_type(data[:16]) else "file",
                "size": len(data),
            }
        )
    return {"attachments": attachments}


def _run_detail_etag(store: RunStore, run_id: str) -> str:
    if not run_id.startswith("run_") or any(char in run_id for char in "\\/"):
        raise HTTPException(status_code=404, detail="Run not found")

    def version(path: Path) -> tuple[int, int, int, int] | None:
        try:
            stat = path.stat()
        except FileNotFoundError:
            return None
        return stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino

    run_version = version(store._root / f"{run_id}.json")
    if run_version is None:
        raise HTTPException(status_code=404, detail="Run not found")
    events_version = version(store._root / f"{run_id}.events.jsonl")
    pending = _pending_approvals.get(run_id)
    approval_id = pending["action"].id if pending and not pending["future"].done() else None
    fingerprint = json.dumps((run_version, events_version, approval_id), separators=(",", ":"))
    return f'W/"{hashlib.sha256(fingerprint.encode()).hexdigest()[:32]}"'


@app.get("/api/runs/{run_id}")
async def run_detail(run_id: str, request: Request, response: Response) -> Any:
    store = RunStore(load_settings(cli._project_root()).data_root)
    etag = _run_detail_etag(store, run_id)
    client_tags = {part.strip() for part in request.headers.get("if-none-match", "").split(",")}
    if "*" in client_tags or etag in client_tags or etag[2:] in client_tags:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-store"})
    try:
        run = store.load_run(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc
    pending = _pending_approvals.get(run_id)
    approval = None
    if pending and not pending["future"].done():
        action, assessment = pending["action"], pending["assessment"]
        approval = {
            "action_id": action.id,
            "tool_name": action.tool_name,
            "arguments": action.arguments,
            "risk": assessment.level.value,
            "reasons": assessment.reasons,
        }
    result = {
        "run": run.model_dump(mode="json"),
        "events": [event.model_dump(mode="json") for event in store.load_events(run_id)],
        "approval": approval,
    }
    # If a separate process changed either file while it was being read, do not
    # cache this mixed snapshot; the next poll will request a fresh one.
    if _run_detail_etag(store, run_id) == etag:
        response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-store"
    return result


@app.get("/api/runs/{run_id}/rewind-preview")
async def rewind_preview(run_id: str, step_id: str) -> dict[str, Any]:
    settings = load_settings(cli._project_root())
    store = RunStore(settings.data_root)
    try:
        run = store.load_run(run_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Topic not found.") from exc
    task = _active_tasks.get(run_id)
    if task and not task.done():
        raise HTTPException(status_code=409, detail="Wait for this task to stop before rewinding.")
    cutoff, step_cutoff, target_id = _resolve_rewind_boundary(run, step_id, None)
    workspace = Path(run.execution.workdir or settings.workspace_root).resolve()
    snapshots = SnapshotStore(settings.data_root, workspace)
    restorable, warnings = _rollback_discarded_effects(run, cutoff, snapshots, dry_run=True)
    paths: list[str] = []
    restored_contents = _restored_file_contents(snapshots, restorable)
    for target in restored_contents:
        path = str(target.relative_to(workspace))
        if path not in paths:
            paths.append(path)
    source_issues = _retained_file_read_issues(run, cutoff, restored_contents)
    warnings.extend(_retained_read_warnings(source_issues))
    return {
        "from_step_id": target_id,
        "from_step_title": run.workflow_steps[step_cutoff].title,
        "base_updated_at": run.updated_at.isoformat(),
        "preview_token": _rewind_preview_token(restorable, warnings),
        "reused_steps": [step.model_dump(include={"id", "title"}) for step in run.workflow_steps[:step_cutoff]],
        "discarded_steps": [step.model_dump(include={"id", "title"}) for step in run.workflow_steps[step_cutoff:]],
        "discarded_message_count": len(run.messages) - cutoff,
        "restore_paths": paths,
        "stale_source_paths": [path for path, issue in source_issues.items() if issue == "changed"],
        "unverified_source_paths": [path for path, issue in source_issues.items() if issue == "unverified"],
        "warnings": warnings,
    }


@app.get("/api/runs/{run_id}/revisions/{revision_id}")
async def revision_archive(run_id: str, revision_id: str) -> dict[str, Any]:
    store = RunStore(load_settings(cli._project_root()).data_root)
    try:
        run = store.load_run(run_id)
        revision = next(item for item in run.workflow_revisions if item.id == revision_id)
        if not revision.archive_available:
            raise FileNotFoundError(revision_id)
        return store.load_revision_archive(run_id, revision_id)
    except (FileNotFoundError, StopIteration, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Revision archive not found.") from exc


@app.get("/api/runs/{run_id}/actions/{action_id}/preview")
async def action_file_preview(run_id: str, action_id: str) -> dict[str, Any]:
    """Show only a file action recorded by this run, never an arbitrary path."""
    settings = load_settings(cli._project_root())
    store = RunStore(settings.data_root)
    try:
        run = store.load_run(run_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc
    action = next(
        (call for message in run.messages for call in message.tool_calls if call.id == action_id), None
    )
    result = next(
        (message for message in run.messages if message.role == "tool" and message.tool_call_id == action_id), None
    )
    if not action or action.tool_name not in {"read_file", "read_pdf", "write_file"}:
        raise HTTPException(status_code=404, detail="File action not found")
    if not result or result.metadata.get("status") != "succeeded":
        raise HTTPException(status_code=409, detail="File action did not complete successfully")
    recorded_path = result.metadata.get("path")
    if not isinstance(recorded_path, str) or not recorded_path:
        raise HTTPException(status_code=409, detail="File action has no recorded path")
    workspace = Path(run.execution.workdir or settings.workspace_root).resolve()
    target = Path(recorded_path).resolve()
    raw_action_path = action.arguments.get("path")
    if not isinstance(raw_action_path, str) or not raw_action_path:
        raise HTTPException(status_code=409, detail="File action has no path argument")
    candidate = Path(raw_action_path).expanduser()
    action_target = candidate.resolve() if candidate.is_absolute() else (workspace / candidate).resolve()
    if action_target != target:
        raise HTTPException(status_code=403, detail="Recorded file path does not match the action")
    try:
        target.relative_to(workspace)
        outside_workspace = False
    except ValueError:
        outside_workspace = True
    limit = 120_000
    if action.tool_name != "write_file":
        content = result.content or ""
        recorded_version = _recorded_file_version(result)
        try:
            stat = target.stat()
            current_version = f"{stat.st_mtime_ns}:{stat.st_size}"
        except OSError:
            current_version = None
        source_state = (
            "unstable" if result.metadata.get("source_changed_around_read") is True else
            "unverified" if recorded_version is None else
            "current" if recorded_version == current_version else "changed"
        )
        return {
            "kind": "content", "path": str(target), "content": content[:limit],
            "truncated": len(content) > limit, "source": "recorded_action", "source_state": source_state,
            "outside_workspace": outside_workspace,
        }
    snapshot_id = result.metadata.get("snapshot_id")
    if not isinstance(snapshot_id, str):
        raise HTTPException(status_code=409, detail="No before-image was recorded")
    snapshots = SnapshotStore(settings.data_root, workspace_root=None if outside_workspace else workspace)
    try:
        before_path, existed, before = snapshots.load_before_write(snapshot_id)
    except (FileNotFoundError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=409, detail="Before-image is unavailable") from exc
    if before_path != target:
        raise HTTPException(status_code=409, detail="Snapshot path does not match the action")
    after = action.arguments.get("content")
    if not isinstance(after, str):
        raise HTTPException(status_code=409, detail="Written content is unavailable")
    diff = "".join(difflib.unified_diff(
        before.splitlines(keepends=True) if existed else [], after.splitlines(keepends=True),
        fromfile=f"before/{target.name}" if existed else "/dev/null",
        tofile=f"after/{target.name}",
    ))
    return {
        "kind": "diff", "path": str(target), "content": diff[:limit],
        "truncated": len(diff) > limit, "created": not existed, "source": "recorded_action",
        "outside_workspace": outside_workspace,
    }


@app.post("/api/runs/{run_id}/approval")
async def decide_approval(run_id: str, request: ApprovalDecisionRequest) -> dict:
    pending = _pending_approvals.get(run_id)
    if not pending or pending["future"].done():
        raise HTTPException(status_code=409, detail="This topic is not waiting for approval.")
    if pending["action"].id != request.action_id:
        raise HTTPException(status_code=409, detail="The pending action has changed. Refresh and try again.")
    pending["future"].set_result(request.approved)
    return {"accepted": True, "approved": request.approved}


@app.post("/api/runs/{run_id}/stop")
async def stop_run(run_id: str) -> dict:
    task = _active_tasks.get(run_id)
    store = RunStore(load_settings(cli._project_root()).data_root)
    try:
        run = store.load_run(run_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Topic not found.") from exc
    if run.status not in {RunStatus.CREATED, RunStatus.RUNNING, RunStatus.WAITING_APPROVAL, RunStatus.PAUSED}:
        return {"stopped": False, "status": run.status.value}
    if task and not task.done():
        task.cancel()
    if task and not task.done():
        with suppress(asyncio.CancelledError):
            await task
    # Cancellation may persist tool results and other final state. Read those
    # changes back instead of overwriting them with the pre-cancellation copy.
    run = store.load_run(run_id)
    if run.status != RunStatus.CANCELLED:
        run.status = RunStatus.CANCELLED
        store.save_run(run)
    return {"stopped": True, "status": run.status.value}


@app.put("/api/runs/{run_id}")
async def rename_run(run_id: str, request: RenameRunRequest) -> dict:
    store = RunStore(load_settings(cli._project_root()).data_root)
    try:
        saved = store.load_run(run_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Topic not found.") from exc
    run = _current_run(run_id, saved)
    run.title = request.title.strip()
    store.save_run(run)
    return {"saved": True, "title": run.title}


@app.delete("/api/runs/{run_id}")
async def delete_run(run_id: str) -> dict:
    if not run_id.startswith("run_") or any(char in run_id for char in "\\/"):
        raise HTTPException(status_code=404, detail="Topic not found.")
    task = _active_tasks.get(run_id)
    if task and not task.done():
        raise HTTPException(status_code=409, detail="Cannot delete a running topic; stop it first.")
    store = RunStore(load_settings(cli._project_root()).data_root)
    root = store._root
    path, events = root / f"{run_id}.json", root / f"{run_id}.events.jsonl"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Topic not found.")
    path.unlink()
    if events.exists():
        events.unlink()
    store.delete_revision_archives(run_id)
    return {"deleted": True}


@app.post("/api/runs", status_code=202)
async def start_run(request: StartRunRequest) -> dict:
    root = cli._project_root()
    settings = load_settings(root)
    store = RunStore(settings.data_root)
    run = None
    if request.run_id:
        active = _active_tasks.get(request.run_id)
        if active and not active.done():
            raise HTTPException(status_code=409, detail="This topic is already running.")
        try:
            run = store.load_run(request.run_id)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Topic not found.") from exc

    previous = run.execution if run else RunExecutionSettings()
    # Older ledgers predate execution settings but already record the authorized
    # directory in their policy. Migrate it instead of silently using a new root.
    saved_workdir = previous.workdir
    if run and not saved_workdir and run.policy.allowed_scopes:
        saved_workdir = run.policy.allowed_scopes[0]
    workdir = _attachment_workdir(request.workdir or saved_workdir, settings.workspace_root, root)
    attachments = _validated_attachments(request.attachments, workdir)
    profile_name = request.profile or previous.profile or settings.models["default_profile"]
    if profile_name not in settings.models["profiles"]:
        raise HTTPException(status_code=422, detail="Unknown model profile.")
    execution = RunExecutionSettings(
        workdir=str(workdir),
        profile=profile_name,
        max_iterations=request.max_iterations or previous.max_iterations or settings.agent.max_iterations,
        max_tool_calls=request.max_tool_calls or previous.max_tool_calls or settings.agent.max_tool_calls_per_run,
        interface_language=request.interface_language or previous.interface_language or "zh",
    )
    effective_autonomy = _effective_autonomy(request, settings.default_autonomy, run)
    rewound_revision_id: str | None = None

    if run is not None:
        # A normal follow-up resumes the topic. A stage rewind deliberately
        # discards that action and everything after it, then restarts from the
        # retained earlier context plus the user's feedback.
        if request.rewind_step_id or request.rewind_action_id:
            if request.rewind_base_updated_at and request.rewind_base_updated_at != run.updated_at.isoformat():
                raise HTTPException(
                    status_code=409,
                    detail="This topic changed after the rewind preview. Review it again.",
                )
            cutoff, step_cutoff, target_step_id = _resolve_rewind_boundary(
                run, request.rewind_step_id, request.rewind_action_id
            )
            retained_steps = run.workflow_steps[:step_cutoff]
            reused_ids = [step.id for step in retained_steps]
            discarded_count = len(run.messages) - cutoff
            revision = WorkflowRevision(
                from_step_id=target_step_id,
                from_step_title=(
                    run.workflow_steps[step_cutoff].title if step_cutoff < len(run.workflow_steps) else None
                ),
                feedback=request.goal,
                reused_step_ids=reused_ids,
                discarded_message_count=discarded_count,
                archive_available=True,
            )
            snapshots = SnapshotStore(settings.data_root, workdir)
            if request.rewind_preview_token:
                planned_restore, planned_warnings = _rollback_discarded_effects(
                    run, cutoff, snapshots, dry_run=True,
                )
                planned_contents = _restored_file_contents(snapshots, planned_restore)
                planned_issues = _retained_file_read_issues(run, cutoff, planned_contents)
                planned_warnings.extend(_retained_read_warnings(planned_issues))
                if _rewind_preview_token(planned_restore, planned_warnings) != request.rewind_preview_token:
                    raise HTTPException(
                        status_code=409,
                        detail="The files changed after the rewind preview. Review it again.",
                    )
            store.save_revision_archive(run, revision.id)
            restored, rollback_warnings = _rollback_discarded_effects(
                run,
                cutoff,
                snapshots,
            )
            restored_contents = _restored_file_contents(snapshots, restored)
            source_issues = _retained_file_read_issues(run, cutoff, restored_contents)
            rollback_warnings.extend(_retained_read_warnings(source_issues))
            revision.restored_snapshot_ids = restored
            revision.rollback_warnings = rollback_warnings
            run.workflow_revisions.append(revision)
            rewound_revision_id = revision.id
            run.messages = run.messages[:cutoff]
            if source_issues:
                run.messages.append(ChatMessage(
                    role="system",
                    content=(
                        "Some retained file evidence changed or could not be verified. Re-read these paths "
                        f"before relying on prior tool results or claiming completion: {', '.join(source_issues)}"
                    ),
                ))
            run.workflow_steps = retained_steps
            run.workflow_plan = []
            run.stage_notes.pop("understand", None)
            kept_actions = {
                action.id
                for message in run.messages
                for action in message.tool_calls
            }
            store.prune_events_to_actions(run.id, kept_actions)
            store.append_event(RunEvent(
                type="workflow.rewound",
                run_id=run.id,
                title="按反馈回到较早阶段",
                detail=request.goal[:500],
                data={
                    "revision_id": revision.id,
                    "from_step_id": target_step_id,
                    "reused_steps": len(reused_ids),
                    "restored_files": len(restored),
                    "warnings": rollback_warnings,
                },
            ))
        run.status = RunStatus.CREATED
        run.policy.autonomy_threshold = effective_autonomy
        run.policy.capability_rules.setdefault("browser_interaction", "ask")
        user_metadata: dict[str, Any] = {"attachments": attachments}
        if rewound_revision_id:
            user_metadata["rewind_revision_id"] = rewound_revision_id
        run.messages.append(ChatMessage(role="user", content=request.goal, metadata=user_metadata))
    else:
        run = Run(
            goal=request.goal,
            policy=UserPolicy(
                autonomy_threshold=effective_autonomy,
                allowed_scopes=[str(workdir.resolve())],
                capability_rules={
                    **getattr(settings, "default_capability_rules", {}),
                    "browser_interaction": getattr(settings, "default_capability_rules", {}).get(
                        "browser_interaction", "ask"
                    ),
                },
            ),
            messages=[
                ChatMessage(role="system", content=system_prompt(request.goal, execution.interface_language)),
                ChatMessage(role="user", content=request.goal, metadata={"attachments": attachments}),
            ],
        )
    run.execution = execution
    run.policy.allowed_scopes = [str(workdir)]
    if request.browser_session_id:
        active_run_ids = {run_id for run_id, task in _active_tasks.items() if not task.done()}
        try:
            await _computer_use_sessions.bind(request.browser_session_id, run.id, active_run_ids)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    store.save_run(run)

    async def execute() -> None:
        try:
            await cli._run(
                request.goal,
                profile_name,
                effective_autonomy.value,
                str(workdir),
                execution.max_iterations,
                execution.max_tool_calls,
                run=run,
                interface_language=execution.interface_language,
                approval_gate=WebApprovalGate(run.id),
                render_console=False,
                computer_use_manager=_computer_use_sessions,
            )
        except Exception as exc:
            # Provider/MCP initialization can fail before AgentRunner takes
            # ownership. Persist every failure at this outer boundary too.
            try:
                latest = store.load_run(run.id)
            except FileNotFoundError:
                return
            if latest.status == RunStatus.CANCELLED:
                return
            error = (
                str(exc)[:2000]
                if isinstance(exc, ModelRequestError)
                else f"{type(exc).__name__}: {exc}"[:2000]
            )
            latest.status = RunStatus.FAILED
            prefix = "任务执行失败：" if execution.interface_language == "zh" else "Task failed: "
            latest.messages.append(ChatMessage(role="assistant", content=prefix + error))
            store.save_run(latest)
            store.append_event(RunEvent(type="run_failed", run_id=run.id, title="Task failed", detail=error))

    _active_runs[run.id] = run
    task = asyncio.create_task(execute())
    _active_tasks[run.id] = task

    def release_run(done: asyncio.Task) -> None:
        if _active_tasks.get(run.id) is done:
            _active_tasks.pop(run.id, None)
            _active_runs.pop(run.id, None)

    task.add_done_callback(release_run)
    return {"accepted": True, "run_id": run.id}


def start(host: str | None = None, port: int | None = None, open_browser: bool = False) -> None:
    """Run the local-only Web console using the values in clearact.json by default."""
    with (cli._project_root() / "clearact.json").open(encoding="utf-8") as handle:
        web = json.load(handle)["web"]
    selected_host, selected_port = host or web["host"], port or web["port"]
    url = f"http://{selected_host}:{selected_port}"
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"ClearAct gateway is running at {url}")
    uvicorn.run(app, host=selected_host, port=selected_port, log_level="warning")


@app.post("/api/workspaces/validate")
async def validate_workspace(request: WorkspaceRequest) -> dict[str, str]:
    root = cli._project_root()
    settings = load_settings(root)
    workdir = _attachment_workdir(request.path.strip(), settings.workspace_root, root)
    return {"path": str(workdir)}


def _browse_workspace(path: str | None, query: str) -> dict[str, Any]:
    """List folders for the in-page workspace browser without opening a desktop dialog."""
    if not path:
        if os.name == "nt":
            drives = ctypes.windll.kernel32.GetLogicalDrives()
            folders = [f"{chr(65 + index)}:\\" for index in range(26) if drives & (1 << index)]
        else:
            folders = ["/"]
        return {
            "path": None,
            "parent": None,
            "directories": [{"name": folder, "path": folder} for folder in folders],
            "truncated": False,
        }

    directory = Path(path).expanduser()
    if not directory.is_absolute():
        raise HTTPException(status_code=422, detail="请输入完整的文件夹路径。")
    directory = directory.resolve()
    if not directory.is_dir():
        raise HTTPException(status_code=422, detail="文件夹不存在或无法访问。")
    folders: list[dict[str, str]] = []
    truncated = False
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False) or (query and query.casefold() not in entry.name.casefold()):
                    continue
                if len(folders) >= 500:
                    truncated = True
                    break
                folders.append({"name": entry.name, "path": entry.path})
    except OSError as exc:
        raise HTTPException(status_code=403, detail="无法读取这个文件夹，请选择其他位置或直接输入路径。") from exc
    folders.sort(key=lambda folder: folder["name"].casefold())
    parent = directory.parent
    return {
        "path": str(directory),
        "parent": str(parent) if parent != directory else None,
        "directories": folders,
        "truncated": truncated,
    }


@app.get("/api/workspaces/browse")
async def browse_workspace(path: str | None = None, query: str = "") -> dict[str, Any]:
    try:
        return await asyncio.wait_for(asyncio.to_thread(_browse_workspace, path, query[:100]), timeout=5)
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="读取文件夹超时。可返回磁盘列表或直接输入路径。") from exc
