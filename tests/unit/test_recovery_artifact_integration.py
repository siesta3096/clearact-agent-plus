import asyncio
from types import SimpleNamespace

import pytest

from clearact import webapp
from clearact.domain.enums import RunStatus
from clearact.domain.errors import ToolValidationError
from clearact.domain.models import Action, ChatMessage, Run, RunExecutionSettings, WorkflowStep
from clearact.storage.run_store import RunStore
from clearact.storage.snapshots import SnapshotStore
from clearact.tools import office
from clearact.tools.base import ToolContext
from clearact.tools.office import CreateDocumentTool, CreateSpreadsheetTool


@pytest.fixture
def recovery_artifact_environment(workspace, tmp_path, monkeypatch):
    settings = SimpleNamespace(workspace_root=workspace, data_root=tmp_path / "data")
    monkeypatch.setattr(webapp, "load_settings", lambda root: settings)
    monkeypatch.setattr(webapp.cli, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(webapp, "_active_tasks", {})
    monkeypatch.setattr(webapp, "_active_runs", {})
    monkeypatch.setattr(webapp, "_pending_approvals", {})
    return settings, RunStore(settings.data_root), SnapshotStore(settings.data_root, workspace)


@pytest.mark.parametrize("spreadsheet", [False, True])
def test_rewind_reports_office_write_that_committed_before_restart_without_a_saved_result(
    recovery_artifact_environment, spreadsheet,
):
    settings, store, snapshots = recovery_artifact_environment
    tool = CreateSpreadsheetTool() if spreadsheet else CreateDocumentTool()
    arguments = (
        {"path": "report.xlsx", "sheets": [{"name": "Report", "headers": ["Value"], "rows": [[10]]}]}
        if spreadsheet else
        {"path": "report.docx", "title": "Report", "sections": [{"paragraphs": ["Updated content"]}]}
    )
    target = settings.workspace_root / arguments["path"]
    original = b"Original user file\x00\xff"
    target.write_bytes(original)
    action = Action(tool_name=tool.name, arguments=arguments)
    run = Run(
        goal="Update report", status=RunStatus.RUNNING,
        execution=RunExecutionSettings(workdir=str(settings.workspace_root)),
        messages=[ChatMessage(role="assistant", tool_calls=[action])],
    )
    store.save_run(run)
    # Reproduce a crash after the atomic write, before its ToolResult was saved.
    asyncio.run(tool.execute(arguments, ToolContext(settings.workspace_root, snapshots), action.id))
    generated = target.read_bytes()
    assert generated != original
    assert webapp._interrupt_incomplete_runs(store) == 1
    restarted = store.load_run(run.id)
    assert restarted.messages[1].metadata["status"] == "interrupted"

    restorable, warnings = webapp._rollback_discarded_effects(restarted, 0, snapshots, dry_run=True)
    assert restorable == []
    assert warnings == [f"External effect may remain: {tool.name} ({action.id})"]
    assert target.read_bytes() == generated


def test_recovery_updates_phase_boundaries_after_inserting_missing_historical_results(recovery_artifact_environment):
    _, store, _ = recovery_artifact_environment
    first = Action(tool_name="execute_command", arguments={"command": ["python", "old.py"]})
    second = Action(tool_name="create_document", arguments={"path": "report.docx"})
    step = WorkflowStep(
        title="Generate report", summary="Write report after feedback", status="active",
        start_message_index=2, action_ids=[second.id],
    )
    run = Run(
        goal="Finish report", status=RunStatus.RUNNING,
        messages=[
            ChatMessage(role="assistant", tool_calls=[first]),
            ChatMessage(role="user", content="Use the revised requirements"),
            ChatMessage(role="assistant", tool_calls=[second]),
        ],
        workflow_steps=[step],
    )
    store.save_run(run)
    webapp._interrupt_incomplete_runs(store)
    restarted = store.load_run(run.id)
    cutoff, _, _ = webapp._resolve_rewind_boundary(restarted, step.id, None)
    assert cutoff == 3
    assert restarted.messages[cutoff].role == "assistant"
    assert restarted.messages[cutoff].tool_calls[0].id == second.id
    assert restarted.messages[cutoff - 1].role == "user"


@pytest.mark.parametrize("existed", [False, True])
def test_office_generation_preserves_user_changes_made_while_rendering(
    recovery_artifact_environment, monkeypatch, existed,
):
    settings, _, snapshots = recovery_artifact_environment
    target = settings.workspace_root / "report.docx"
    if existed:
        target.write_bytes(b"Previously approved report")
    user_edit = b"New user report saved while generation runs"

    def render_with_concurrent_user_edit(content):
        target.write_bytes(user_edit)
        return b"New generated artifact"

    monkeypatch.setattr(office, "_render_document", render_with_concurrent_user_edit)
    with pytest.raises(ToolValidationError, match="changed"):
        asyncio.run(CreateDocumentTool().execute(
            {"path": "report.docx", "title": "Report", "sections": [{"paragraphs": ["Body"]}]},
            ToolContext(settings.workspace_root, snapshots), "concurrent_change",
        ))
    assert target.read_bytes() == user_edit
