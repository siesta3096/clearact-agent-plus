import asyncio
from pathlib import Path

import httpx
import pytest

from clearact.context.budget import ContextBudget
from clearact.context.builder import ContextBuilder
from clearact.domain.enums import RiskLevel, RunStatus
from clearact.domain.models import Action, LLMResponse, RiskAssessment, Run, ToolDefinition, ToolResult, UserPolicy
from clearact.runtime.approvals import DenyAllApprovalGate
from clearact.runtime.event_bus import EventBus
from clearact.runtime.executor import ToolExecutor
from clearact.runtime.model_retry import ModelRequestError, ModelServiceUnavailableError
from clearact.runtime.policy import PolicyEngine
from clearact.runtime.risk import RiskEvaluator
from clearact.runtime.runner import AgentRunner
from clearact.runtime.stage_mapper import StageMapper
from clearact.storage.checkpoint_store import CheckpointStore
from clearact.storage.run_store import RunStore
from clearact.tools.base import ToolContext
from clearact.tools.filesystem import WriteFileTool
from clearact.tools.registry import ToolRegistry


class SearchTool:
    name = "web_search"

    def definition(self) -> ToolDefinition:
        return ToolDefinition(name=self.name, description="search", parameters={"type": "object"})

    async def execute(self, arguments: dict, _context: ToolContext, action_id: str) -> ToolResult:
        return ToolResult(action_id=action_id, tool_name=self.name, ok=True, content="Results for: test")


class ScriptedProvider:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = iter(responses)

    async def chat(self, messages, tools) -> LLMResponse:
        return next(self._responses)


class FlakyProvider:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = 0

    async def chat(self, messages, tools):
        self.calls += 1
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def build_runner(root: Path, provider: ScriptedProvider, *, max_tool_calls: int = 4) -> AgentRunner:
    registry = ToolRegistry()
    registry.register(WriteFileTool())
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
        max_iterations=4,
        max_tool_calls=max_tool_calls,
    )


def test_runner_preserves_assistant_tool_call_and_completes(workspace):
    async def execute():
        action = Action(id="call_123", tool_name="write_file", arguments={"path": "answer.txt", "content": "done"})
        runner = build_runner(
            workspace,
            ScriptedProvider([LLMResponse(tool_calls=[action]), LLMResponse(content="文件已经创建。")]),
        )
        run = Run(goal="create answer", policy=UserPolicy(autonomy_threshold=RiskLevel.GREEN))
        return action, run, await runner.run(run)

    action, run, final = asyncio.run(execute())

    assert final == "文件已经创建。"
    assert run.status is RunStatus.COMPLETED
    assert (workspace / "answer.txt").read_text(encoding="utf-8") == "done"
    assert run.messages[0].tool_calls == [action]
    assert run.messages[1].tool_call_id == "call_123"


def test_runner_retries_transient_model_disconnect_and_keeps_run_active(workspace):
    async def execute():
        request = httpx.Request("POST", "https://example.test/chat/completions")
        provider = FlakyProvider(
            [
                httpx.RemoteProtocolError("server disconnected", request=request),
                httpx.ReadTimeout("timed out", request=request),
                LLMResponse(content="recovered"),
            ]
        )
        registry = ToolRegistry()
        store = RunStore(workspace / "data")
        events = []
        bus = EventBus()
        bus.subscribe_sync(events.append)
        runner = AgentRunner(
            provider=provider,
            registry=registry,
            context_builder=ContextBuilder(ContextBudget(4096, 0.7)),
            risk_evaluator=RiskEvaluator(workspace, {}),
            policy_engine=PolicyEngine(),
            approval_gate=DenyAllApprovalGate(),
            executor=ToolExecutor(registry, 1),
            event_bus=bus,
            run_store=store,
            checkpoint_store=CheckpointStore(workspace / "data"),
            tool_context=ToolContext(workspace),
            stage_mapper=StageMapper({}),
            max_iterations=2,
            max_tool_calls=2,
            model_retry_attempts=3,
            retry_base_delay=0,
        )
        run = Run(goal="test")
        final = await runner.run(run)
        return provider, run, final, events

    provider, run, final, events = asyncio.run(execute())

    assert provider.calls == 3
    assert final == "recovered"
    assert run.status is RunStatus.COMPLETED
    assert [event.type for event in events].count("model.retrying") == 2
    assert any(event.type == "model.recovered" for event in events)


