import asyncio
import sys

import pytest

from clearact.context.budget import ContextBudget
from clearact.context.builder import ContextBuilder
from clearact.domain.enums import DecisionOutcome, RiskLevel, RunStatus
from clearact.domain.errors import ToolValidationError
from clearact.domain.models import Action, ChatMessage, LLMResponse, Run, UserPolicy
from clearact.runtime.event_bus import EventBus
from clearact.runtime.executor import ToolExecutor
from clearact.runtime.policy import PolicyEngine
from clearact.runtime.risk import RiskEvaluator
from clearact.runtime.runner import AgentRunner
from clearact.runtime.stage_mapper import StageMapper
from clearact.storage.checkpoint_store import CheckpointStore
from clearact.storage.run_store import RunStore
from clearact.tools.base import ToolContext
from clearact.tools.command import ExecuteCommandTool
from clearact.tools.registry import ToolRegistry


class RecordingProvider:
    def __init__(self, actions):
        self.responses = iter([LLMResponse(tool_calls=actions), LLMResponse(content="Finished.")])
        self.offered_tools = []

    async def chat(self, _messages, tools):
        self.offered_tools.append({tool.name for tool in tools})
        return next(self.responses)


class RecordingApprovalGate:
    def __init__(self, grants, expected_markers):
        self.grants = iter(grants)
        self.expected_markers = expected_markers
        self.requests = []

    async def request(self, action, assessment):
        assert not self.expected_markers[action.id].exists(), "Command ran before approval"
        self.requests.append((action, assessment))
        return next(self.grants)


def command_registry():
    registry = ToolRegistry()
    registry.register(ExecuteCommandTool())
    return registry


def marker_action(filename, *, action_id="command_marker", **arguments):
    code = f"from pathlib import Path; Path({filename!r}).write_text('executed'); print('executed')"
    return Action(
        id=action_id, tool_name="execute_command", arguments={"command": [sys.executable, "-c", code], **arguments},
    )


def build_command_runner(workspace, provider, gate):
    registry = command_registry()
    return AgentRunner(
        provider=provider,
        registry=registry,
        context_builder=ContextBuilder(ContextBudget(100_000, 0.7)),
        risk_evaluator=RiskEvaluator(workspace, {}),
        policy_engine=PolicyEngine(),
        approval_gate=gate,
        executor=ToolExecutor(registry, 2),
        event_bus=EventBus(),
        run_store=RunStore(workspace / "data"),
        checkpoint_store=CheckpointStore(workspace / "data"),
        tool_context=ToolContext(workspace),
        stage_mapper=StageMapper({}),
        max_iterations=4,
        max_tool_calls=4,
    )


@pytest.mark.parametrize(("requested", "effective"), [(None, 0.8), (0.4, 0.4), (3, 0.8)])
def test_executor_reports_effective_command_budget_and_stops_late_side_effects(workspace, requested, effective):
    marker = workspace / "too-late.txt"
    arguments = {"command": [
        sys.executable, "-c",
        "import time; from pathlib import Path; print('started', flush=True); "
        "time.sleep(1.3); Path('too-late.txt').write_text('survived')",
    ]}
    if requested is not None:
        arguments["timeout_seconds"] = requested
    action = Action(tool_name="execute_command", arguments=arguments)
    before = dict(action.arguments)

    async def exercise():
        result = await ToolExecutor(command_registry(), 0.8).execute(action, ToolContext(workspace))
        await asyncio.sleep(1.4)
        return result

    result = asyncio.run(exercise())

    assert not result.ok
    assert result.metadata["timed_out"]
    assert result.metadata["timeout_seconds"] == effective
    assert result.metadata["exit_code"] is not None
    assert "started" in result.metadata["stdout"]
    assert not marker.exists()
    assert action.arguments == before


def test_executor_uses_normal_command_default_when_global_budget_is_larger(workspace):
    action = marker_action("approved.txt")

    result = asyncio.run(ToolExecutor(command_registry(), 500).execute(action, ToolContext(workspace)))

    assert result.ok
    assert result.metadata["timeout_seconds"] == 60
    assert (workspace / "approved.txt").read_text() == "executed"
    assert "timeout_seconds" not in action.arguments


@pytest.mark.parametrize("requested", [301, float("inf")], ids=["above_tool_limit", "infinite"])
def test_executor_rejects_invalid_budget_before_any_command_effect(workspace, requested):
    action = marker_action("invalid-ran.txt", timeout_seconds=requested)

    with pytest.raises(ToolValidationError, match="timeout_seconds"):
        asyncio.run(ToolExecutor(command_registry(), 1).execute(action, ToolContext(workspace)))

    assert not (workspace / "invalid-ran.txt").exists()
    assert action.arguments["timeout_seconds"] == requested


