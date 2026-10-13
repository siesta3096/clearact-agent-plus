import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from clearact import webapp
from clearact.domain.models import Action, ChatMessage, Run, RunExecutionSettings
from clearact.storage.run_store import RunStore
from clearact.storage.snapshots import SnapshotStore
from clearact.tools.base import ToolContext
from clearact.tools.office import CreateDocumentTool, CreateSpreadsheetTool


@pytest.fixture
def artifact_environment(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = SimpleNamespace(workspace_root=workspace, data_root=tmp_path / "data")
    monkeypatch.setattr(webapp, "load_settings", lambda root: settings)
    monkeypatch.setattr(webapp.cli, "_project_root", lambda: tmp_path)
    return settings, RunStore(settings.data_root), TestClient(webapp.app)


def generate_run(settings, *, spreadsheet=False, path=None, autonomy="green", title="Report"):
    tool = CreateSpreadsheetTool() if spreadsheet else CreateDocumentTool()
    arguments = (
        {"path": path or "budget.xlsx", "sheets": [{"name": "Budget", "headers": ["Item", "Cost"],
                                                      "rows": [["A", 10], ["Total", "=SUM(B2:B2)"]]}]}
        if spreadsheet else
        {"path": path or "report.docx", "title": title, "sections": [{"paragraphs": ["Actual content"]}]}
    )
    action = Action(tool_name=tool.name, arguments=arguments)
    context = ToolContext(settings.workspace_root, SnapshotStore(settings.data_root, settings.workspace_root),
                          autonomy=autonomy)
    result = asyncio.run(tool.execute(arguments, context, action.id))
    run = Run(goal="Generate files", execution=RunExecutionSettings(workdir=str(settings.workspace_root)), messages=[
        ChatMessage(role="assistant", tool_calls=[action]),
        ChatMessage(role="tool", name=tool.name, tool_call_id=action.id, content=result.content,
                    metadata={**result.metadata, "status": "succeeded"}),
    ])
    return run, action, result


@pytest.mark.parametrize("spreadsheet", [False, True])
def test_download_returns_only_recorded_unchanged_native_file(artifact_environment, spreadsheet):
    settings, store, client = artifact_environment
    run, action, result = generate_run(settings, spreadsheet=spreadsheet)
    store.save_run(run)
    response = client.get(f"/api/runs/{run.id}/actions/{action.id}/download")
    assert response.status_code == 200
    assert response.content == Path(result.metadata["path"]).read_bytes()
    assert response.content.startswith(b"PK")
    assert "attachment" in response.headers["content-disposition"]
    assert "openxmlformats" in response.headers["content-type"]


@pytest.mark.parametrize("mutation", ["changed", "missing", "wrong_path", "failed", "outside"])
def test_download_rejects_modified_missing_or_unrelated_file(artifact_environment, mutation):
    settings, store, client = artifact_environment
    run, action, result = generate_run(settings)
    target = Path(result.metadata["path"])
    if mutation == "changed":
        target.write_bytes(b"user edit")
    elif mutation == "missing":
        target.unlink()
    elif mutation == "wrong_path":
        run.messages[0].tool_calls[0].arguments["path"] = "another.docx"
    elif mutation == "failed":
        run.messages[1].metadata["status"] = "failed"
    else:
        outside = settings.workspace_root.parent / "outside.docx"
        outside.write_bytes(target.read_bytes())
        run.messages[0].tool_calls[0].arguments["path"] = str(outside)
        run.messages[1].metadata["path"] = str(outside)
    store.save_run(run)
    response = client.get(f"/api/runs/{run.id}/actions/{action.id}/download")
    assert response.status_code in {403, 404, 409}
    assert response.content != b"user edit"


def test_office_rewind_restores_multiple_binary_writes_and_preview_has_no_side_effects(artifact_environment):
    settings, _, _ = artifact_environment
    target = settings.workspace_root / "report.docx"
    original = b"Original\x00\xffbinary"
    target.write_bytes(original)
    first, _, _ = generate_run(settings)
    second, _, _ = generate_run(settings, title="Revised report")
    # Both generated versions need their own hashes and before-images in action order.
    run = first
    run.messages.extend(second.messages)
    snapshots = SnapshotStore(settings.data_root, settings.workspace_root)
    generated = target.read_bytes()
    planned, warnings = webapp._rollback_discarded_effects(run, 0, snapshots, dry_run=True)
    assert len(planned) == 2
    assert warnings == []
    assert target.read_bytes() == generated
    restored, warnings = webapp._rollback_discarded_effects(run, 0, snapshots)
    assert restored == planned
    assert warnings == []
    assert target.read_bytes() == original


def test_office_rewind_preserves_newer_user_edits(artifact_environment):
    settings, _, _ = artifact_environment
    run, _, result = generate_run(settings, spreadsheet=True)
    target = Path(result.metadata["path"])
    target.write_bytes(b"user-edited workbook")
    restored, warnings = webapp._rollback_discarded_effects(
        run, 0, SnapshotStore(settings.data_root, settings.workspace_root),
    )
    assert restored == []
    assert "File changed" in warnings[0]
    assert target.read_bytes() == b"user-edited workbook"


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled", "interrupted"])
def test_rewind_reports_command_side_effects_even_after_failure(artifact_environment, status):
    settings, _, _ = artifact_environment
    action = Action(tool_name="execute_command", arguments={"command": ["python", "example.py"]})
    run = Run(goal="Run command", messages=[ChatMessage(role="assistant", tool_calls=[action]),
        ChatMessage(role="tool", name=action.tool_name, tool_call_id=action.id,
                    metadata={"status": status, "side_effects": "unknown"})])
    restored, warnings = webapp._rollback_discarded_effects(
        run, 0, SnapshotStore(settings.data_root, settings.workspace_root),
    )
    assert restored == []
    assert warnings == [f"External effect may remain: execute_command ({action.id})"]