def test_runner_does_not_retry_deterministic_model_error(workspace):
    async def execute():
        provider = FlakyProvider([ValueError("invalid response")])
        runner = build_runner(workspace, provider)
        run = Run(goal="test")
        with pytest.raises(ValueError, match="invalid response"):
            await runner.run(run)
        return provider, run

    provider, run = asyncio.run(execute())

    assert provider.calls == 1
    assert run.status is RunStatus.FAILED


def test_runner_explains_unauthorized_model_response_without_retrying(workspace):
    async def execute():
        request = httpx.Request("POST", "http://localhost:1235/api/chat")
        response = httpx.Response(401, request=request)
        error = httpx.HTTPStatusError("unauthorized", request=request, response=response)
        provider = FlakyProvider([error])
        runner = build_runner(workspace, provider)
        run = Run(goal="test")
        with pytest.raises(ModelRequestError, match="请检查接口类型和 API Key"):
            await runner.run(run)
        return provider

    provider = asyncio.run(execute())

    assert provider.calls == 1


def test_exhausted_model_retries_preserve_completed_tool_result(workspace):
    async def execute():
        action = Action(id="call_once", tool_name="write_file", arguments={"path": "kept.txt", "content": "kept"})
        request = httpx.Request("POST", "https://example.test/chat/completions")
        disconnects = [
            httpx.RemoteProtocolError("server disconnected", request=request)
            for _ in range(3)
        ]
        provider = FlakyProvider([LLMResponse(tool_calls=[action]), *disconnects])
        registry = ToolRegistry()
        registry.register(WriteFileTool())
        runner = AgentRunner(
            provider=provider,
            registry=registry,
            context_builder=ContextBuilder(ContextBudget(4096, 0.7)),
            risk_evaluator=RiskEvaluator(workspace, {}),
            policy_engine=PolicyEngine(),
            approval_gate=DenyAllApprovalGate(),
            executor=ToolExecutor(registry, 1),
            event_bus=EventBus(),
            run_store=RunStore(workspace / "data"),
            checkpoint_store=CheckpointStore(workspace / "data"),
            tool_context=ToolContext(workspace),
            stage_mapper=StageMapper({}),
            max_iterations=3,
            max_tool_calls=3,
            model_retry_attempts=3,
            retry_base_delay=0,
        )
        run = Run(goal="write once", policy=UserPolicy(autonomy_threshold=RiskLevel.GREEN))
        with pytest.raises(ModelServiceUnavailableError, match="已自动尝试 3 次"):
            await runner.run(run)
        return provider, run

    provider, run = asyncio.run(execute())

    assert provider.calls == 4
    assert (workspace / "kept.txt").read_text(encoding="utf-8") == "kept"
    completed = [message for message in run.messages if message.tool_call_id == "call_once"]
    assert len(completed) == 1
    assert completed[0].metadata["status"] == "succeeded"
    assert run.status is RunStatus.FAILED


def test_runner_executes_every_visible_search_in_one_planning_turn(workspace):
    async def execute():
        searches = [
            Action(id=f"call_{index}", tool_name="web_search", arguments={"query": f"query {index}"})
            for index in range(3)
        ]
        registry = ToolRegistry()
        registry.register(SearchTool())
        runner = AgentRunner(
            provider=ScriptedProvider([LLMResponse(tool_calls=searches), LLMResponse(content="done")]),
            registry=registry,
            context_builder=ContextBuilder(ContextBudget(4096, 0.7)),
            risk_evaluator=RiskEvaluator(workspace, {}),
            policy_engine=PolicyEngine(),
            approval_gate=DenyAllApprovalGate(),
            executor=ToolExecutor(registry, 1),
            event_bus=EventBus(),
            run_store=RunStore(workspace / "data"),
            checkpoint_store=CheckpointStore(workspace / "data"),
            tool_context=ToolContext(workspace),
            stage_mapper=StageMapper({}),
            max_iterations=4,
            max_tool_calls=4,
        )
        run = Run(goal="write a report", policy=UserPolicy(autonomy_threshold=RiskLevel.GREEN))
        return run, await runner.run(run)

    run, final = asyncio.run(execute())
    assert final == "done"
    assistant_plan = next(message for message in run.messages if message.role == "assistant")
    assert [action.tool_name for action in assistant_plan.tool_calls] == ["web_search", "web_search", "web_search"]
    assert sum(message.name == "web_search" for message in run.messages if message.role == "tool") == 3


