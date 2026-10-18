import asyncio
import hashlib
import json
import shutil

import pytest

from clearact.domain.errors import ScopeViolationError, ToolValidationError
from clearact.storage.snapshots import SnapshotStore
from clearact.tools.base import ToolContext
from clearact.tools.development import CheckFilesTool, EditFileTool, SearchFilesTool


def context(root):
    return ToolContext(root, SnapshotStore(root.parent / "data", root))


def test_search_returns_source_lines_and_skips_secrets_dependencies_binary(workspace):
    (workspace / "app.py").write_text("# Needle\nprint('NEEDLE')\n", encoding="utf-8")
    (workspace / ".env").write_text("Needle=secret", encoding="utf-8")
    (workspace / "blob").write_bytes(b"Needle\x00")
    (workspace / "node_modules").mkdir()
    (workspace / "node_modules" / "pkg.js").write_text("Needle", encoding="utf-8")
    result = asyncio.run(SearchFilesTool().execute({"query": "needle"}, context(workspace), "search"))
    assert [(item["path"], item["line"]) for item in result.metadata["matches"]] == [("app.py", 1), ("app.py", 2)]
    assert "secret" not in result.content


@pytest.mark.parametrize("mutation", ["ambiguous", "stale", "missing"])
def test_edit_rejects_ambiguous_stale_missing_without_overwrite(workspace, mutation):
    target = workspace / "app.py"
    target.write_text("old old" if mutation == "ambiguous" else "old", encoding="utf-8")
    arguments = {"path": "app.py", "old_text": "old", "new_text": "new"}
    if mutation == "stale":
        arguments["expected_sha256"] = "0" * 64
    if mutation == "missing":
        target.unlink()
    before = target.read_bytes() if target.exists() else None
    with pytest.raises(ToolValidationError):
        asyncio.run(EditFileTool().execute(arguments, context(workspace), "edit"))
    assert (target.read_bytes() if target.exists() else None) == before


def test_edit_preserves_crlf_and_can_restore_original_snapshot(workspace):
    target = workspace / "app.py"
    original = b"one\r\ntwo\r\n"
    target.write_bytes(original)
    ctx = context(workspace)
    result = asyncio.run(EditFileTool().execute({"path": "app.py", "old_text": "two", "new_text": "three",
                                                "expected_sha256": hashlib.sha256(original).hexdigest()}, ctx, "edit"))
    assert target.read_bytes() == b"one\r\nthree\r\n"
    assert result.metadata["content_sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    ctx.snapshot_store.restore(result.metadata["snapshot_id"])
    assert target.read_bytes() == original


@pytest.mark.parametrize(("name", "source", "passed"), [
    ("app.py", "def broken(:\n", False), ("data.json", "{bad}", False),
    ("data.json", '{"ok":true}', True), ("index.html", '<script src="missing.js"></script>', False),
    ("app.css", "body{}", False),
])
def test_static_checks_honestly_report_coverage(workspace, name, source, passed):
    (workspace / name).write_text(source, encoding="utf-8")
    result = asyncio.run(CheckFilesTool().execute({"paths": [name]}, context(workspace), "check"))
    assert result.ok is passed
    assert json.loads(result.content)["all_passed"] is passed
    assert bool(result.metadata["checks"][0]["content_sha256"]) is passed


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js unavailable")
def test_js_check_does_not_execute_source_and_reports_syntax_failure(workspace):
    (workspace / "app.js").write_text("throw new Error('DO NOT EXECUTE');\nexport const value = 1;", encoding="utf-8")
    (workspace / "broken.js").write_text("export const = ;", encoding="utf-8")
    result = asyncio.run(CheckFilesTool().execute({"paths": ["app.js", "broken.js"]}, context(workspace), "check"))
    assert not result.ok
    assert result.metadata["checks"][0]["passed"] is True
    assert result.metadata["checks"][1]["passed"] is False


def test_native_checker_rejects_workspace_node_instead_of_executing_it(workspace, monkeypatch):
    (workspace / "app.js").write_text("export const x=1;", encoding="utf-8")
    fake_node = workspace / "node.cmd"
    fake_node.write_text("echo must not execute", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda _: str(fake_node))
    result = asyncio.run(CheckFilesTool().execute({"paths": ["app.js"]}, context(workspace), "untrusted_node"))
    assert not result.ok
    assert "outside the workspace" in result.content


@pytest.mark.parametrize("tool,args", [
    (SearchFilesTool(), {"query": "secret", "path": ".."}),
    (EditFileTool(), {"path": "../secret.txt", "old_text": "secret", "new_text": "changed"}),
    (CheckFilesTool(), {"paths": ["../secret.py"]}),
])
def test_development_tools_keep_workspace_scope(workspace, tool, args):
    if tool.name == "check_files":
        assert not asyncio.run(tool.execute(args, context(workspace), "scope")).ok
    else:
        with pytest.raises((ToolValidationError, ScopeViolationError)):
            asyncio.run(tool.execute(args, context(workspace), "scope"))
