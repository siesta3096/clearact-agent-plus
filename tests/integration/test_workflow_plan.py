import asyncio
from pathlib import Path

import pytest

from clearact.context.budget import ContextBudget
from clearact.context.builder import ContextBuilder
from clearact.domain.enums import RiskLevel, RunStatus
from clearact.domain.models import (
    Action,
    ChatMessage,
    LLMResponse,
    Run,
    ToolDefinition,
    ToolResult,
    UserPolicy,
    WorkflowStep,
)
from clearact.runtime.approvals import DenyAllApprovalGate
from clearact.runtime.event_bus import EventBus
from clearact.runtime.executor import ToolExecutor
from clearact.runtime.model_retry import ModelRequestError
from clearact.runtime.policy import PolicyEngine
from clearact.runtime.risk import RiskEvaluator
from clearact.runtime.runner import AgentRunner
from clearact.runtime.stage_mapper import StageMapper
from clearact.storage.checkpoint_store import CheckpointStore
from clearact.storage.run_store import RunStore
from clearact.tools.base import ToolContext
from clearact.tools.registry import ToolRegistry
from clearact.tools.workflow import DeclareWorkflowPlanTool, DeclareWorkflowStepTool, ReviseWorkflowPlanTool


class SearchTool:
    name = "web_search"

    def definition(self) -> ToolDefinition:
        return ToolDefinition(name=self.name, description="search", parameters={})

    async def execute(self, arguments, context, action_id) -> ToolResult:
        return ToolResult(action_id=action_id, tool_name=self.name, ok=True, content="https://example.test")


class ScriptedProvider:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = iter(responses)
        self.offered_tools: list[list[str]] = []
        self.seen_messages: list[list] = []

    async def chat(self, messages, tools) -> LLMResponse:
        self.offered_tools.append([tool.name for tool in tools])
        self.seen_messages.append(list(messages))
        return next(self.responses)


def build_runner(root: Path, provider: ScriptedProvider, *, max_iterations: int = 5) -> AgentRunner:
    registry = ToolRegistry()
    registry.register(DeclareWorkflowPlanTool())
    registry.register(DeclareWorkflowStepTool())
    registry.register(ReviseWorkflowPlanTool())
    registry.register(SearchTool())
    return AgentRunner(
        provider=provider,
        registry=registry,
        context_builder=ContextBuilder(ContextBudget(4096, 0.7)),
        risk_evaluator=RiskEvaluator(root, {}),
        policy_engine=PolicyEngine(),
        approval_gate=DenyAllApprovalGate(),
        executor=ToolExecutor(registry, 1),
        event_bus=EventBus(),
        run_store=RunStore(root / "data"),
        checkpoint_store=CheckpointStore(root / "data"),
        tool_context=ToolContext(root),
        stage_mapper=StageMapper({}),
        max_iterations=max_iterations,
        max_tool_calls=3,
    )