def test_runner_stops_at_tool_call_limit(workspace):
    async def execute():
        action = Action(tool_name="write_file", arguments={"path": "answer.txt", "content": "done"})
        runner = build_runner(workspace, ScriptedProvider([LLMResponse(tool_calls=[action])]), max_tool_calls=0)
        run = Run(goal="create answer")
        with pytest.raises(RuntimeError, match="tool-call limit"):
            await runner.run(run)
        return run

    run = asyncio.run(execute())

    assert run.status is RunStatus.FAILED
    assert not (workspace / "answer.txt").exists()


def test_tool_filter_keeps_focused_search_available_after_a_source_fetch():
    from clearact.context.tool_filter import ToolFilter

    tools = [
        ToolDefinition(name="web_search", description="search", parameters={}),
        ToolDefinition(name="fetch_url", description="fetch", parameters={}),
        ToolDefinition(name="write_file", description="write", parameters={}),
    ]

    allowed = ToolFilter().select(tools, {"fetch_url"})

    assert [tool.name for tool in allowed] == ["web_search", "fetch_url", "write_file"]


def test_failed_tool_result_is_not_checkpointed_or_reported_as_success(workspace):
    class FailingSearchTool:
        name = "web_search"

        def definition(self) -> ToolDefinition:
            return ToolDefinition(name=self.name, description="search", parameters={})

        async def execute(self, _arguments, _context, action_id) -> ToolResult:
            return ToolResult(
                action_id=action_id,
                tool_name=self.name,
                ok=False,
                content="The upstream search failed.",
                metadata={"status": "succeeded", "provider": "demo"},
            )

    async def execute():
        action = Action(id="call_failed", tool_name="web_search", arguments={"query": "test"})
        registry = ToolRegistry()
        registry.register(FailingSearchTool())
        event_bus = EventBus()
        events = []
        event_bus.subscribe_sync(events.append)
        runner = AgentRunner(
            provider=ScriptedProvider([LLMResponse(tool_calls=[action]), LLMResponse(content="done")]),
            registry=registry,
            context_builder=ContextBuilder(ContextBudget(4096, 0.7)),
            risk_evaluator=RiskEvaluator(workspace, {}),
            policy_engine=PolicyEngine(),
            approval_gate=DenyAllApprovalGate(),
            executor=ToolExecutor(registry, 1),
            event_bus=event_bus,
            run_store=RunStore(workspace / "data"),
            checkpoint_store=CheckpointStore(workspace / "data"),
            tool_context=ToolContext(workspace),
            stage_mapper=StageMapper({}),
            max_iterations=4,
            max_tool_calls=4,
        )
        run = Run(goal="search")
        final = await runner.run(run)
        return run, final, events

    run, final, events = asyncio.run(execute())

    assert final == "done"
    result = next(message for message in run.messages if message.role == "tool")
    assert result.metadata == {"status": "failed", "provider": "demo"}
    assert not (workspace / "data" / "checkpoints" / f"{run.id}.jsonl").exists()
    assert "action.failed" in [event.type for event in events]
    assert "action.completed" not in [event.type for event in events]


def test_cancellation_closes_every_action_in_the_visible_plan(workspace):
    class BlockingSearchTool:
        name = "web_search"

        def __init__(self) -> None:
            self.started = asyncio.Event()

        def definition(self) -> ToolDefinition:
            return ToolDefinition(name=self.name, description="search", parameters={})

        async def execute(self, _arguments, _context, _action_id) -> ToolResult:
            self.started.set()
            await asyncio.Event().wait()
            raise AssertionError("The cancelled tool must not complete.")

    async def execute():
        actions = [
            Action(id="call_cancel_one", tool_name="web_search", arguments={"query": "one"}),
            Action(id="call_cancel_two", tool_name="web_search", arguments={"query": "two"}),
        ]
        tool = BlockingSearchTool()
        registry = ToolRegistry()
        registry.register(tool)
        runner = AgentRunner(
            provider=ScriptedProvider([LLMResponse(tool_calls=actions)]),
            registry=registry,
            context_builder=ContextBuilder(ContextBudget(4096, 0.7)),
            risk_evaluator=RiskEvaluator(workspace, {}),
            policy_engine=PolicyEngine(),
            approval_gate=DenyAllApprovalGate(),
            executor=ToolExecutor(registry, 60),
            event_bus=EventBus(),
            run_store=RunStore(workspace / "data"),
            checkpoint_store=CheckpointStore(workspace / "data"),
            tool_context=ToolContext(workspace),
            stage_mapper=StageMapper({}),
            max_iterations=4,
            max_tool_calls=4,
        )
        run = Run(goal="search")
        task = asyncio.create_task(runner.run(run))
        await tool.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return run, registry

    run, registry = asyncio.run(execute())

    assert run.status is RunStatus.CANCELLED
    planned_ids = {action.id for message in run.messages for action in message.tool_calls}
    results = [message for message in run.messages if message.role == "tool"]
    assert {message.tool_call_id for message in results} == planned_ids
    assert all(message.metadata["status"] == "cancelled" for message in results)
    ContextBuilder(ContextBudget(4096, 0.7)).build(run.messages, registry.definitions(), set())


