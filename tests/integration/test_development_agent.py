"""Development controller regressions, using fixed responses and real filesystem/test tools."""

import asyncio
import sys

from test_task_scenarios import RecordingApprovalGate, ScriptedProvider, _run, _runner

from clearact.domain.enums import RunStatus
from clearact.domain.models import Action, ChatMessage, CompletionCheck, LLMResponse, WorkflowPlanItem
from clearact.tools.development import CheckFilesTool, EditFileTool, SearchFilesTool
from clearact.tools.filesystem import ListFilesTool


class DevelopmentProvider(ScriptedProvider):
    async def chat(self, messages, tools):
        if not self.inputs:
            assert {"list_files", "write_file", "edit_file", "check_files"} <= {tool.name for tool in tools}
        return await super().chat(messages, tools)


def developer(root, provider, granted=True):
    runner, store = _runner(root, provider, RecordingApprovalGate(granted))
    for tool in (ListFilesTool(), SearchFilesTool(), EditFileTool(), CheckFilesTool()):
        runner._registry.register(tool)
    runner._max_iterations = 20
    runner._max_tool_calls = 40
    return runner, store


def call(name, **arguments):
    return Action(tool_name=name, arguments=arguments)


def test_real_multifile_project_failing_test_edit_retest_and_delivery(workspace):
    files = {
        "calc.py": "def add(a, b):\n    return a - b\n",
        "app.py": "from calc import add\nprint(add(2, 3))\n",
        "test_calc.py": "from calc import add\nassert add(2, 3) == 5\n"
                        "assert add(-2, 2) == 0\nprint('2 checks passed')\n",
        "README.md": "Run: python app.py\nTest: python test_calc.py\n",
        "ARCHITECTURE.md": "app.py -> calc.py. test_calc.py checks the domain calculation.\n",
    }
    def test():
        return call("execute_command", command=[sys.executable, "test_calc.py"], timeout_seconds=5)

    first, second = test(), test()
    provider = DevelopmentProvider([
        LLMResponse(tool_calls=[call("list_files", path=".")]),
        LLMResponse(tool_calls=[call("declare_workflow_step", title="检查子目录", summary="检查候选目录。",
                                    kind="research"), call("list_files", path="missing")]),
        LLMResponse(tool_calls=[call("declare_workflow_step", title="实现项目",
                                    summary="写入模块与文档。", kind="files"),
                                *[call("write_file", path=path, content=content) for path, content in files.items()]]),
        LLMResponse(tool_calls=[first]),
        # Change size as well: CPython's same-second timestamp cache can reuse equal-size bytecode.
        LLMResponse(tool_calls=[call("edit_file", path="calc.py", old_text="a - b", new_text="a + b + 0")]),
        LLMResponse(tool_calls=[call("check_files", paths=["calc.py", "app.py", "test_calc.py"]), second]),
        LLMResponse(content="全部一次通过！已生成 imaginary.py。"),
    ])
    runner, store = developer(workspace, provider)
    run = _run("在空目录创建 Python 计算器项目，拆分模块，写好文件并测试。")
    run.workflow_plan = [WorkflowPlanItem(id="project", title="交付项目", summary="交付代码与说明。",
                                        checks=[CompletionCheck(type="file", path=path) for path in files])]
    asyncio.run(runner.run(run))
    saved = store.load_run(run.id)
    assert saved.status is RunStatus.COMPLETED
    assert saved.delivery_report.passed
    assert {item.path for item in saved.delivery_report.files} == set(files)
    assert saved.delivery_report.files[0].sha256
    results = {message.tool_call_id: message for message in saved.messages if message.role == "tool"}
    assert results[first.id].metadata["status"] == "failed"
    assert results[second.id].metadata["exit_code"] == 0
    assert "2 checks passed" in results[second.id].content
    assert (workspace / "calc.py").read_text() == "def add(a, b):\n    return a + b + 0\n"
    assert saved.rejected_responses == []
    assert "imaginary.py" not in saved.messages[-1].content
    assert "一次通过" not in saved.messages[-1].content
    assert "imaginary.py" in saved.messages[-1].metadata["model_draft"]
    assert any(step.status == "recovered" and step.resolution for step in saved.workflow_steps)
    assert "失败尝试" in saved.messages[-1].content
    assert saved.completed_plan_item_ids == ["project"]

    # Maintenance changes one file; a fulfilled original plan must not require rewriting all five.
    run.messages.append(ChatMessage(role="user", content="修改 app.py 的演示为 add(4, 5)，运行现有测试。"))
    run.status = RunStatus.CREATED
    runner._provider = DevelopmentProvider([
        LLMResponse(tool_calls=[call("read_file", path="app.py")]),
        LLMResponse(tool_calls=[call("edit_file", path="app.py", old_text="add(2, 3)", new_text="add(4, 5)")]),
        LLMResponse(tool_calls=[call("check_files", paths=["app.py"]), test()]),
        LLMResponse(content="更新了演示并重新测试。"),
    ])
    asyncio.run(runner.run(run))
    assert run.status is RunStatus.COMPLETED
    assert {item.path for item in run.delivery_report.files} == {"app.py"}
    assert len(run.delivery_history) == 1 and len(run.delivery_history[0].files) == 5