def test_plan_is_required_then_phase_is_revealed_when_work_begins(workspace):
    plan = Action(
        id="plan_call",
        tool_name="declare_workflow_plan",
        arguments={
            "summary": "先核验资料，再形成结论。",
            "steps": [
                {"id": "sources", "title": "核验资料", "summary": "查找可信来源。", "kind": "research"},
                {"id": "conclusion", "title": "形成结论", "summary": "综合证据。", "kind": "analysis"},
            ],
        },
    )
    phase = Action(
        id="step_call",
        tool_name="declare_workflow_step",
        arguments={
            "plan_item_id": "sources",
            "title": "核验资料",
            "summary": "查找可信来源。",
            "kind": "research",
        },
    )
    search = Action(id="search_call", tool_name="web_search", arguments={"query": "evidence"})
    conclusion = Action(
        id="conclusion_call",
        tool_name="declare_workflow_step",
        arguments={"plan_item_id": "conclusion", "title": "形成结论", "summary": "综合证据。", "kind": "analysis"},
    )
    provider = ScriptedProvider(
        [
            LLMResponse(tool_calls=[plan]),
            LLMResponse(tool_calls=[phase, search]),
            LLMResponse(content="done too early"),
            LLMResponse(tool_calls=[conclusion]),
            LLMResponse(content="done"),
        ]
    )

    async def execute():
        run = Run(goal="research", policy=UserPolicy(autonomy_threshold=RiskLevel.WHITE))
        final = await build_runner(workspace, provider).run(run)
        return run, final

    run, final = asyncio.run(execute())

    assert final == "done"
    assert run.status is RunStatus.COMPLETED
    assert provider.offered_tools[0] == ["declare_workflow_plan"]
    assert "web_search" in provider.offered_tools[1]
    assert [item.id for item in run.workflow_plan] == ["sources", "conclusion"]
    assert [step.title for step in run.workflow_steps] == ["理解与拆解任务", "核验资料", "形成结论"]
    assert run.workflow_steps[1].action_ids == ["search_call"]
    assert all(step.status == "completed" for step in run.workflow_steps)
    assert any(message.role == "system" and "Completion check" in (message.content or "")
               for message in provider.seen_messages[3])
    assert not any(message.role == "system" and "Completion check" in (message.content or "")
                   for message in run.messages)
    assert len(provider.offered_tools) == 5


def test_invalid_initial_plan_is_retried_instead_of_becoming_generic(workspace):
    invalid = Action(id="invalid_plan", tool_name="declare_workflow_plan", arguments={
        "steps": [{"id": "draft", "title": "", "summary": "没有标题。"}],
    })
    valid = Action(id="valid_plan", tool_name="declare_workflow_plan", arguments={
        "steps": [{"id": "draft", "title": "形成初稿", "summary": "整理已有信息。"}],
    })
    phase = Action(id="phase", tool_name="declare_workflow_step", arguments={
        "plan_item_id": "draft", "title": "形成初稿", "summary": "整理已有信息。",
    })
    provider = ScriptedProvider([
        LLMResponse(tool_calls=[invalid]),
        LLMResponse(tool_calls=[valid]),
        LLMResponse(tool_calls=[phase]),
        LLMResponse(content="初稿已形成。"),
    ])
    run = Run(goal="整理资料形成初稿")

    asyncio.run(build_runner(workspace, provider, max_iterations=4).run(run))

    assert provider.offered_tools[0] == ["declare_workflow_plan"]
    assert provider.offered_tools[1] == ["declare_workflow_plan"]
    assert [item.id for item in run.workflow_plan] == ["draft"]
    results = {message.tool_call_id: message for message in run.messages if message.role == "tool"}
    assert results["invalid_plan"].metadata["status"] == "failed"
    assert results["valid_plan"].metadata["status"] == "succeeded"


def test_repeated_invalid_plans_fail_early_with_clear_error(workspace):
    attempts = [Action(id=f"invalid-{index}", tool_name="declare_workflow_plan", arguments={
        "steps": [{"id": "draft", "title": "", "summary": "没有标题。"}],
    }) for index in range(3)]
    provider = ScriptedProvider([LLMResponse(tool_calls=[action]) for action in attempts])
    run = Run(goal="整理资料形成初稿")

    with pytest.raises(ModelRequestError, match="连续三次未能生成有效任务步骤"):
        asyncio.run(build_runner(workspace, provider, max_iterations=20).run(run))

    assert len(provider.offered_tools) == 3
    assert run.workflow_plan == []
    assert run.status is RunStatus.FAILED