@pytest.mark.parametrize("autonomy", list(RiskLevel))
def test_command_allow_rule_and_any_autonomy_still_require_real_approval(workspace, autonomy):
    action = marker_action("unapproved.txt")
    policy = UserPolicy(autonomy_threshold=autonomy, capability_rules={"command_execute": "allow"})
    assessment = RiskEvaluator(workspace, {}).assess(action)
    decision = PolicyEngine().decide(assessment, policy, action.tool_name)
    provider = RecordingProvider([action])
    gate = RecordingApprovalGate([False], {action.id: workspace / "unapproved.txt"})
    run = Run(goal="Execute the authorized command", policy=policy)

    asyncio.run(build_command_runner(workspace, provider, gate).run(run))

    assert assessment.level is RiskLevel.RED
    assert assessment.hard_stop
    assert decision.outcome is DecisionOutcome.REQUIRE_APPROVAL
    assert len(gate.requests) == 1
    assert not (workspace / "unapproved.txt").exists()
    outcome = next(message for message in run.messages if message.role == "tool")
    assert outcome.metadata["status"] == "denied"
    assert run.status is RunStatus.COMPLETED


def test_runner_requires_a_separate_approval_for_each_command(workspace):
    approved = marker_action("approved.txt", action_id="command_approved")
    declined = marker_action("declined.txt", action_id="command_declined")
    provider = RecordingProvider([approved, declined])
    gate = RecordingApprovalGate(
        [True, False], {approved.id: workspace / "approved.txt", declined.id: workspace / "declined.txt"},
    )
    run = Run(
        goal="Run the two commands", policy=UserPolicy(
            autonomy_threshold=RiskLevel.RED, capability_rules={"command_execute": "allow"},
        ),
    )

    asyncio.run(build_command_runner(workspace, provider, gate).run(run))

    assert [action.id for action, _ in gate.requests] == [approved.id, declined.id]
    assert all(assessment.hard_stop for _, assessment in gate.requests)
    assert (workspace / "approved.txt").read_text() == "executed"
    assert not (workspace / "declined.txt").exists()
    outcomes = {message.tool_call_id: message for message in run.messages if message.role == "tool"}
    assert outcomes[approved.id].metadata["status"] == "succeeded"
    assert outcomes[declined.id].metadata["status"] == "denied"


@pytest.mark.parametrize("disabled", [
    "allow_read", "allow_write", "allow_web", "command_execute", "local_read", "web_read",
    "workspace_create", "workspace_modify", "outside_write", "destructive", "mcp_read", "mcp_write",
    "browser_interaction",
])
def test_disabled_capability_cannot_be_bypassed_by_command_even_with_an_approving_gate(workspace, disabled):
    action = marker_action("blocked.txt")
    policy = UserPolicy(autonomy_threshold=RiskLevel.RED, capability_rules={"command_execute": "allow"})
    if disabled.startswith("allow_"):
        setattr(policy, disabled, False)
    else:
        policy.capability_rules[disabled] = "deny"
    decision = PolicyEngine().decide(RiskEvaluator(workspace, {}).assess(action), policy, action.tool_name)
    provider = RecordingProvider([action])
    gate = RecordingApprovalGate([True], {action.id: workspace / "blocked.txt"})
    run = Run(goal="Run a command", policy=policy)

    asyncio.run(build_command_runner(workspace, provider, gate).run(run))

    assert decision.outcome is DecisionOutcome.DENY
    assert "execute_command" not in provider.offered_tools[0]
    assert gate.requests == []
    assert not (workspace / "blocked.txt").exists()
    outcome = next(message for message in run.messages if message.role == "tool")
    assert outcome.metadata["status"] == "denied"


@pytest.mark.parametrize("instruction", [
    "别联网，请只用现有文件整理结果。",
    "Do not use the internet. Summarize the local files.",
])
def test_runner_hides_and_blocks_commands_when_user_requires_offline_work(workspace, instruction):
    action = marker_action("offline-bypass.txt")
    provider = RecordingProvider([action])
    gate = RecordingApprovalGate([True], {action.id: workspace / "offline-bypass.txt"})
    run = Run(
        goal="联网调查最新资料", messages=[ChatMessage(role="user", content=instruction)],
        policy=UserPolicy(autonomy_threshold=RiskLevel.RED, capability_rules={"command_execute": "allow"}),
    )

    asyncio.run(build_command_runner(workspace, provider, gate).run(run))

    assert run.research_strategy.mode == "local_only"
    assert all("execute_command" not in offered for offered in provider.offered_tools)
    assert gate.requests == []
    assert not (workspace / "offline-bypass.txt").exists()
    outcome = next(message for message in run.messages if message.role == "tool")
    assert outcome.metadata["status"] == "skipped"
    assert outcome.metadata["reason"] == "local_only"
