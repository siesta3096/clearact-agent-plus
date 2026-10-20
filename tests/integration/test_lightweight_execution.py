"""Small work should complete without planning round trips or fabricated extra deliverables."""

import asyncio

from test_development_agent import call, developer
from test_task_scenarios import ScriptedProvider, _run

from clearact.domain.enums import RunStatus
from clearact.domain.models import LLMResponse


def test_small_file_task_writes_immediately_and_finishes_in_two_requests(workspace):
    provider = ScriptedProvider([
        LLMResponse(tool_calls=[call("write_file", path="hello.txt", content="hello\n")]),
        LLMResponse(content="已保存 hello.txt。"),
    ])
    runner, _ = developer(workspace, provider)
    run = _run("创建 hello.txt，内容是 hello。")
    asyncio.run(runner.run(run))
    assert run.status is RunStatus.COMPLETED
    assert (workspace / "hello.txt").read_text() == "hello\n"
    assert len(provider.inputs) == 2
    assert run.workflow_plan == []
    assert [message.name for message in run.messages if message.role == "tool"] == ["write_file"]


def test_no_plan_does_not_allow_claiming_an_unwritten_explicit_file(workspace):
    (workspace / "report.txt").write_text("Original report", encoding="utf-8")
    provider = ScriptedProvider([
        LLMResponse(content="已写入 report.txt。"), LLMResponse(content="已写入 report.txt。"),
    ])
    runner, _ = developer(workspace, provider)
    run = _run("创建 report.txt，写入本次总结。")
    asyncio.run(runner.run(run))
    assert run.status is RunStatus.NEEDS_REVIEW
    assert "report.txt" in run.stage_notes["completion_review"]
    assert (workspace / "report.txt").read_text() == "Original report"
    assert len(provider.inputs) == 2


def test_single_file_project_needs_no_generated_architecture_or_extra_source_files(workspace):
    provider = ScriptedProvider([
        LLMResponse(tool_calls=[call("list_files", path="."),
                                call("write_file", path="app.py", content="print('hello')\n")]),
        LLMResponse(tool_calls=[call("check_files", paths=["app.py"])]),
        LLMResponse(content="已写入并检查。"),
    ])
    runner, _ = developer(workspace, provider)
    run = _run("创建一个单文件 Python 项目 app.py，打印 hello。")
    asyncio.run(runner.run(run))
    assert run.status is RunStatus.COMPLETED
    assert run.delivery_report.passed
    assert {file.path for file in run.delivery_report.files} == {"app.py"}
    assert not (workspace / "README.md").exists()
    assert not (workspace / "ARCHITECTURE.md").exists()
    assert len(provider.inputs) == 3


def test_explicit_architecture_file_is_still_required_when_user_asks_for_it(workspace):
    provider = ScriptedProvider([
        LLMResponse(tool_calls=[call("list_files", path="."),
                                call("write_file", path="app.py", content="print('hello')\n")]),
        LLMResponse(tool_calls=[call("check_files", paths=["app.py"])]),
        LLMResponse(content="完成。"), LLMResponse(content="完成。"),
    ])
    runner, _ = developer(workspace, provider)
    run = _run("创建 Python 项目 app.py，并创建 ARCHITECTURE.md。")
    asyncio.run(runner.run(run))
    assert run.status is RunStatus.NEEDS_REVIEW
    assert any(check.key == "requested_paths" and not check.passed for check in run.delivery_report.checks)
