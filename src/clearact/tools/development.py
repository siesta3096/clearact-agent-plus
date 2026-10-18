from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from clearact.domain.errors import ScopeViolationError, ToolValidationError
from clearact.domain.models import ToolDefinition, ToolResult
from clearact.tools.artifacts import MAX_ARTIFACT_BYTES, capture_artifact_version, save_artifact
from clearact.tools.base import ToolContext
from clearact.tools.filesystem import _FilesystemTool

_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".clearact"}


class SearchFilesTool(_FilesystemTool):
    name = "search_files"

    def definition(self) -> ToolDefinition:
        return ToolDefinition(name=self.name, description=(
            "Find literal text in workspace code with file paths and line numbers. Skips dependencies and binary files."
        ), parameters={"type": "object", "properties": {
            "query": {"type": "string"}, "path": {"type": "string", "default": "."},
            "glob": {"type": "string", "default": "*"},
            "case_sensitive": {"type": "boolean", "default": False},
        }, "required": ["query"]})

    async def execute(self, arguments: dict, context: ToolContext, action_id: str) -> ToolResult:
        query, raw = arguments.get("query"), arguments.get("path", ".")
        pattern, sensitive = arguments.get("glob", "*"), arguments.get("case_sensitive", False)
        if (not isinstance(query, str) or not 1 <= len(query) <= 512 or not isinstance(raw, str)
                or not isinstance(pattern, str) or not 1 <= len(pattern) <= 128 or type(sensitive) is not bool):
            raise ToolValidationError("query/path/glob/case_sensitive is invalid")
        root = self._resolve(context, raw)
        if not root.is_relative_to(context.workspace_root) or not root.is_dir():
            raise ToolValidationError("search_files path must be a directory inside this workspace")
        matches, inspected, skipped = [], 0, 0
        needle = query if sensitive else query.casefold()
        truncated = False
        for directory, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = sorted(name for name in dirs if name not in _SKIP_DIRS
                             and not (Path(directory) / name).is_symlink())
            for name in sorted(names):
                target = Path(directory) / name
                if not target.match(pattern) or name == ".env" or name.startswith(".env."):
                    continue
                inspected += 1
                if inspected > 1000:
                    truncated = True
                    break
                try:
                    resolved = target.resolve()
                    if target.is_symlink() or not resolved.is_relative_to(root) or target.stat().st_size > 1024 * 1024:
                        skipped += 1
                        continue
                    with target.open("rb") as handle:
                        data = handle.read(1024 * 1024 + 1)
                    if len(data) > 1024 * 1024:
                        skipped += 1
                        continue
                    if b"\x00" in data:
                        skipped += 1
                        continue
                    text = data.decode("utf-8")
                except (OSError, UnicodeError):
                    skipped += 1
                    continue
                for number, line in enumerate(text.splitlines(), 1):
                    if needle in (line if sensitive else line.casefold()):
                        matches.append({"path": target.relative_to(context.workspace_root).as_posix(),
                                        "line": number, "text": line[:500]})
                        if len(matches) >= 100:
                            truncated = True
                            break
                if truncated:
                    break
            if truncated:
                break
        content = "\n".join(f"{item['path']}:{item['line']}: {item['text']}" for item in matches)
        return ToolResult(action_id=action_id, tool_name=self.name, ok=True,
                          content=content or "No matching text found.",
                          metadata={"matches": matches, "inspected_files": min(inspected, 1000),
                                    "skipped_files": skipped, "truncated": truncated})


