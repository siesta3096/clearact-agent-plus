import asyncio

import pytest

from clearact.domain.models import ChatMessage, Run, WorkflowPlanItem
from clearact.runtime.continuation import continues_unfinished_plan
from clearact.runtime.delivery import (
    archive_delivery,
    development_state,
    evaluate_delivery,
    infer_contract,
    is_direct_answer_request,
)
from clearact.tools.base import ToolContext
from clearact.tools.filesystem import WriteFileTool


@pytest.mark.parametrize(("text", "direct"), [
    ("只回复整数结果：17 乘以 23 等于多少？", True), ("17 * 23 = ?", True),
    ("1 + 2 等于多少？", True), ("不用工具，直接解释递归。", True),
    ("计算 17*23 并写入 result.txt", False), ("17*23的结果保存成文件", False),
    ("今天股票涨了多少？", False), ("读取 math.txt，计算17*23", False),
])
def test_direct_answer_routing_does_not_swallow_file_or_external_tasks(text, direct):
    assert is_direct_answer_request(text) is direct


def test_named_source_file_repair_is_development_without_keyword_code():
    goal = "修复 calc.py，保留 README.md 不改。"
    run = Run(goal=goal, messages=[ChatMessage(role="user", content=goal)])
    assert infer_contract(run).mode == "develop"
    assert infer_contract(run).expected_paths == ["calc.py"]


def test_source_reference_does_not_force_development_for_a_text_output():
    goal = "读取 calc.py，生成 summary.txt。"
    run = Run(goal=goal, messages=[ChatMessage(role="user", content=goal)])
    assert infer_contract(run).mode == "execute"
    assert infer_contract(run).expected_paths == ["summary.txt"]


@pytest.mark.parametrize(("latest", "mode"), [
    ("那你去实现一下吧，中世纪地堡，幽默一点。", "develop"),
    ("请按原始任务要求从所选阶段重试。先核对实际文件与工具记录，完成尚未交付的工作。", "develop"),
    ("解释每个模块的作用，不要修改文件。", "discuss"),
    ("解释为什么写文件失败。", "discuss"),
    ("为什么创建游戏失败了？", "discuss"),
    ("先解释问题，再修复代码。", "develop"),
    ("只运行现有测试，不要写文件。", "execute"),
])
def test_followup_intent_keeps_implementation_distinct_from_discussion(latest, mode):
    run = Run(goal="在空文件夹创造一个视觉小说游戏。", messages=[
        ChatMessage(role="user", content="在空文件夹创造一个视觉小说游戏。"),
        ChatMessage(role="assistant", content="只有计划工具，请手动保存代码。"),
        ChatMessage(role="user", content=latest),
    ])
    assert infer_contract(run).mode == mode


@pytest.mark.parametrize(("text", "expected"), [
    ("从这个阶段重试。", True), ("继续实现吧。", True),
    ("解释为什么失败，不要重试。", False), ("他说“继续实现”，这是什么意思？", False),
])
def test_pending_plan_audit_does_not_turn_explanations_into_execution(text, expected):
    run = Run(goal="Create game", workflow_plan=[WorkflowPlanItem(id="engine", title="引擎", summary="实现引擎。")],
              messages=[ChatMessage(role="user", content=text)])
    assert continues_unfinished_plan(run) is expected


def test_reading_reference_or_negated_file_does_not_require_writing_it():
    goal = "不要创建 new.py，修复 app.py，参考 config.json。"
    run = Run(goal=goal, messages=[ChatMessage(role="user", content=goal)])
    contract = infer_contract(run)
    assert contract.mode == "develop"
    assert contract.expected_paths == ["app.py"]
    assert not contract.project_creation


def test_development_preference_still_answers_explicit_explanation_without_forcing_edits():
    run = Run(goal="开发游戏", messages=[ChatMessage(role="user", content="解释为什么写文件失败，不要重新执行。")])
    run.execution.task_mode = "develop"
    assert infer_contract(run).mode == "discuss"


@pytest.mark.parametrize("question", ["你现在做好了吗", "完成了吗？", "游戏写好了吗", "Are you done yet?"])
def test_status_inquiry_does_not_restart_unfinished_implementation(question):
    run = Run(goal="在空文件夹创建视觉小说游戏", messages=[
        ChatMessage(role="user", content="在空文件夹创建视觉小说游戏"),
        ChatMessage(role="assistant", content="请把代码手动保存为 index.html。"),
        ChatMessage(role="user", content=question),
    ])
    assert infer_contract(run).mode == "discuss"


def test_status_question_with_explicit_continuation_still_allows_development():
    goal = "创建视觉小说游戏"
    run = Run(goal=goal, messages=[ChatMessage(role="user", content=goal),
                                   ChatMessage(role="user", content="做好了吗？没做好就继续实现。")])
    assert infer_contract(run).mode == "develop"


def test_delivery_preserves_user_changes_and_archive_remains_readable(workspace):
    goal = "修复 Python 程序 app.py。"
    run = Run(goal=goal, messages=[ChatMessage(role="user", content=goal)])
    run.task_contract = infer_contract(run)
    from clearact.domain.models import Action

    action = Action(tool_name="write_file", arguments={"path": "app.py", "content": "print(1)\n"})
    result = asyncio.run(WriteFileTool().execute(action.arguments, ToolContext(workspace), action.id))
    run.messages.extend([ChatMessage(role="assistant", tool_calls=[action]),
                         ChatMessage(role="tool", name="write_file", tool_call_id=action.id,
                                     metadata={**result.metadata, "status": "succeeded"})])
    (workspace / "app.py").write_text("# external edit\n", encoding="utf-8")
    run.delivery_report = evaluate_delivery(run, ToolContext(workspace))
    assert not run.delivery_report.passed
    assert run.delivery_report.files == []
    assert (workspace / "app.py").read_text() == "# external edit\n"
    archived = run.delivery_report.model_dump()
    archive_delivery(run)
    archive_delivery(run)
    assert run.delivery_report is None and len(run.delivery_history) == 1
    assert run.delivery_history[0].model_dump() == archived


def test_compaction_keeps_actual_project_paths_without_promoting_file_contents(workspace):
    from clearact.context.budget import ContextBudget
    from clearact.context.builder import ContextBuilder
    from clearact.domain.models import Action

    action = Action(tool_name="write_file", arguments={"path": "src/engine.js", "content": "PRIVATE FILE CONTENT"})
    run = Run(goal="继续修复项目", messages=[
        ChatMessage(role="user", content="创建网页项目"),
        ChatMessage(role="assistant", tool_calls=[action]),
        ChatMessage(role="tool", name="write_file", tool_call_id=action.id,
                    metadata={"status": "succeeded", "path": str(workspace / "src/engine.js")}),
        *[ChatMessage(role="assistant", content="older conversation" * 500) for _ in range(8)],
        ChatMessage(role="user", content="继续修复项目"),
    ])
    latest = Action(tool_name="list_files", arguments={"path": "."})
    run.messages.extend([ChatMessage(role="assistant", tool_calls=[latest]),
                         ChatMessage(role="tool", name="list_files", tool_call_id=latest.id, content="src")])
    state = development_state(run, workspace)
    messages, _ = ContextBuilder(ContextBudget(4000, .8)).build(
        [*run.messages, ChatMessage(role="system", content=state)], [], set(),
    )
    assert not any(message.tool_call_id == action.id for message in messages)
    assert "src/engine.js" in state and "PRIVATE FILE CONTENT" not in state
    assert any(message.content == state for message in messages)