def test_partially_invalid_plan_is_not_silently_truncated(workspace):
    runner = build_runner(workspace, ScriptedProvider([]))
    run = Run(goal="调查资料")
    partial = Action(tool_name="declare_workflow_plan", arguments={
        "steps": [
            {"id": "research", "title": "查找资料", "summary": "找来源。"},
            {"id": "deliver", "title": "", "summary": "形成结论。"},
        ],
    })

    assert not runner._record_workflow_plan(run, partial)
    assert run.workflow_plan == []
    null_title = Action(tool_name="declare_workflow_plan", arguments={
        "steps": [{"id": "draft", "title": None, "summary": "不能把空值变成标题。"}],
    })
    assert not runner._record_workflow_plan(run, null_title)
    assert run.workflow_plan == []

    valid = Action(tool_name="declare_workflow_plan", arguments={
        "steps": [
            {"id": "research", "title": "查找资料", "summary": "找来源。"},
            {"id": "deliver", "title": "形成结论", "summary": "整理材料。"},
        ],
    })
    assert runner._record_workflow_plan(run, valid)
    revision = Action(tool_name="revise_workflow_plan", arguments={
        "reason": "调整后续工作。",
        "steps": [
            {"id": "verify", "title": "核验来源", "summary": "检查可靠性。"},
            {"id": "deliver", "title": "", "summary": "形成结论。"},
        ],
    })
    assert runner._revise_workflow_plan(run, revision) is None
    assert [item.id for item in run.workflow_plan] == ["research", "deliver"]


def test_plan_can_replace_future_phases_without_losing_completed_work(workspace):
    plan = Action(id="plan", tool_name="declare_workflow_plan", arguments={
        "summary": "调查后交付。",
        "steps": [
            {"id": "gather", "title": "搜集依据", "summary": "找到资料。", "kind": "research"},
            {"id": "draft", "title": "直接起草", "summary": "撰写初稿。", "kind": "analysis"},
        ],
    })
    gather = Action(id="gather_step", tool_name="declare_workflow_step", arguments={
        "plan_item_id": "gather", "title": "搜集依据", "summary": "找到资料。",
    })
    search = Action(id="search", tool_name="web_search", arguments={"query": "evidence"})
    revise = Action(id="revise", tool_name="revise_workflow_plan", arguments={
        "reason": "资料显示需先核验来源。",
        "steps": [
            {"id": "verify", "title": "核验来源", "summary": "核对可靠性。", "kind": "research"},
            {"id": "deliver", "title": "整理交付", "summary": "形成结论。", "kind": "analysis"},
        ],
    })
    verify = Action(id="verify_step", tool_name="declare_workflow_step", arguments={
        "plan_item_id": "verify", "title": "核验来源", "summary": "核对可靠性。",
    })
    deliver = Action(id="deliver_step", tool_name="declare_workflow_step", arguments={
        "plan_item_id": "deliver", "title": "整理交付", "summary": "形成结论。",
    })
    provider = ScriptedProvider([
        LLMResponse(tool_calls=[plan]),
        LLMResponse(tool_calls=[gather, search]),
        LLMResponse(tool_calls=[revise]),
        LLMResponse(tool_calls=[verify]),
        LLMResponse(tool_calls=[deliver]),
        LLMResponse(content="已经完成核验与交付。"),
    ])
    runner = build_runner(workspace, provider, max_iterations=6)
    store = RunStore(workspace / "data")
    runner._event_bus.subscribe_sync(store.append_event)
    run = Run(goal="调查并交付结论", policy=UserPolicy(autonomy_threshold=RiskLevel.WHITE))

    assert asyncio.run(runner.run(run)) == "已经完成核验与交付。"
    assert [item.id for item in run.workflow_plan] == ["gather", "verify", "deliver"]
    assert [step.plan_item_id for step in run.workflow_steps[1:]] == ["gather", "verify", "deliver"]
    revision_result = next(message for message in run.messages if message.tool_call_id == revise.id)
    assert revision_result.metadata["status"] == "succeeded"
    assert revision_result.metadata["replaced"] == ["直接起草"]
    assert revision_result.metadata["reason"] == "资料显示需先核验来源。"
    assert any(event.type == "workflow.plan_revised" for event in store.load_events(run.id))


