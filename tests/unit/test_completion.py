import hashlib

import pytest

from clearact.domain.models import Action, ChatMessage, CompletionCheck, Run, WorkflowPlanItem, WorkflowStep
from clearact.runtime.completion import evaluate_completion_checks
from clearact.runtime.runner import AgentRunner
from clearact.tools.base import ToolContext


def make_run(checks):
    return Run(goal="Verify deliverables", messages=[ChatMessage(role="user", content="Verify deliverables")],
               workflow_plan=[WorkflowPlanItem(id="work", title="Deliver", summary="Create outputs", checks=checks)],
               workflow_steps=[WorkflowStep(title="Deliver", summary="Create outputs", plan_item_id="work",
                                            status="active", start_message_index=1)])


def outcome(run, name, arguments, *, status="succeeded", metadata=None):
    action = Action(tool_name=name, arguments=arguments)
    run.messages.extend([
        ChatMessage(role="assistant", tool_calls=[action]),
        ChatMessage(role="tool", name=name, tool_call_id=action.id,
                    metadata={"status": status, **(metadata or {})}),
    ])
    run.workflow_steps[-1].action_ids.append(action.id)
    return action


def evaluate(run, workspace):
    return evaluate_completion_checks(run, ToolContext(workspace), include_unreached=True)


@pytest.mark.parametrize("check", [
    {"type": "file"}, {"type": "file", "path": "\x00"}, {"type": "file", "path": " "},
    {"type": "file", "path": "a.txt", "tool_name": "read_file"},
    {"type": "tool_success"}, {"type": "tool_success", "tool_name": "declare_workflow_plan"},
    {"type": "tool_success", "tool_name": "read_file", "min_count": True},
    {"type": "sources_opened", "min_count": 0}, {"type": "sources_opened", "min_count": 21},
    {"type": "sources_opened", "path": "a.txt"}, {"type": "shell", "command": "echo pass"},
])
def test_invalid_checks_reject_the_whole_plan_instead_of_silently_dropping_requirements(check):
    assert AgentRunner._parse_workflow_items([{
        "id": "work", "title": "Work", "summary": "Produce results", "checks": [check],
    }]) == []


def test_old_plan_and_run_records_load_without_new_fields():
    run = Run.model_validate({"goal": "Legacy task", "workflow_plan": [{
        "id": "legacy", "title": "Legacy", "summary": "Existing plan",
    }]})
    assert run.completion_checks == []
    assert run.workflow_plan[0].checks == []


def test_source_checks_ignore_snippets_repeats_fragments_and_navigation(workspace):
    run = make_run([CompletionCheck(type="sources_opened", min_count=2)])
    outcome(run, "web_search", {"query": "evidence"}, metadata={"url": "https://search.example"})
    outcome(run, "fetch_url", {"url": "https://EXAMPLE.com:443/article#one"})
    outcome(run, "fetch_url", {"url": "https://example.com/article#two"})
    outcome(run, "computer_use", {"action": "navigate"}, metadata={"url": "https://another.example"})
    result = evaluate(run, workspace)[0]
    assert result.status == "failed"
    assert "1 个不同" in result.detail
    outcome(run, "computer_use", {"action": "inspect"}, metadata={"url": "https://another.example"})
    assert evaluate(run, workspace)[0].status == "passed"


@pytest.mark.parametrize("status", ["failed", "denied", "skipped", "cancelled", "interrupted"])
def test_unsuccessful_or_reused_results_do_not_count_as_new_tool_success(workspace, status):
    run = make_run([CompletionCheck(type="tool_success", tool_name="execute_command")])
    outcome(run, "execute_command", {"command": ["python", "--version"]},
            status=status, metadata={"exit_code": 0})
    assert evaluate(run, workspace)[0].status == "failed"


