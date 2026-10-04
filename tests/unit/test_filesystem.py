import asyncio
from pathlib import Path

import pytest

import clearact.tools.filesystem as filesystem
from clearact.domain.errors import ScopeViolationError, ToolValidationError
from clearact.storage.snapshots import SnapshotStore
from clearact.tools.base import ToolContext
from clearact.tools.filesystem import ListFilesTool, ReadFileTool, ReadPdfTool, WriteFileTool


def test_write_then_read_file_in_workspace(workspace):
    async def execute():
        context = ToolContext(workspace)
        write_result = await WriteFileTool().execute(
            {"path": "notes/hello.txt", "content": "你好，ClearAct"}, context, "act_write"
        )
        read_result = await ReadFileTool().execute({"path": "notes/hello.txt"}, context, "act_read")
        return write_result, read_result

    write_result, read_result = asyncio.run(execute())

    assert write_result.ok is True
    assert write_result.metadata["existed"] is False
    assert read_result.content == "你好，ClearAct"
    stat = (workspace / "notes" / "hello.txt").stat()
    assert read_result.metadata["source_version"] == f"{stat.st_mtime_ns}:{stat.st_size}"


def test_read_file_continues_from_character_offset(workspace):
    (workspace / "notes.txt").write_text("你好ABC", encoding="utf-8")
    context = ToolContext(workspace)

    async def execute():
        first = await ReadFileTool().execute({"path": "notes.txt", "max_chars": 3}, context, "first")
        second = await ReadFileTool().execute(
            {"path": "notes.txt", "offset": first.metadata["next_offset"], "max_chars": 3}, context, "second"
        )
        return first, second

    first, second = asyncio.run(execute())
    assert first.content.startswith("你好A")
    assert '"offset": 3' in first.content
    assert first.metadata["truncated"] is True
    assert first.metadata["next_offset"] == 3
    assert second.content == "BC"
    assert second.metadata["truncated"] is False
    assert second.metadata["next_offset"] is None


def test_read_file_streams_large_unicode_file_and_rejects_offset_past_end(workspace, monkeypatch):
    (workspace / "large.txt").write_text("你" * 150_000 + "结束", encoding="utf-8")

    def reject_whole_file_read(_self, *args, **kwargs):
        raise AssertionError("read_file must not load the whole file")

    monkeypatch.setattr(Path, "read_text", reject_whole_file_read)
    context = ToolContext(workspace)
    result = asyncio.run(
        ReadFileTool().execute({"path": "large.txt", "offset": 149_999, "max_chars": 2}, context, "segment")
    )
    assert result.content.startswith("你结")
    assert result.metadata["next_offset"] == 150_001
    assert result.metadata["truncated"] is True

    final = asyncio.run(
        ReadFileTool().execute({"path": "large.txt", "offset": 150_001, "max_chars": 2}, context, "final")
    )
    assert final.content == "束"
    assert final.metadata["next_offset"] is None

    with pytest.raises(ToolValidationError, match="offset exceeds"):
        asyncio.run(ReadFileTool().execute({"path": "large.txt", "offset": 150_003}, context, "past_end"))


def test_oversized_read_requests_are_chunked_for_text_and_pdf(workspace, monkeypatch):
    (workspace / "large.txt").write_text("A" * 60_000, encoding="utf-8")
    (workspace / "large.pdf").write_bytes(b"placeholder")

    class Page:
        def extract_text(self):
            return "B" * 60_000

    class Reader:
        def __init__(self, _path):
            self.pages = [Page()]

    monkeypatch.setattr(filesystem, "PdfReader", Reader)
    context = ToolContext(workspace)

    async def execute():
        text = await ReadFileTool().execute({"path": "large.txt", "max_chars": 1_000_000}, context, "text")
        pdf = await ReadPdfTool().execute({"path": "large.pdf", "max_chars": 1_000_000}, context, "pdf")
        return text, pdf

    text, pdf = asyncio.run(execute())
    assert text.metadata["next_offset"] == 50_000
    assert text.content.startswith("A" * 50_000)
    assert '"max_chars": 50000' in text.content
    assert pdf.metadata["next_page_offset"] == 50_000
    assert pdf.content.startswith("B" * 50_000)
    assert '"max_chars": 50000' in pdf.content
    assert ReadFileTool().definition().parameters["properties"]["max_chars"]["maximum"] == 50_000
    assert ReadPdfTool().definition().parameters["properties"]["max_chars"]["maximum"] == 50_000