def test_new_evidence_can_extend_a_fully_reached_plan(workspace):
    plan = Action(id="plan", tool_name="declare_workflow_plan", arguments={
        "steps": [{"id": "inspect", "title": "检查资料", "summary": "读取已有来源。"}],
    })
    inspect = Action(id="inspect_step", tool_name="declare_workflow_step", arguments={
        "plan_item_id": "inspect", "title": "检查资料", "summary": "读取已有来源。",
    })
    search = Action(id="search", tool_name="web_search", arguments={"query": "new evidence"})
    extend = Action(id="extend", tool_name="revise_workflow_plan", arguments={
        "reason": "新证据需要单独核验。",
        "steps": [{"id": "verify", "title": "核验新证据", "summary": "交叉检查新来源。"}],
    })
    verify = Action(id="verify_step", tool_name="declare_workflow_step", arguments={
        "plan_item_id": "verify", "title": "核验新证据", "summary": "交叉检查新来源。",
    })
    provider = ScriptedProvider([
        LLMResponse(tool_calls=[plan]),
        LLMResponse(tool_calls=[inspect, search]),
        LLMResponse(tool_calls=[extend]),
        LLMResponse(tool_calls=[verify]),
        LLMResponse(content="已核验并完成。"),
    ])
    run = Run(goal="调查新证据", policy=UserPolicy(autonomy_threshold=RiskLevel.WHITE))

    asyncio.run(build_runner(workspace, provider, max_iterations=5).run(run))

    assert "revise_workflow_plan" in provider.offered_tools[2]
    assert [item.id for item in run.workflow_plan] == ["inspect", "verify"]
    assert [step.plan_item_id for step in run.workflow_steps[1:]] == ["inspect", "verify"]
    result = next(message for message in run.messages if message.tool_call_id == "extend")
    assert result.metadata["status"] == "succeeded"
    assert result.metadata["replaced"] == []


def test_repeated_plan_revision_waits_for_new_task_result(workspace):
    plan = Action(id="plan", tool_name="declare_workflow_plan", arguments={
        "steps": [{"id": "draft", "title": "起草", "summary": "形成初稿。"}],
    })
    first = Action(id="first", tool_name="revise_workflow_plan", arguments={
        "reason": "需要核验来源。",
        "steps": [{"id": "verify", "title": "核验来源", "summary": "检查资料。"}],
    })
    repeat = Action(id="repeat", tool_name="revise_workflow_plan", arguments={
        "reason": "再次改计划，但尚无新结果。",
        "steps": [{"id": "rewrite", "title": "重新起草", "summary": "重写内容。"}],
    })
    provider = ScriptedProvider([
        LLMResponse(tool_calls=[plan]),
        LLMResponse(tool_calls=[first]),
        LLMResponse(tool_calls=[repeat]),
        LLMResponse(content="尚未完成核验。"),
    ])
    run = Run(goal="调查资料", policy=UserPolicy(autonomy_threshold=RiskLevel.WHITE))

    asyncio.run(build_runner(workspace, provider, max_iterations=4).run(run))

    assert [item.id for item in run.workflow_plan] == ["verify"]
    assert "revise_workflow_plan" not in provider.offered_tools[2]
    results = {message.tool_call_id: message for message in run.messages if message.role == "tool"}
    assert results["first"].metadata["status"] == "succeeded"
    assert results["repeat"].metadata == {"status": "skipped", "reason": "no_new_result"}
    run.messages.append(ChatMessage(
        role="tool", name="web_search", content="New source",
        metadata={"status": "succeeded"},
    ))
    assert not AgentRunner._plan_revision_without_new_result(run)


def test_identical_future_plan_is_not_recorded_as_a_revision(workspace):
    runner = build_runner(workspace, ScriptedProvider([]))
    run = Run(goal="调查资料")
    original = Action(id="plan", tool_name="declare_workflow_plan", arguments={
        "steps": [{"id": "verify", "title": "核验来源", "summary": "检查资料。"}],
    })
    runner._record_workflow_plan(run, original)
    same = Action(id="same", tool_name="revise_workflow_plan", arguments={
        "reason": "措辞不同，但步骤没有变化。",
        "steps": [{"id": "verify", "title": "核验来源", "summary": "检查资料。"}],
    })

    assert runner._revise_workflow_plan(run, same) is None
    assert [item.id for item in run.workflow_plan] == ["verify"]