def test_command_requires_zero_exit_and_successful_result(workspace):
    run = make_run([CompletionCheck(type="tool_success", tool_name="execute_command")])
    outcome(run, "execute_command", {}, metadata={"exit_code": 7})
    assert evaluate(run, workspace)[0].status == "failed"
    outcome(run, "execute_command", {}, metadata={"exit_code": 0})
    assert evaluate(run, workspace)[0].status == "passed"


def test_old_turn_sources_and_files_cannot_satisfy_new_request(workspace):
    run = make_run([CompletionCheck(type="sources_opened"), CompletionCheck(type="file", path="old.txt")])
    target = workspace / "old.txt"
    target.write_text("old", encoding="utf-8")
    outcome(run, "fetch_url", {"url": "https://example.com"})
    outcome(run, "write_file", {"path": "old.txt", "content": "old"}, metadata={"path": str(target)})
    run.messages.append(ChatMessage(role="user", content="Create a new result"))
    run.workflow_steps[-1].start_message_index = len(run.messages)
    assert [result.status for result in evaluate(run, workspace)] == ["failed", "failed"]


def test_existing_file_without_current_write_is_not_a_completed_deliverable(workspace):
    (workspace / "report.txt").write_text("existing", encoding="utf-8")
    run = make_run([CompletionCheck(type="file", path="report.txt")])
    assert evaluate(run, workspace)[0].status == "failed"


def test_file_check_verifies_latest_record_and_preserves_external_edit(workspace):
    target = workspace / "report.txt"
    target.write_text("new result", encoding="utf-8")
    run = make_run([CompletionCheck(type="file", path="report.txt")])
    outcome(run, "write_file", {"path": "report.txt", "content": "new result"}, metadata={"path": str(target)})
    assert evaluate(run, workspace)[0].status == "passed"
    target.write_text("user edit", encoding="utf-8")
    assert evaluate(run, workspace)[0].status == "failed"
    assert target.read_text(encoding="utf-8") == "user edit"
    target.unlink()
    assert evaluate(run, workspace)[0].status == "failed"


def test_plain_text_renamed_to_office_file_fails_native_structure_check(workspace):
    target = workspace / "report.docx"
    target.write_bytes(b"this is not a native document")
    run = make_run([CompletionCheck(type="file", path="report.docx")])
    outcome(run, "create_document", {"path": "report.docx"}, metadata={
        "path": str(target), "content_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
    })
    assert evaluate(run, workspace)[0].status == "failed"


@pytest.mark.parametrize("restriction", ["outside", "read_denied"])
def test_checks_never_read_outside_workspace_or_without_read_permission(workspace, tmp_path, monkeypatch, restriction):
    path = str(tmp_path / "private.txt") if restriction == "outside" else "report.txt"
    run = make_run([CompletionCheck(type="file", path=path)])
    if restriction == "read_denied":
        run.policy.allow_read = False
    monkeypatch.setattr(type(workspace), "read_bytes", lambda _: pytest.fail("Unauthorized content read"))
    assert evaluate(run, workspace)[0].status == "unverified"


def test_earlier_turn_completed_plan_checks_are_not_imposed_on_followup(workspace):
    run = make_run([CompletionCheck(type="file", path="old.txt")])
    run.messages.append(ChatMessage(role="assistant", content="Previous turn finished."))
    run.messages.append(ChatMessage(role="user", content="Explain the previous answer"))
    assert evaluate_completion_checks(run, ToolContext(workspace), include_unreached=False) == []


def test_plan_revision_keeps_new_checks_and_rejects_mixed_invalid_items():
    items = AgentRunner._parse_workflow_items([{
        "id": "research", "title": "Research", "summary": "Open two sources",
        "checks": [{"type": "sources_opened", "min_count": 2}],
    }])
    assert items[0].checks[0].min_count == 2
    assert AgentRunner._parse_workflow_items([{
        "title": "Invalid", "summary": "Do work", "checks": [{"type": "sources_opened"}] * 7,
    }]) == []
