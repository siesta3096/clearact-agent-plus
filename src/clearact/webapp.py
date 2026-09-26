from __future__ import annotations

import asyncio
import base64
import binascii
import ctypes
import hashlib
import json
import os
import tempfile
import threading
import webbrowser
from contextlib import suppress
from pathlib import Path
from typing import Any

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
from clearact.tools.mcp import MCPManager

_ASSET_DIR = Path(__file__).with_name("web")

app = FastAPI(title="ClearAct Console")
_active_tasks: dict[str, asyncio.Task] = {}
_active_runs: dict[str, Run] = {}
_pending_approvals: dict[str, dict[str, Any]] = {}


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
    interface_language: str | None = Field(default=None, pattern="^(zh|en)$")
    profile: str | None = None
    workdir: str | None = None
    autonomy: RiskLevel | None = None
    max_iterations: int | None = Field(default=None, ge=1, le=10_000)
    max_tool_calls: int | None = Field(default=None, ge=1, le=100_000)
    attachments: list[AttachmentReference] = Field(default_factory=list, max_length=8)


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
    run: Run, cutoff: int, snapshot_store: SnapshotStore
) -> tuple[list[str], list[str]]:
    """Undo local file writes in reverse order and flag effects we cannot undo."""
    actions = {
        action.id: action
        for message in run.messages[cutoff:]
        for action in message.tool_calls
    }
    restored: list[str] = []
    warnings: list[str] = []
    for message in reversed(run.messages[cutoff:]):
        if message.role != "tool" or message.metadata.get("status") != "succeeded":
            continue
        action = actions.get(message.tool_call_id or "")
        snapshot_id = message.metadata.get("snapshot_id")
        if isinstance(snapshot_id, str) and snapshot_id:
            try:
                snapshot_store.restore(snapshot_id)
                restored.append(snapshot_id)
            except (FileNotFoundError, OSError, ValueError) as exc:
                warnings.append(f"Could not restore {snapshot_id}: {exc}")
        elif action and (action.tool_name.startswith("mcp__") or action.tool_name == "write_file"):
            warnings.append(f"External effect may remain: {action.tool_name} ({action.id})")
    return restored, warnings


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
    root = RunStore(load_settings(cli._project_root()).data_root)._root
    path, events = root / f"{run_id}.json", root / f"{run_id}.events.jsonl"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Topic not found.")
    path.unlink()
    if events.exists():
        events.unlink()
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

    if run is not None:
        # A normal follow-up resumes the topic. A stage rewind deliberately
        # discards that action and everything after it, then restarts from the
        # retained earlier context plus the user's feedback.
        if request.rewind_step_id or request.rewind_action_id:
            step_cutoff = None
            cutoff = None
            target_step_id = request.rewind_step_id
            if request.rewind_step_id:
                step_cutoff = next(
                    (index for index, step in enumerate(run.workflow_steps) if step.id == request.rewind_step_id),
                    None,
                )
                if step_cutoff is not None:
                    step = run.workflow_steps[step_cutoff]
                    cutoff = step.start_message_index
                    if step.id == "understand":
                        cutoff = next(
                            (index for index, message in enumerate(run.messages) if message.role == "assistant"),
                            len(run.messages),
                        )
            else:
                cutoff = next(
                    (
                        index
                        for index, message in enumerate(run.messages)
                        if any(action.id == request.rewind_action_id for action in message.tool_calls)
                    ),
                    None,
                )
                step_cutoff = next(
                    (
                        index
                        for index, step in enumerate(run.workflow_steps)
                        if request.rewind_action_id in step.action_ids
                    ),
                    len(run.workflow_steps),
                )
                target_step_id = next(
                    (
                        step.id
                        for step in run.workflow_steps
                        if request.rewind_action_id in step.action_ids
                    ),
                    request.rewind_action_id,
                )
            if cutoff is None or step_cutoff is None:
                raise HTTPException(status_code=422, detail="The selected workflow step is no longer available.")
            retained_steps = run.workflow_steps[:step_cutoff]
            reused_ids = [step.id for step in retained_steps]
            discarded_count = len(run.messages) - cutoff
            restored, rollback_warnings = _rollback_discarded_effects(
                run,
                cutoff,
                SnapshotStore(settings.data_root, workdir),
            )
            run.workflow_revisions.append(
                WorkflowRevision(
                    from_step_id=target_step_id or "unknown",
                    feedback=request.goal,
                    reused_step_ids=reused_ids,
                    discarded_message_count=discarded_count,
                    restored_snapshot_ids=restored,
                    rollback_warnings=rollback_warnings,
                )
            )
            run.messages = run.messages[:cutoff]
            run.workflow_steps = retained_steps
            run.workflow_plan = []
            run.stage_notes.pop("understand", None)
            kept_actions = {
                action.id
                for message in run.messages
                for action in message.tool_calls
                if action.tool_name not in {"declare_workflow_plan", "declare_workflow_step"}
            }
            store.prune_events_to_actions(run.id, kept_actions)
        run.status = RunStatus.CREATED
        run.policy.autonomy_threshold = effective_autonomy
        run.messages.append(ChatMessage(role="user", content=request.goal, metadata={"attachments": attachments}))
    else:
        run = Run(
            goal=request.goal,
            policy=UserPolicy(
                autonomy_threshold=effective_autonomy,
                allowed_scopes=[str(workdir.resolve())],
                capability_rules=getattr(settings, "default_capability_rules", {}),
            ),
            messages=[
                ChatMessage(role="system", content=system_prompt(request.goal, execution.interface_language)),
                ChatMessage(role="user", content=request.goal, metadata={"attachments": attachments}),
            ],
        )
    run.execution = execution
    run.policy.allowed_scopes = [str(workdir)]
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