def test_existing_plan_survives_stale_initial_plan_call(workspace):
    stale = Action(id="stale", tool_name="declare_workflow_plan", arguments={
        "steps": [{"id": "replacement", "title": "覆盖计划", "summary": "不应替换原计划。"}],
    })
    provider = ScriptedProvider([
        LLMResponse(tool_calls=[stale]),
        LLMResponse(content="继续使用原计划。"),
    ])
    runner = build_runner(workspace, provider, max_iterations=2)
    run = Run(goal="调查资料", messages=[ChatMessage(role="user", content="继续调查")])
    assert runner._record_workflow_plan(run, Action(tool_name="declare_workflow_plan", arguments={
        "steps": [{"id": "verify", "title": "核验来源", "summary": "检查资料。"}],
    }))

    asyncio.run(runner.run(run))

    assert [item.id for item in run.workflow_plan] == ["verify"]
    assert all("declare_workflow_plan" not in offered for offered in provider.offered_tools)
    assert any(
        message.role == "system" and "A workflow plan already exists" in (message.content or "")
        for message in provider.seen_messages[0]
    )
    result = next(message for message in run.messages if message.tool_call_id == "stale")
    assert result.metadata == {"status": "skipped", "reason": "existing_plan"}


def test_redeclaring_active_phase_does_not_add_a_duplicate_card(workspace):
    plan = Action(id="plan", tool_name="declare_workflow_plan", arguments={
        "steps": [{"id": "research", "title": "核验资料", "summary": "读取并检查来源。"}],
    })
    phase = Action(id="phase", tool_name="declare_workflow_step", arguments={
        "plan_item_id": "research", "title": "核验资料", "summary": "读取并检查来源。",
    })
    repeat = Action(id="repeat", tool_name="declare_workflow_step", arguments=phase.arguments)
    search = Action(id="search", tool_name="web_search", arguments={"query": "evidence"})
    provider = ScriptedProvider([
        LLMResponse(tool_calls=[plan]),
        LLMResponse(tool_calls=[phase]),
        LLMResponse(tool_calls=[repeat]),
        LLMResponse(tool_calls=[search]),
        LLMResponse(content="核验完成。"),
    ])
    run = Run(goal="调查资料", policy=UserPolicy(autonomy_threshold=RiskLevel.WHITE))

    asyncio.run(build_runner(workspace, provider, max_iterations=5).run(run))

    assert [step.plan_item_id for step in run.workflow_steps] == [None, "research"]
    result = next(message for message in run.messages if message.tool_call_id == "repeat")
    assert result.metadata == {"status": "skipped", "reason": "duplicate_phase"}
    assert run.workflow_steps[-1].action_ids == ["search"]


def test_completed_phase_cannot_be_redeclared_in_same_user_turn(workspace):
    runner = build_runner(workspace, ScriptedProvider([]))
    run = Run(goal="调查资料", messages=[ChatMessage(role="user", content="调查资料")])
    runner._record_workflow_plan(run, Action(tool_name="declare_workflow_plan", arguments={
        "steps": [
            {"id": "gather", "title": "搜集资料", "summary": "收集来源。"},
            {"id": "draft", "title": "形成初稿", "summary": "汇总来源。"},
        ],
    }))
    gather = Action(tool_name="declare_workflow_step", arguments={
        "plan_item_id": "gather", "title": "搜集资料", "summary": "收集来源。",
    })
    draft = Action(tool_name="declare_workflow_step", arguments={
        "plan_item_id": "draft", "title": "形成初稿", "summary": "汇总来源。",
    })

    assert runner._activate_workflow_step(run, gather, 1) == "activated"
    assert runner._activate_workflow_step(run, draft, 2) == "activated"
    assert runner._activate_workflow_step(run, gather, 3) == "duplicate"
    assert [step.plan_item_id for step in run.workflow_steps] == ["gather", "draft"]
    assert run.workflow_steps[-1].status == "active"

    run.messages.extend(ChatMessage(role="assistant", content="progress") for _ in range(3))
    run.messages.append(ChatMessage(role="user", content="新一轮请重新搜集资料"))
    assert runner._activate_workflow_step(run, gather, len(run.messages)) == "activated"
    assert [step.plan_item_id for step in run.workflow_steps] == ["gather", "draft", "gather"]