class EditFileTool(_FilesystemTool):
    name = "edit_file"

    def definition(self) -> ToolDefinition:
        return ToolDefinition(name=self.name, description=(
            "Edit an existing UTF-8 file by replacing one unique exact text match. Read first; ambiguous/stale "
            "matches are rejected. Optional expected_sha256 rejects any change since the known version."
        ), parameters={"type": "object", "properties": {
            "path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"},
            "expected_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        }, "required": ["path", "old_text", "new_text"]})

    async def execute(self, arguments: dict, context: ToolContext, action_id: str) -> ToolResult:
        raw, old, new = arguments.get("path"), arguments.get("old_text"), arguments.get("new_text")
        if not isinstance(raw, str) or not isinstance(old, str) or not old or not isinstance(new, str):
            raise ToolValidationError("path, non-empty old_text and new_text are required")
        target = self._resolve(context, raw, writing=True)
        version = capture_artifact_version(target)
        if not version.existed:
            raise ToolValidationError("edit_file needs an existing file; use write_file for new files")
        if target.stat().st_size > MAX_ARTIFACT_BYTES:
            raise ToolValidationError("File exceeds the editing limit")
        with target.open("rb") as handle:
            data = handle.read(MAX_ARTIFACT_BYTES + 1)
        if len(data) > MAX_ARTIFACT_BYTES:
            raise ToolValidationError("File grew beyond the editing limit")
        expected = arguments.get("expected_sha256")
        if expected is not None and (not isinstance(expected, str) or hashlib.sha256(data).hexdigest() != expected):
            raise ToolValidationError("File changed since the expected version; re-read before editing")
        try:
            text = data.decode("utf-8")
        except UnicodeError as exc:
            raise ToolValidationError("edit_file only supports UTF-8 text") from exc
        if text.count(old) != 1:
            raise ToolValidationError("old_text must match exactly once; read more context before editing")
        result = text.replace(old, new, 1).encode("utf-8")
        # Re-resolve before committing so a changed parent symlink cannot redirect the edit.
        if self._resolve(context, raw, writing=True) != target:
            raise ToolValidationError("Target path changed during editing")
        metadata = save_artifact(context, target, result, expected_version=version)
        return ToolResult(action_id=action_id, tool_name=self.name, ok=True,
                          content=f"Edited {target}; sha256={metadata['content_sha256']}", metadata=metadata)


class _HTMLReferences(HTMLParser):
    def __init__(self):
        super().__init__()
        self.references = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("src"):
            self.references.append(attrs["src"])
        if tag == "link" and attrs.get("rel") == "stylesheet" and attrs.get("href"):
            self.references.append(attrs["href"])


class CheckFilesTool(_FilesystemTool):
    name = "check_files"

    def definition(self) -> ToolDefinition:
        return ToolDefinition(name=self.name, description=(
            "Read-only checks: Python/JSON parsing, HTML local script/style references, JS syntax via installed "
            "Node --check. Pass only .py/.json/.html/.htm/.js/.mjs/.cjs; CSS/TypeScript are unsupported. "
            "Does not execute project code, tests or network requests. Report exact coverage."
        ), parameters={"type": "object", "properties": {
            "paths": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 20},
        }, "required": ["paths"]})

    async def execute(self, arguments: dict, context: ToolContext, action_id: str) -> ToolResult:
        paths = arguments.get("paths")
        if not isinstance(paths, list) or not 1 <= len(paths) <= 20 or any(not isinstance(path, str) for path in paths):
            raise ToolValidationError("paths must contain 1–20 workspace file paths")
        results = []
        for raw in paths:
            detail, passed, coverage = "", False, ""
            try:
                target = self._resolve(context, raw)
                if not target.is_relative_to(context.workspace_root) or not target.is_file():
                    raise ToolValidationError("Check target must be a file inside this workspace")
                if target.stat().st_size > 2 * 1024 * 1024:
                    raise ToolValidationError("Static checks support files up to 2 MB")
                with target.open("rb") as handle:
                    data = handle.read(2 * 1024 * 1024 + 1)
                if len(data) > 2 * 1024 * 1024:
                    raise ToolValidationError("File grew beyond the static check limit")
                text = data.decode("utf-8-sig")
                suffix = target.suffix.lower()
                if suffix == ".py":
                    ast.parse(text, filename=raw)
                    coverage = "Python syntax (no execution)"
                elif suffix == ".json":
                    json.loads(text)
                    coverage = "JSON parsing"
                elif suffix in {".html", ".htm"}:
                    parser = _HTMLReferences()
                    parser.feed(text)
                    for ref in parser.references:
                        if re.match(r"^(?:[a-z][\w+.-]*:|//|#)", ref, re.I):
                            continue
                        local = unquote(urlsplit(ref).path)
                        candidate = (context.workspace_root / local.lstrip("/") if local.startswith("/")
                                     else target.parent / local).resolve()
                        if not candidate.is_relative_to(context.workspace_root) or not candidate.is_file():
                            raise ToolValidationError(f"Missing or out-of-workspace local script/style: {ref}")
                    coverage = "HTML local script/style references (no browser execution)"
                elif suffix in {".js", ".mjs", ".cjs"}:
                    node = shutil.which("node")
                    if not node:
                        raise ToolValidationError("Node.js is unavailable; JS syntax was not checked")
                    executable = Path(node).resolve()
                    if (executable.is_relative_to(context.workspace_root)
                            or os.name == "nt" and executable.suffix.lower() != ".exe"):
                        raise ToolValidationError(
                            "Static checks require an installed Node executable outside the workspace"
                        )
                    env = {key: value for key, value in os.environ.items()
                           if key.upper() not in {"NODE_OPTIONS", "NODE_PATH"}}
                    process = await asyncio.create_subprocess_exec(
                        str(executable), "--check", "--input-type=" + ("commonjs" if suffix == ".cjs" else "module"),
                        cwd=tempfile.gettempdir(), env=env, stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                        **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
                    )
                    try:
                        stdout, stderr = await asyncio.wait_for(process.communicate(data), timeout=15)
                    finally:
                        if process.returncode is None:
                            process.kill()
                            await process.wait()
                    if process.returncode != 0:
                        raise ToolValidationError((stderr or stdout).decode("utf-8", errors="replace")[:1600])
                    coverage = "JavaScript syntax via Node --check (no source execution)"
                else:
                    raise ToolValidationError("Unsupported static check format; use an approved test command")
                if self._resolve(context, raw) != target:
                    raise ToolValidationError("Target path changed during verification")
                with target.open("rb") as handle:
                    current = handle.read(2 * 1024 * 1024 + 1)
                if current != data:
                    raise ToolValidationError("File changed during verification; check again")
                passed, detail = True, "Passed"
            except (OSError, UnicodeError, ValueError, SyntaxError, ToolValidationError,
                    ScopeViolationError, TimeoutError) as exc:
                detail = str(exc)[:1800] or type(exc).__name__
            results.append({"path": raw, "passed": passed, "coverage": coverage, "detail": detail,
                            "content_sha256": hashlib.sha256(data).hexdigest() if passed else None})
        ok = all(item["passed"] for item in results)
        return ToolResult(action_id=action_id, tool_name=self.name, ok=ok,
                          content=json.dumps({"all_passed": ok, "checks": results}, ensure_ascii=False),
                          metadata={"all_passed": ok, "checks": results})