def test_read_pdf_continues_with_page_and_page_offset(workspace, monkeypatch):
    (workspace / "report.pdf").write_bytes(b"placeholder")

    class Page:
        def __init__(self, text):
            self.text = text

        def extract_text(self):
            return self.text

    class Reader:
        def __init__(self, _path):
            self.pages = [Page("ABCDE"), Page("FGHIJ")]

    monkeypatch.setattr(filesystem, "PdfReader", Reader)
    context = ToolContext(workspace)

    async def execute():
        tool = ReadPdfTool()
        first = await tool.execute({"path": "report.pdf", "max_chars": 3, "max_pages": 1}, context, "first")
        second = await tool.execute(
            {"path": "report.pdf", "start_page": 1, "page_offset": 3, "max_chars": 3, "max_pages": 1},
            context,
            "second",
        )
        third = await tool.execute(
            {"path": "report.pdf", "start_page": 2, "max_chars": 10}, context, "third"
        )
        whole = await tool.execute({"path": "report.pdf"}, context, "whole")
        return first, second, third, whole

    first, second, third, whole = asyncio.run(execute())
    assert first.content.startswith("ABC")
    assert '"start_page": 1' in first.content
    assert '"page_offset": 3' in first.content
    assert (first.metadata["next_page"], first.metadata["next_page_offset"]) == (1, 3)
    assert first.metadata["truncated"] is True
    assert second.content.startswith("DE")
    assert (second.metadata["next_page"], second.metadata["next_page_offset"]) == (2, 0)
    assert '"start_page": 2' in second.content
    assert third.content == "FGHIJ"
    assert third.metadata["truncated"] is False
    assert third.metadata["next_page"] is None
    assert whole.content == "ABCDE\n\nFGHIJ"
    stat = (workspace / "report.pdf").stat()
    assert whole.metadata["source_version"] == f"{stat.st_mtime_ns}:{stat.st_size}"

    with pytest.raises(ToolValidationError, match="start_page exceeds"):
        asyncio.run(ReadPdfTool().execute({"path": "report.pdf", "start_page": 3}, context, "invalid"))


def test_green_write_rejects_path_outside_workspace(workspace):
    async def execute():
        await WriteFileTool().execute(
            {"path": "../outside.txt", "content": "must not be written"}, ToolContext(workspace), "act_escape"
        )

    with pytest.raises(ScopeViolationError):
        asyncio.run(execute())


@pytest.mark.parametrize("autonomy", ["white", "green"])
@pytest.mark.parametrize("tool", [ReadFileTool(), ListFilesTool()])
def test_low_autonomy_cannot_read_or_list_outside_workspace(workspace, tmp_path, autonomy, tool):
    target = tmp_path / "private.txt"
    target.write_text("private", encoding="utf-8")

    with pytest.raises(ScopeViolationError):
        asyncio.run(tool.execute({"path": str(target)}, ToolContext(workspace, autonomy=autonomy), "act_escape"))


def test_red_can_read_and_write_ordinary_path_outside_workspace(workspace, tmp_path):
    async def execute():
        target = tmp_path / "ordinary" / "note.txt"
        context = ToolContext(workspace, autonomy="red")
        await WriteFileTool().execute({"path": str(target), "content": "allowed"}, context, "act_write")
        return await ReadFileTool().execute({"path": str(target)}, context, "act_read")

    assert asyncio.run(execute()).content == "allowed"


def test_snapshot_restore_reverts_existing_file(tmp_path):
    target = tmp_path / "workspace" / "note.txt"
    target.parent.mkdir()
    target.write_text("before", encoding="utf-8")
    store = SnapshotStore(tmp_path / "data", target.parent)

    snapshot_id = store.save_before_write(target, "before", existed=True)
    target.write_text("after", encoding="utf-8")

    assert store.restore(snapshot_id) == target
    assert target.read_text(encoding="utf-8") == "before"


def test_snapshot_restore_removes_new_file(tmp_path):
    target = tmp_path / "workspace" / "new.txt"
    target.parent.mkdir()
    store = SnapshotStore(tmp_path / "data", target.parent)

    snapshot_id = store.save_before_write(target, None, existed=False)
    target.write_text("created", encoding="utf-8")
    store.restore(snapshot_id)

    assert not target.exists()