def test_incomplete_phase_declaration_is_reported_as_invalid(workspace):
    plan = Action(id="plan", tool_name="declare_workflow_plan", arguments={
        "steps": [{"id": "research", "title": "核验资料", "summary": "读取并检查来源。"}],
    })
    invalid = Action(id="invalid", tool_name="declare_workflow_step", arguments={
        "plan_item_id": "research", "title": "核验资料", "summary": "",
    })
    provider = ScriptedProvider([
        LLMResponse(tool_calls=[plan]),
        LLMResponse(tool_calls=[invalid]),
        LLMResponse(content="尚未完成。"),
    ])
    run = Run(goal="调查资料")

    asyncio.run(build_runner(workspace, provider, max_iterations=3).run(run))

    result = next(message for message in run.messages if message.tool_call_id == "invalid")
    assert result.metadata == {"status": "failed", "reason": "invalid_phase"}
    assert "provide a non-empty title and summary" in result.content
    assert len(run.workflow_steps) == 1


def test_phase_title_recovers_missing_id_but_rejects_unknown_phase(workspace):
    runner = build_runner(workspace, ScriptedProvider([]))
    run = Run(goal="调查资料")
    runner._record_workflow_plan(run, Action(tool_name="declare_workflow_plan", arguments={
        "steps": [{"id": "sources", "title": "核验资料", "summary": "读取并检查来源。"}],
    }))
    matched = Action(tool_name="declare_workflow_step", arguments={
        "title": "核验资料", "summary": "读取并检查来源。",
    })
    duplicate = Action(tool_name="declare_workflow_step", arguments={
        "plan_item_id": "wrong-id", "title": "核验资料", "summary": "读取并检查来源。",
    })
    unknown = Action(tool_name="declare_workflow_step", arguments={
        "plan_item_id": "wrong-id", "title": "未知阶段", "summary": "计划里没有这个阶段。",
    })

    assert runner._activate_workflow_step(run, matched, 0) == "activated"
    assert run.workflow_steps[-1].plan_item_id == "sources"
    assert runner._activate_workflow_step(run, duplicate, 1) == "duplicate"
    assert runner._activate_workflow_step(run, unknown, 2) == "unknown_plan_item"
    null_step = Action(tool_name="declare_workflow_step", arguments={
        "plan_item_id": "sources", "title": None, "summary": "不能把空值变成标题。",
    })
    assert runner._activate_workflow_step(run, null_step, 3) == "invalid"
    assert len(run.workflow_steps) == 1

    ambiguous = Run(goal="调查资料")
    runner._record_workflow_plan(ambiguous, Action(tool_name="declare_workflow_plan", arguments={
        "steps": [
            {"id": "first", "title": "核验资料", "summary": "先核验一部分。"},
            {"id": "second", "title": "核验资料", "summary": "再核验另一部分。"},
        ],
    }))
    assert runner._activate_workflow_step(ambiguous, matched, 0) == "unknown_plan_item"
    assert ambiguous.workflow_steps == []

    ad_hoc = Run(goal="调查资料")
    unplanned = Action(tool_name="declare_workflow_step", arguments={
        "plan_item_id": None, "title": "补充核对", "summary": "核对新发现。",
    })
    assert runner._activate_workflow_step(ad_hoc, unplanned, 0) == "activated"
    assert ad_hoc.workflow_steps[-1].plan_item_id is None


def test_model_without_tool_calls_cannot_mark_unplanned_task_complete(workspace):
    provider = ScriptedProvider([LLMResponse(content="I will work on it.")])
    run = Run(goal="create a report")

    with pytest.raises(ModelRequestError, match="支持工具调用"):
        asyncio.run(build_runner(workspace, provider).run(run))

    assert run.status is RunStatus.FAILED