def test_actual_failure_followup_code_in_chat_cannot_complete_unfinished_project(workspace):
    provider = DevelopmentProvider([
        LLMResponse(content="工具不可用，请手动保存：```html\n<html>Game</html>\n```",
                    reasoning_content="returned trace"),
        LLMResponse(content="这是完整代码，请保存。"),
    ])
    runner, store = developer(workspace, provider)
    run = _run("这是空的文件夹，在里面创造一个视觉小说类型游戏。")
    run.workflow_plan = [WorkflowPlanItem(id="engine", title="编写游戏", summary="实现游戏并保存文件。")]
    run.messages.extend([ChatMessage(role="assistant", content="设计已完成，只有计划工具。"),
                         ChatMessage(role="user", content="那你去实现一下吧，中世纪地堡，幽默一点。")])
    asyncio.run(runner.run(run))
    saved = store.load_run(run.id)
    assert saved.status is RunStatus.NEEDS_REVIEW
    assert not saved.delivery_report.passed and saved.delivery_report.files == []
    assert len(saved.rejected_responses) == 2
    assert saved.rejected_responses[0].reasoning_content == "returned trace"
    assert not any(workspace.glob("*.html"))
    assert "请保存" not in saved.messages[-1].content
    assert "尚未完成交付" in saved.messages[-1].content
    assert "write_file" in " ".join(message.content or "" for message in provider.inputs[0])
    assert any(event.data.get("tool_names") and "write_file" in event.data["tool_names"]
               for event in store.load_events(run.id) if event.type == "model.waiting")


def test_discussion_does_not_force_pending_work_or_offer_writes(workspace):
    class DiscussionProvider(ScriptedProvider):
        async def chat(self, messages, tools):
            assert "write_file" not in {tool.name for tool in tools}
            return await super().chat(messages, tools)

    provider = DiscussionProvider([LLMResponse(content="当前只有计划，没有落盘文件。")])
    runner, _ = developer(workspace, provider)
    run = _run("在空目录创建一个游戏。")
    run.workflow_plan = [WorkflowPlanItem(id="engine", title="实现游戏", summary="编写游戏文件。")]
    run.messages.append(ChatMessage(role="user", content="解释为什么只给我代码，不要执行或写文件。"))
    asyncio.run(runner.run(run))
    assert run.status is RunStatus.COMPLETED
    assert run.task_contract.mode == "discuss"
    assert run.delivery_report is None


def test_checks_before_last_edit_do_not_verify_delivery(workspace):
    provider = DevelopmentProvider([
        LLMResponse(tool_calls=[call("list_files", path=".")]),
        LLMResponse(tool_calls=[call("write_file", path="app.py", content="print(1)\n")]),
        LLMResponse(tool_calls=[call("check_files", paths=["app.py"])]),
        LLMResponse(tool_calls=[call("edit_file", path="app.py", old_text="print(1)", new_text="print(2)")]),
        LLMResponse(content="完成。"), LLMResponse(content="完成。"),
    ])
    runner, _ = developer(workspace, provider)
    run = _run("修复 Python 程序 app.py。")
    asyncio.run(runner.run(run))
    assert run.status is RunStatus.NEEDS_REVIEW
    assert not next(check for check in run.delivery_report.checks if check.key == "verification").passed


def test_declined_test_keeps_written_project_but_does_not_claim_tested(workspace):
    provider = DevelopmentProvider([
        LLMResponse(tool_calls=[call("list_files", path=".")]),
        LLMResponse(tool_calls=[call("write_file", path="app.py", content="print(1)\n")]),
        LLMResponse(tool_calls=[call("check_files", paths=["app.py"]),
                                call("execute_command", command=[sys.executable, "-m", "unittest"])]),
        LLMResponse(content="语法通过，测试命令未获批准。"),
        LLMResponse(content="测试未执行。"),
    ])
    runner, _ = developer(workspace, provider, granted=False)
    run = _run("修复 Python 程序 app.py 并测试。")
    asyncio.run(runner.run(run))
    assert run.status is RunStatus.NEEDS_REVIEW
    assert (workspace / "app.py").exists()
    assert not next(check for check in run.delivery_report.checks if check.key == "tests").passed


def test_later_failed_test_invalidates_earlier_successful_syntax_check(workspace):
    provider = DevelopmentProvider([
        LLMResponse(tool_calls=[call("list_files", path=".")]),
        LLMResponse(tool_calls=[call("write_file", path="app.py", content="print(1)\n")]),
        LLMResponse(tool_calls=[call("check_files", paths=["app.py"])]),
        LLMResponse(tool_calls=[call("execute_command", command=[sys.executable, "-m", "unittest", "missing_test"])]),
        LLMResponse(content="完成。"), LLMResponse(content="完成。"),
    ])
    runner, _ = developer(workspace, provider)
    run = _run("修复 Python 程序 app.py。")
    asyncio.run(runner.run(run))
    assert run.status is RunStatus.NEEDS_REVIEW
    assert not next(check for check in run.delivery_report.checks if check.key == "verification").passed
