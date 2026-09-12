from __future__ import annotations

import asyncio
import os
from pathlib import Path

from pypdf import PdfReader

from clearact.domain.errors import ScopeViolationError, ToolValidationError
from clearact.domain.models import ToolDefinition, ToolResult
from clearact.tools.base import ToolContext


class _FilesystemTool:
    def _resolve(self, context: ToolContext, raw_path: str, *, writing: bool = False) -> Path:
        candidate = Path(raw_path).expanduser()
        # Never let an unspecified/relative path escape the configured workspace.
        target = candidate.resolve() if candidate.is_absolute() else (context.workspace_root / candidate).resolve()
        if not self._within(target, context.workspace_root) and context.autonomy in {"white", "green"}:
            raise ScopeViolationError("White and green modes only permit files inside the workspace.")
        if writing and self._is_protected_system_path(target):
            raise ScopeViolationError("Writing system locations is blocked in every autonomy mode.")
        return target

    @staticmethod
    def _within(target: Path, root: Path) -> bool:
        try:
            target.relative_to(root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _is_protected_system_path(target: Path) -> bool:
        roots = [
            Path(os.environ.get("SystemRoot", r"C:\\Windows")),
            Path(os.environ.get("ProgramFiles", r"C:\\Program Files")),
        ]
        return any(root.exists() and _FilesystemTool._within(target, root.resolve()) for root in roots)


class ListFilesTool(_FilesystemTool):
    name = "list_files"

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="List files. Relative paths are under the workspace; yellow/red can use absolute paths.",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        )

    async def execute(self, arguments: dict, context: ToolContext, action_id: str) -> ToolResult:
        raw_path = arguments.get("path")
        if not isinstance(raw_path, str):
            raise ToolValidationError("path must be a string")
        target = self._resolve(context, raw_path)
        if not target.is_dir():
            raise ToolValidationError("path must be an existing directory")
        entries = [
            {"path": str(path), "type": "directory" if path.is_dir() else "file"}
            for path in sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
        ]
        visible_entries = entries[:100]
        listing = "\n".join(f"- {item['type']}: {item['path']}" for item in visible_entries)
        return ToolResult(
            action_id=action_id,
            tool_name=self.name,
            ok=True,
            content=(
                f"Found {len(entries)} entries in {target}.\n{listing}"
                + ("\n- … additional entries omitted" if len(entries) > len(visible_entries) else "")
            ),
            metadata={"entries": entries, "path": str(target)},
        )


class ReadFileTool(_FilesystemTool):
    name = "read_file"

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Read a UTF-8 text file. Relative paths are under the workspace; yellow/red can use absolute paths."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "max_chars": {"type": "integer", "minimum": 1, "default": 12000},
                },
                "required": ["path"],
            },
        )

    async def execute(self, arguments: dict, context: ToolContext, action_id: str) -> ToolResult:
        raw_path, max_chars = arguments.get("path"), arguments.get("max_chars", 12000)
        if not isinstance(raw_path, str) or not isinstance(max_chars, int) or max_chars < 1:
            raise ToolValidationError("path and max_chars are invalid")
        target = self._resolve(context, raw_path)
        if not target.is_file():
            raise ToolValidationError("path must be an existing file")
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ToolValidationError("only UTF-8 text files are supported") from exc
        return ToolResult(
            action_id=action_id,
            tool_name=self.name,
            ok=True,
            content=content[:max_chars],
            metadata={"path": str(target), "truncated": len(content) > max_chars},
        )


class ReadPdfTool(_FilesystemTool):
    """Extract user-visible text from uploaded or workspace PDF files."""

    name = "read_pdf"

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Extract text from a PDF in the workspace. Use this for uploaded PDF attachments instead of "
                "read_file. Relative paths are under the workspace."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "max_chars": {"type": "integer", "minimum": 1, "default": 30000},
                },
                "required": ["path"],
            },
        )

    async def execute(self, arguments: dict, context: ToolContext, action_id: str) -> ToolResult:
        raw_path, max_chars = arguments.get("path"), arguments.get("max_chars", 30000)
        if not isinstance(raw_path, str) or not isinstance(max_chars, int) or max_chars < 1:
            raise ToolValidationError("path and max_chars are invalid")
        target = self._resolve(context, raw_path)
        if not target.is_file():
            raise ToolValidationError("path must be an existing file")
        if target.suffix.lower() != ".pdf":
            raise ToolValidationError("read_pdf only supports PDF files")

        def extract() -> tuple[str, int]:
            reader = PdfReader(str(target))
            chunks: list[str] = []
            length = 0
            for page in reader.pages:
                chunk = page.extract_text() or ""
                chunks.append(chunk)
                length += len(chunk)
                if length >= max_chars:
                    break
            return "\n\n".join(chunks), len(reader.pages)

        try:
            content, page_count = await asyncio.to_thread(extract)
        except Exception as exc:
            raise ToolValidationError(f"could not extract text from PDF: {exc}") from exc
        if not content.strip():
            raise ToolValidationError(
                "PDF contains no extractable text; upload page images or use a vision-capable model"
            )
        return ToolResult(
            action_id=action_id,
            tool_name=self.name,
            ok=True,
            content=content[:max_chars],
            metadata={"path": str(target), "page_count": page_count, "truncated": len(content) > max_chars},
        )


class WriteFileTool(_FilesystemTool):
    name = "write_file"

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Create or replace a UTF-8 text file. Relative paths always write under the workspace; "
                "yellow/red can use absolute paths."
            ),
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        )

    async def execute(self, arguments: dict, context: ToolContext, action_id: str) -> ToolResult:
        raw_path, content = arguments.get("path"), arguments.get("content")
        if not isinstance(raw_path, str) or not isinstance(content, str):
            raise ToolValidationError("path and content must be strings")
        target = self._resolve(context, raw_path, writing=True)
        existed = target.exists()
        previous_content = target.read_text(encoding="utf-8") if existed else None
        snapshot_id = (
            context.snapshot_store.save_before_write(target, previous_content, existed)
            if context.snapshot_store
            else None
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return ToolResult(
            action_id=action_id,
            tool_name=self.name,
            ok=True,
            content=f"{'Updated' if existed else 'Created'} {target}.",
            metadata={"path": str(target), "existed": existed, "snapshot_id": snapshot_id},
        )