def test_simple_greeting_can_reply_without_forcing_a_tool_call(workspace):
    provider = ScriptedProvider([LLMResponse(content="你好！有什么可以帮你？", finish_reason="stop")])
    run = Run(goal="你好")

    final = asyncio.run(build_runner(workspace, provider).run(run))

    assert final == "你好！有什么可以帮你？"
    assert run.status is RunStatus.COMPLETED
    assert [step.title for step in run.workflow_steps] == ["直接回复"]
    assert [item.id for item in run.workflow_plan] == ["reply"]
    assert len(provider.offered_tools) == 1


def test_completion_check_only_inspects_files_written_since_latest_user_turn(workspace):
    missing_path = workspace / "missing.txt"
    run = Run(goal="save a file", messages=[
        ChatMessage(
            role="tool", name="write_file", content="Created",
            metadata={"status": "succeeded", "path": str(missing_path)},
        ),
        ChatMessage(role="user", content="new request"),
    ])
    assert AgentRunner._missing_written_files(run) == []
    run.messages.append(ChatMessage(
        role="tool", name="write_file", content="Created",
        metadata={"status": "succeeded", "path": str(missing_path)},
    ))
    assert AgentRunner._missing_written_files(run) == [str(missing_path)]


def test_phase_with_only_failed_actions_is_not_shown_as_completed():
    run = Run(goal="inspect a file", workflow_steps=[WorkflowStep(
        title="读取材料", summary="读取本地文件", status="active", action_ids=["read-1"],
    )], messages=[ChatMessage(
        role="tool", name="read_file", tool_call_id="read-1", content="read failed",
        metadata={"status": "failed"},
    )])
    AgentRunner._complete_active_step(run)
    assert run.workflow_steps[0].status == "needs_review"

    run.workflow_steps[0].status = "active"
    run.messages.append(ChatMessage(
        role="tool", name="read_file", tool_call_id="read-1", content="file contents",
        metadata={"status": "succeeded"},
    ))
    AgentRunner._complete_active_step(run)
    assert run.workflow_steps[0].status == "completed"


def test_empty_research_phase_needs_review_but_analysis_can_complete():
    run = Run(goal="调查资料", workflow_steps=[
        WorkflowStep(title="查找资料", summary="搜索来源", kind="research", status="active"),
    ])
    AgentRunner._complete_active_step(run)
    assert run.workflow_steps[0].status == "needs_review"

    run.workflow_steps.append(WorkflowStep(
        title="分析资料", summary="整理已有内容", kind="analysis", status="active",
    ))
    AgentRunner._complete_active_step(run)
    assert run.workflow_steps[-1].status == "completed"


def test_completion_check_gives_empty_research_phase_a_chance_to_run(workspace):
    plan = Action(id="plan", tool_name="declare_workflow_plan", arguments={
        "steps": [{"id": "research", "title": "查找资料", "summary": "检查来源。", "kind": "research"}],
    })
    phase = Action(id="phase", tool_name="declare_workflow_step", arguments={
        "plan_item_id": "research", "title": "查找资料", "summary": "检查来源。",
    })
    search = Action(id="search", tool_name="web_search", arguments={"query": "evidence"})
    provider = ScriptedProvider([
        LLMResponse(tool_calls=[plan]),
        LLMResponse(tool_calls=[phase]),
        LLMResponse(content="准备交付，但尚未检索。"),
        LLMResponse(tool_calls=[search]),
        LLMResponse(content="现在完成检索并交付。"),
    ])
    run = Run(goal="调查资料", policy=UserPolicy(autonomy_threshold=RiskLevel.WHITE))

    final = asyncio.run(build_runner(workspace, provider, max_iterations=5).run(run))

    assert final == "现在完成检索并交付。"
    assert any("Declared work phases with no actions: 查找资料" in (message.content or "")
               for message in provider.seen_messages[3])
    assert run.workflow_steps[-1].status == "completed"
    assert "completion_review" not in run.stage_notes
