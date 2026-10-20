"""Long returned thinking must not destroy the next real tool round or the recorded history."""

import asyncio

import pytest
from test_development_agent import call, developer
from test_task_scenarios import ScriptedProvider, _run

from clearact.context.budget import ContextBudget
from clearact.context.builder import ContextBuilder
from clearact.domain.enums import RunStatus
from clearact.domain.models import LLMResponse, WorkflowPlanItem
from clearact.tools.project_preview import PreviewProjectTool


def test_large_thinking_stage_then_real_file_delivery_and_browser_check(workspace):
    thinking = "r" * 74_119
    stage = call("declare_workflow_step", title="实现网页游戏", summary="实际创建页面、脚本与说明。", kind="analysis")
    files = {
        "index.html": '<html><button id="play">Play</button><p id="result">Ready</p>'
                      '<script src="app.js"></script></html>',
        "app.js": "document.querySelector('#play').onclick=()=>"
                  "document.querySelector('#result').textContent='Ending A';",
        "README.md": "Open index.html. Click Play. Browser checks verify the result.\n",
        "ARCHITECTURE.md": "index.html defines the page; app.js handles the game interaction.\n",
    }
    provider = ScriptedProvider([
        LLMResponse(tool_calls=[stage], reasoning_content=thinking),
        LLMResponse(tool_calls=[call("list_files", path="."),
                                *[call("write_file", path=path, content=text) for path, text in files.items()]]),
        LLMResponse(tool_calls=[call("check_files", paths=["index.html", "app.js"]), call("preview_project", steps=[
            {"selector": "#play", "action": "click", "expect_text": "Ending A", "expect_selector": "#result"},
        ])]),
        LLMResponse(content="已完成并检查。"),
    ])
    runner, store = developer(workspace, provider)
    runner._registry.register(PreviewProjectTool())
    run = _run("在空目录创建一个网页游戏并测试，分离页面和脚本。")
    asyncio.run(runner.run(run))
    assert run.status is RunStatus.COMPLETED
    assert run.delivery_report.passed and len(run.delivery_report.files) == 4
    assert any(message.reasoning_content == thinking for message in store.load_run(run.id).messages)
    replayed = next(message for message in provider.inputs[1]
                    if any(action.id == stage.id for action in message.tool_calls))
    assert replayed.reasoning_content is None
    assert any(message.tool_call_id == stage.id for message in provider.inputs[1])
    assert any(event.type == "context.compacted" and event.data["reasoning_characters_omitted"] == len(thinking)
               for event in store.load_events(run.id))


def test_essential_tool_output_overflow_stops_for_review_and_preserves_file(workspace):
    content = "saved content\n" * 10_000
    provider = ScriptedProvider([
        LLMResponse(tool_calls=[call("list_files", path=".")]),
        LLMResponse(tool_calls=[call("write_file", path="app.py", content=content)]),
    ])
    runner, store = developer(workspace, provider)
    run = _run("修复程序 app.py。")
    final = asyncio.run(runner.run(run))
    assert run.status is RunStatus.NEEDS_REVIEW
    assert "上下文预算" in final and "ContextBudgetError:" not in final
    assert (workspace / "app.py").read_text() == content
    assert len(run.delivery_report.files) == 1
    assert len(provider.inputs) == 2  # No automatic repeat of the write or model generation.
    assert store.load_run(run.id).messages[-1].metadata["recoverable"] is True
    assert any(event.type == "context.budget_exceeded" for event in store.load_events(run.id))


@pytest.mark.parametrize("question", ["你现在做好了吗", "现在你的任务完成了吗", "目前这个项目做好了没有？"])
def test_status_after_failed_plan_only_inspects_and_keeps_original_work_unfinished(workspace, question):
    provider = ScriptedProvider([])
    runner, _ = developer(workspace, provider)
    run = _run("创建视觉小说游戏")
    from clearact.domain.models import ChatMessage
    run.messages.append(ChatMessage(role="user", content=question))
    run.workflow_plan = [WorkflowPlanItem(id="engine", title="实现游戏", summary="写入游戏文件。")]
    asyncio.run(runner.run(run))
    assert run.task_contract.mode == "discuss"
    assert run.status is RunStatus.NEEDS_REVIEW
    assert "尚未完成" in run.messages[-1].content
    assert "原任务" in run.stage_notes["completion_review"]
    assert not any(message.name == "write_file" for message in run.messages)
    assert provider.inputs == []
    assert run.completed_plan_item_ids == []


def test_status_rechecks_saved_hashes_and_never_reuses_a_models_completion_claim(workspace):
    from clearact.domain.models import ChatMessage

    provider = ScriptedProvider([
        LLMResponse(tool_calls=[call("write_file", path="app.py", content="print('hello')\n")]),
        LLMResponse(tool_calls=[call("check_files", paths=["app.py"])]),
        LLMResponse(content="全部完成。"),
    ])
    runner, _ = developer(workspace, provider)
    run = _run("创建 Python 项目 app.py。")
    asyncio.run(runner.run(run))
    assert run.status is RunStatus.COMPLETED
    (workspace / "app.py").write_text("External edit\n", encoding="utf-8")
    run.status = RunStatus.CREATED
    run.messages.append(ChatMessage(role="user", content="现在你的任务完成了吗"))
    final = asyncio.run(runner.run(run))
    assert run.status is RunStatus.NEEDS_REVIEW
    assert "写入后内容发生变化" in final
    assert "全部完成" not in final
    assert len(provider.inputs) == 3
    assert (workspace / "app.py").read_text() == "External edit\n"


def test_full_reasoning_mode_gracefully_keeps_the_original_trace(workspace):
    thinking = "x" * 100_000
    provider = ScriptedProvider([LLMResponse(tool_calls=[call(
        "declare_workflow_step", title="实现项目", summary="写入代码。", kind="analysis",
    )], reasoning_content=thinking)])
    runner, _ = developer(workspace, provider)
    runner._context_builder = ContextBuilder(ContextBudget(32_768, .7), reasoning_history="full")
    run = _run("创建代码项目")
    asyncio.run(runner.run(run))
    assert run.status is RunStatus.NEEDS_REVIEW
    assert len(provider.inputs) == 1
    assert any(message.reasoning_content == thinking for message in run.messages)
