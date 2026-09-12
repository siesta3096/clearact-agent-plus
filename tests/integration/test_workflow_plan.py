import asyncio
from pathlib import Path

from clearact.context.budget import ContextBudget
from clearact.context.builder import ContextBuilder
from clearact.domain.enums import RiskLevel, RunStatus
from clearact.domain.models import Action, LLMResponse, Run, ToolDefinition, ToolResult, UserPolicy
from clearact.runtime.approvals import DenyAllApprovalGate
from clearact.runtime.event_bus import EventBus
from clearact.runtime.executor import ToolExecutor
from clearact.runtime.policy import PolicyEngine
from clearact.runtime.risk import RiskEvaluator
from clearact.runtime.runner import AgentRunner
from clearact.runtime.stage_mapper import StageMapper
from clearact.storage.checkpoint_store import CheckpointStore
from clearact.storage.run_store import RunStore
from clearact.tools.base import ToolContext
from clearact.tools.registry import ToolRegistry
from clearact.tools.workflow import DeclareWorkflowPlanTool, DeclareWorkflowStepTool


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

    async def chat(self, messages, tools) -> LLMResponse:
        self.offered_tools.append([tool.name for tool in tools])
        return next(self.responses)


def build_runner(root: Path, provider: ScriptedProvider) -> AgentRunner:
    registry = ToolRegistry()
    registry.register(DeclareWorkflowPlanTool())
    registry.register(DeclareWorkflowStepTool())
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
        max_iterations=5,
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
    provider = ScriptedProvider(
        [LLMResponse(tool_calls=[plan]), LLMResponse(tool_calls=[phase, search]), LLMResponse(content="done")]
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
    assert [step.title for step in run.workflow_steps] == ["理解与拆解任务", "核验资料"]
    assert run.workflow_steps[-1].action_ids == ["search_call"]
    assert all(step.status == "completed" for step in run.workflow_steps)