def test_tool_call_budget_closes_unstarted_actions_before_failing(workspace):
    class SearchTool:
        name = "web_search"

        def definition(self) -> ToolDefinition:
            return ToolDefinition(name=self.name, description="search", parameters={})

        async def execute(self, _arguments, _context, action_id) -> ToolResult:
            return ToolResult(action_id=action_id, tool_name=self.name, ok=True, content="first result")

    async def execute():
        actions = [
            Action(id="call_budget_one", tool_name="web_search", arguments={"query": "one"}),
            Action(id="call_budget_two", tool_name="web_search", arguments={"query": "two"}),
        ]
        registry = ToolRegistry()
        registry.register(SearchTool())
        runner = AgentRunner(
            provider=ScriptedProvider([LLMResponse(tool_calls=actions)]),
            registry=registry,
            context_builder=ContextBuilder(ContextBudget(4096, 0.7)),
            risk_evaluator=RiskEvaluator(workspace, {}),
            policy_engine=PolicyEngine(),
            approval_gate=DenyAllApprovalGate(),
            executor=ToolExecutor(registry, 1),
            event_bus=EventBus(),
            run_store=RunStore(workspace / "data"),
            checkpoint_store=CheckpointStore(workspace / "data"),
            tool_context=ToolContext(workspace),
            stage_mapper=StageMapper({}),
            max_iterations=4,
            max_tool_calls=1,
        )
        run = Run(goal="search")
        with pytest.raises(RuntimeError, match="tool-call limit"):
            await runner.run(run)
        return run, registry

    run, registry = asyncio.run(execute())

    assert run.status is RunStatus.FAILED
    results = {message.tool_call_id: message for message in run.messages if message.role == "tool"}
    assert results["call_budget_one"].metadata["status"] == "succeeded"
    assert results["call_budget_two"].metadata["status"] == "failed"
    ContextBuilder(ContextBudget(4096, 0.7)).build(run.messages, registry.definitions(), set())


def test_approval_gate_error_marks_waiting_run_failed_without_losing_the_action(workspace):
    class ApprovalRiskEvaluator:
        def assess(self, _action) -> RiskAssessment:
            return RiskAssessment(level=RiskLevel.YELLOW, reasons=["approval required"])

    class ExplodingApprovalGate:
        async def request(self, _action, _assessment) -> bool:
            raise RuntimeError("approval service unavailable")

    async def execute():
        action = Action(id="call_approval", tool_name="web_search", arguments={"query": "test"})
        registry = ToolRegistry()
        registry.register(SearchTool())
        runner = AgentRunner(
            provider=ScriptedProvider([LLMResponse(tool_calls=[action])]),
            registry=registry,
            context_builder=ContextBuilder(ContextBudget(4096, 0.7)),
            risk_evaluator=ApprovalRiskEvaluator(),
            policy_engine=PolicyEngine(),
            approval_gate=ExplodingApprovalGate(),
            executor=ToolExecutor(registry, 1),
            event_bus=EventBus(),
            run_store=RunStore(workspace / "data"),
            checkpoint_store=CheckpointStore(workspace / "data"),
            tool_context=ToolContext(workspace),
            stage_mapper=StageMapper({}),
            max_iterations=4,
            max_tool_calls=4,
        )
        run = Run(goal="search", policy=UserPolicy(autonomy_threshold=RiskLevel.GREEN))
        with pytest.raises(RuntimeError, match="approval service unavailable"):
            await runner.run(run)
        return run, registry

    run, registry = asyncio.run(execute())

    assert run.status is RunStatus.FAILED
    result = next(message for message in run.messages if message.role == "tool")
    assert result.tool_call_id == "call_approval"
    assert result.metadata["status"] == "failed"
    ContextBuilder(ContextBudget(4096, 0.7)).build(run.messages, registry.definitions(), set())
