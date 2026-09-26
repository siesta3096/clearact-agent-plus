from __future__ import annotations

import asyncio
import json
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
            metadata={
                "entries": visible_entries,
                "entry_count": len(entries),
                "truncated": len(entries) > len(visible_entries),
                "path": str(target),
            },
        )


class ReadFileTool(_FilesystemTool):
    name = "read_file"

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Read a UTF-8 text file in character chunks. Use next_offset from a truncated result as "
                "the offset for the next read. Relative paths are under the workspace; "
                "yellow/red can use absolute paths."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "max_chars": {"type": "integer", "minimum": 1, "default": 12000},
                    "offset": {"type": "integer", "minimum": 0, "default": 0},
                },
                "required": ["path"],
            },
        )

    async def execute(self, arguments: dict, context: ToolContext, action_id: str) -> ToolResult:
        raw_path, max_chars = arguments.get("path"), arguments.get("max_chars", 12000)
        offset = arguments.get("offset", 0)
        if (
            not isinstance(raw_path, str)
            or type(max_chars) is not int
            or max_chars < 1
            or type(offset) is not int
            or offset < 0
        ):
            raise ToolValidationError("path, max_chars, and offset are invalid")
        target = self._resolve(context, raw_path)
        if not target.is_file():
            raise ToolValidationError("path must be an existing file")
        try:
            with target.open("r", encoding="utf-8") as handle:
                remaining = offset
                while remaining:
                    skipped = handle.read(min(remaining, 8192))
                    if not skipped:
                        raise ToolValidationError("offset exceeds the file length")
                    remaining -= len(skipped)
                chunk = handle.read(max_chars)
                truncated = bool(handle.read(1))
        except UnicodeDecodeError as exc:
            raise ToolValidationError("only UTF-8 text files are supported") from exc
        next_offset = offset + len(chunk) if truncated else None
        if next_offset is not None:
            continuation = json.dumps(
                {"path": raw_path, "offset": next_offset, "max_chars": max_chars}, ensure_ascii=False
            )
            chunk += f"\n\n[Output truncated. Continue with read_file {continuation}.]"
        return ToolResult(
            action_id=action_id,
            tool_name=self.name,
            ok=True,
            content=chunk,
            metadata={"path": str(target), "truncated": next_offset is not None, "next_offset": next_offset},
        )


class ReadPdfTool(_FilesystemTool):
    """Extract user-visible text from uploaded or workspace PDF files."""

    name = "read_pdf"

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Extract text from a PDF in page ranges. Use next_page and next_page_offset from a "
                "truncated result to continue reading without losing text. Use this for uploaded PDF "
                "attachments instead of read_file. Relative paths are under the workspace."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "max_chars": {"type": "integer", "minimum": 1, "default": 30000},
                    "start_page": {"type": "integer", "minimum": 1, "default": 1},
                    "max_pages": {"type": "integer", "minimum": 1},
                    "page_offset": {"type": "integer", "minimum": 0, "default": 0},
                },
                "required": ["path"],
            },
        )

    async def execute(self, arguments: dict, context: ToolContext, action_id: str) -> ToolResult:
        raw_path, max_chars = arguments.get("path"), arguments.get("max_chars", 30000)
        start_page = arguments.get("start_page", 1)
        max_pages = arguments.get("max_pages")
        page_offset = arguments.get("page_offset", 0)
        if (
            not isinstance(raw_path, str)
            or type(max_chars) is not int
            or max_chars < 1
            or type(start_page) is not int
            or start_page < 1
            or (max_pages is not None and (type(max_pages) is not int or max_pages < 1))
            or type(page_offset) is not int
            or page_offset < 0
        ):
            raise ToolValidationError("path, max_chars, start_page, max_pages, or page_offset is invalid")
        target = self._resolve(context, raw_path)
        if not target.is_file():
            raise ToolValidationError("path must be an existing file")
        if target.suffix.lower() != ".pdf":
            raise ToolValidationError("read_pdf only supports PDF files")

        def extract() -> tuple[str, int, int | None, int | None]:
            reader = PdfReader(str(target))
            page_count = len(reader.pages)
            if start_page > page_count:
                raise ToolValidationError("start_page exceeds the PDF page count")
            last_page = min(page_count, start_page + max_pages - 1) if max_pages else page_count
            chunks: list[str] = []
            used = 0
            for page_number in range(start_page, last_page + 1):
                page_text = reader.pages[page_number - 1].extract_text() or ""
                offset = page_offset if page_number == start_page else 0
                if offset > len(page_text):
                    raise ToolValidationError("page_offset exceeds the page text length")
                page_text = page_text[offset:]
                separator = "\n\n" if chunks else ""
                remaining = max_chars - used
                if len(separator) > remaining:
                    return "".join(chunks), page_count, page_number, offset
                available = remaining - len(separator)
                if len(page_text) > available:
                    chunks.append(separator + page_text[:available])
                    return "".join(chunks), page_count, page_number, offset + available
                chunks.append(separator + page_text)
                used += len(separator) + len(page_text)
            next_page = last_page + 1 if last_page < page_count else None
            return "".join(chunks), page_count, next_page, 0 if next_page is not None else None

        try:
            content, page_count, next_page, next_page_offset = await asyncio.to_thread(extract)
        except ToolValidationError:
            raise
        except Exception as exc:
            raise ToolValidationError(f"could not extract text from PDF: {exc}") from exc
        if not content.strip() and next_page is None:
            raise ToolValidationError(
                "PDF contains no extractable text; upload page images or use a vision-capable model"
            )
        if next_page is not None:
            continuation = {"path": raw_path, "start_page": next_page, "max_chars": max_chars}
            if max_pages is not None:
                continuation["max_pages"] = max_pages
            if next_page_offset:
                continuation["page_offset"] = next_page_offset
            content += f"\n\n[Output truncated. Continue with read_pdf {json.dumps(continuation, ensure_ascii=False)}.]"
        return ToolResult(
            action_id=action_id,
            tool_name=self.name,
            ok=True,
            content=content,
            metadata={
                "path": str(target),
                "page_count": page_count,
                "truncated": next_page is not None,
                "next_page": next_page,
                "next_page_offset": next_page_offset,
            },
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
