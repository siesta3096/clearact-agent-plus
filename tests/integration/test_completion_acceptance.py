import asyncio
import sys

import pytest

from clearact.domain.enums import RunStatus
from clearact.domain.models import Action, ChatMessage, LLMResponse, ToolDefinition, ToolResult
from tests.integration.test_task_scenarios import RecordingApprovalGate, ScriptedProvider, _plan, _run, _runner, _step


def phase(checks, *, kind="files"):
    return {"id": "deliver", "title": "Produce requested result", "summary": "Verify actual results",
            "kind": kind, "checks": checks}


def execute(workspace, item, actions=(), *, repair=None, approval=False):
    responses = [_plan([item]), LLMResponse(tool_calls=[_step(item), *actions]),
                 LLMResponse(content="The task is complete.")]
    if repair:
        responses.append(LLMResponse(tool_calls=repair))
    responses.append(LLMResponse(content="Final answer after completion review."))
    provider = ScriptedProvider(responses)
    runner, store = _runner(workspace, provider, RecordingApprovalGate(approval))
    run = _run("Produce the requested deliverables")
    asyncio.run(runner.run(run))
    return run, store.load_run(run.id), store, provider, runner


def test_model_claiming_success_without_artifact_ends_needs_review(workspace):
    item = phase([{"type": "file", "path": "report.docx"}])
    run, saved, store, provider, _ = execute(workspace, item)
    assert saved.status is RunStatus.NEEDS_REVIEW
    assert saved.workflow_steps[-1].status == "needs_review"
    assert saved.completion_checks[0].status == "failed"
    assert not (workspace / "report.docx").exists()
    assert len(provider.inputs) == 4  # One bounded repair opportunity, no endless retry.
    events = store.load_events(run.id)
    assert sum(event.type == "completion.review_requested" for event in events) == 1
    assert events[-1].type == "run.completed"
    assert events[-1].data["needs_review"] is True
    assert events[-1].title != "任务已完成"


@pytest.mark.parametrize("kind", ["docx", "xlsx"])
def test_real_office_artifact_created_during_repair_passes_and_clears_warning(workspace, kind):
    arguments = (
        {"path": "report.docx", "title": "Result", "sections": [{"paragraphs": ["Verified native output."]}]}
        if kind == "docx" else
        {"path": "report.xlsx", "sheets": [{"name": "Result", "headers": ["A"], "rows": [[1]]}]}
    )
    create = Action(tool_name="create_document" if kind == "docx" else "create_spreadsheet", arguments=arguments)
    item = phase([{"type": "file", "path": f"report.{kind}", "format": kind}])
    run, saved, _, provider, _ = execute(workspace, item, repair=[create])
    assert saved.status is RunStatus.COMPLETED
    assert saved.completion_checks[0].status == "passed"
    assert saved.workflow_steps[-1].status == "completed"
    assert "completion_review" not in saved.stage_notes
    assert any("Failed acceptance checks:" in (message.content or "") for message in provider.inputs[3])
    assert (workspace / f"report.{kind}").read_bytes().startswith(b"PK")
    assert run.completion_checks == saved.completion_checks


def test_text_masquerading_as_native_document_is_not_accepted(workspace):
    item = phase([{"type": "file", "path": "report.docx"}])
    write = Action(tool_name="write_file", arguments={"path": "report.docx", "content": "Plain text only"})
    _, saved, _, _, _ = execute(workspace, item, [write])
    assert saved.status is RunStatus.NEEDS_REVIEW
    assert "DOCX" in saved.completion_checks[0].detail
    assert (workspace / "report.docx").read_text(encoding="utf-8") == "Plain text only"


@pytest.mark.parametrize("granted,exit_code", [(False, 0), (True, 7), (True, 0)])
def test_command_acceptance_uses_approval_and_actual_success(workspace, granted, exit_code):
    item = phase([{"type": "tool_success", "tool_name": "execute_command"}], kind="external")
    command = Action(tool_name="execute_command", arguments={"command": [
        sys.executable, "-c", f"from pathlib import Path; import sys; Path('marker.txt').write_text('ran'); "
        f"sys.exit({exit_code})",
    ]})
    _, saved, _, _, _ = execute(workspace, item, [command], approval=granted)
    passed = granted and exit_code == 0
    assert saved.status is (RunStatus.COMPLETED if passed else RunStatus.NEEDS_REVIEW)
    assert saved.completion_checks[0].status == ("passed" if passed else "failed")
    assert (workspace / "marker.txt").exists() == granted


class FakeFetch:
    name = "fetch_url"

    def definition(self):
        return ToolDefinition(name=self.name, description="Local fixed fetch result", parameters={"type": "object"})

    async def execute(self, arguments, context, action_id):
        return ToolResult(action_id=action_id, tool_name=self.name, ok=True, content="Actual tool body",
                          metadata={"url": arguments["url"]})


@pytest.mark.parametrize("second_url,passed", [
    ("https://example.com/a#second", False), ("https://example.com/b", True),
])
def test_opened_source_check_uses_unique_current_turn_results(workspace, second_url, passed):
    item = phase([{"type": "sources_opened", "min_count": 2}], kind="research")
    fetches = [Action(tool_name="fetch_url", arguments={"url": url})
               for url in ("https://example.com/a#first", second_url)]
    provider = ScriptedProvider([_plan([item]), LLMResponse(tool_calls=[_step(item), *fetches]),
                                 LLMResponse(content="Done"), LLMResponse(content="Review complete")])
    runner, store = _runner(workspace, provider, RecordingApprovalGate(False))
    runner._registry.register(FakeFetch())
    run = _run("Research evidence for the requested report")
    asyncio.run(runner.run(run))
    saved = store.load_run(run.id)
    assert saved.status is (RunStatus.COMPLETED if passed else RunStatus.NEEDS_REVIEW)
    assert saved.completion_checks[0].status == ("passed" if passed else "failed")


def test_followup_can_explain_previous_result_without_reapplying_old_checks(workspace):
    item = phase([{"type": "file", "path": "report.docx"}])
    run, _, _, _, _ = execute(workspace, item)
    assert run.status is RunStatus.NEEDS_REVIEW
    run.status = RunStatus.CREATED  # The same transition performed by the Web follow-up endpoint.
    run.messages.append(ChatMessage(role="user", content="Explain what remains incomplete; do not create files."))
    provider = ScriptedProvider([LLMResponse(content="The file was not generated.")])
    runner, store = _runner(workspace, provider, RecordingApprovalGate(False))
    asyncio.run(runner.run(run))
    saved = store.load_run(run.id)
    assert saved.status is RunStatus.COMPLETED
    assert saved.completion_checks == []
    assert "completion_review" not in saved.stage_notes
    assert not (workspace / "report.docx").exists()
