import asyncio

import pytest

from clearact.domain.errors import ScopeViolationError
from clearact.storage.snapshots import SnapshotStore
from clearact.tools.base import ToolContext
from clearact.tools.filesystem import ListFilesTool, ReadFileTool, WriteFileTool


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
