import asyncio
from pathlib import Path

import httpx
import pytest

from clearact.context.budget import ContextBudget
from clearact.context.builder import ContextBuilder
from clearact.domain.enums import RiskLevel, RunStatus
from clearact.domain.errors import ToolValidationError
from clearact.domain.models import (
    Action,
    ChatMessage,
    LLMResponse,
    RiskAssessment,
    Run,
    ToolDefinition,
    ToolResult,
    UserPolicy,
    WorkflowRevision,
    WorkflowStep,
)
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
from clearact.tools.filesystem import ReadFileTool, WriteFileTool
from clearact.tools.registry import ToolRegistry


class SearchTool:
    name = "web_search"

    def definition(self) -> ToolDefinition:
        return ToolDefinition(name=self.name, description="search", parameters={"type": "object"})

    async def execute(self, arguments: dict, _context: ToolContext, action_id: str) -> ToolResult:
        return ToolResult(
            action_id=action_id,
            tool_name=self.name,
            ok=True,
            content="Results for: test",
            metadata={"query": arguments.get("query"), "count": 1},
        )


class FetchTool:
    name = "fetch_url"

    def definition(self) -> ToolDefinition:
        return ToolDefinition(name=self.name, description="fetch", parameters={"type": "object"})

    async def execute(self, arguments: dict, _context: ToolContext, action_id: str) -> ToolResult:
        return ToolResult(action_id=action_id, tool_name=self.name, ok=True, content="Source text")


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


def build_runner(
    root: Path, provider: ScriptedProvider, *, max_tool_calls: int = 4, context_window: int = 4096,
    event_bus: EventBus | None = None,
) -> AgentRunner:
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    registry.register(WriteFileTool())
    return AgentRunner(
        provider=provider,
        registry=registry,
        context_builder=ContextBuilder(ContextBudget(context_window, 0.7)),
        risk_evaluator=RiskEvaluator(root, {}),
        policy_engine=PolicyEngine(),
        approval_gate=DenyAllApprovalGate(),
        executor=ToolExecutor(registry, 1),
        event_bus=event_bus or EventBus(),
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


def test_repeated_reads_respect_ranges_and_file_changes(workspace):
    (workspace / "notes.txt").write_text("abcdef", encoding="utf-8")
    actions = [
        Action(id="first", tool_name="read_file", arguments={"path": "notes.txt", "max_chars": 2}),
        Action(id="repeat", tool_name="read_file", arguments={"path": "notes.txt", "max_chars": 2}),
        Action(id="next", tool_name="read_file", arguments={"path": "notes.txt", "offset": 2, "max_chars": 2}),
        Action(id="write", tool_name="write_file", arguments={"path": "notes.txt", "content": "UVWXYZ"}),
        Action(id="reread", tool_name="read_file", arguments={"path": "notes.txt", "max_chars": 2}),
    ]

    async def execute():
        runner = build_runner(
            workspace,
            ScriptedProvider([LLMResponse(tool_calls=actions), LLMResponse(content="done")]),
            max_tool_calls=5,
            context_window=8192,
        )
        run = Run(
            goal="update notes",
            policy=UserPolicy(autonomy_threshold=RiskLevel.RED),
            messages=[ChatMessage(role="user", content="update notes")],
        )
        return run, await runner.run(run)

    run, final = asyncio.run(execute())
    results = {message.tool_call_id: message for message in run.messages if message.role == "tool"}
    assert final == "done"
    assert results["first"].metadata["status"] == "succeeded"
    assert results["repeat"].metadata["status"] == "skipped"
    assert results["repeat"].metadata["reused_from_action_id"] == "first"
    assert "ab" in results["repeat"].content
    assert results["next"].content.startswith("cd")
    assert results["reread"].content.startswith("UV")


def test_file_changed_after_read_is_not_reused(workspace):
    source = workspace / "notes.txt"
    source.write_text("original", encoding="utf-8")

    class ChangingReadFileTool(ReadFileTool):
        changed = False

        async def execute(self, arguments, context, action_id):
            result = await super().execute(arguments, context, action_id)
            if not self.changed:
                source.write_text("updated content", encoding="utf-8")
                self.changed = True
            return result

    actions = [
        Action(id="first", tool_name="read_file", arguments={"path": "notes.txt"}),
        Action(id="second", tool_name="read_file", arguments={"path": "notes.txt"}),
    ]
    runner = build_runner(
        workspace,
        ScriptedProvider([LLMResponse(tool_calls=actions), LLMResponse(content="done")]),
        max_tool_calls=2,
    )
    runner._registry._tools["read_file"] = ChangingReadFileTool()
    run = Run(goal="read notes", policy=UserPolicy(autonomy_threshold=RiskLevel.RED))

    asyncio.run(runner.run(run))

    results = {message.tool_call_id: message for message in run.messages if message.role == "tool"}
    assert results["first"].metadata["status"] == "succeeded"
    assert results["first"].metadata["source_changed_around_read"] is True
    assert "read_only_signature" not in results["first"].metadata
    assert "File changed after this read" in results["first"].content
    assert results["second"].metadata["status"] == "succeeded"
    assert results["second"].content == "updated content"


def test_read_guard_uses_effective_character_limit(workspace):
    (workspace / "notes.txt").write_text("small file", encoding="utf-8")
    (workspace / "report.pdf").write_bytes(b"placeholder")
    first = Action(id="first", tool_name="read_file", arguments={"path": "notes.txt", "max_chars": 1_000_000})
    repeat = Action(id="repeat", tool_name="read_file", arguments={"path": "notes.txt", "max_chars": 50_000})
    runner = build_runner(workspace, ScriptedProvider([
        LLMResponse(tool_calls=[first, repeat]), LLMResponse(content="done"),
    ]), max_tool_calls=1)
    run = Run(goal="read notes", policy=UserPolicy(autonomy_threshold=RiskLevel.RED))

    asyncio.run(runner.run(run))

    results = {message.tool_call_id: message for message in run.messages if message.role == "tool"}
    assert results["first"].metadata["status"] == "succeeded"
    assert results["repeat"].metadata["status"] == "skipped"
    assert results["repeat"].metadata["reused_from_action_id"] == "first"
    assert runner._read_file_range(first)["end"] == 50_000
    large_pdf = Action(id="pdf-large", tool_name="read_pdf", arguments={"path": "report.pdf", "max_chars": 1_000_000})
    capped_pdf = Action(id="pdf-capped", tool_name="read_pdf", arguments={"path": "report.pdf", "max_chars": 50_000})
    assert runner._read_only_signature(large_pdf) == runner._read_only_signature(capped_pdf)


def test_skipped_and_denied_actions_have_auditable_events(workspace):
    (workspace / "notes.txt").write_text("evidence", encoding="utf-8")
    actions = [
        Action(id="read", tool_name="read_file", arguments={"path": "notes.txt"}),
        Action(id="repeat", tool_name="read_file", arguments={"path": "notes.txt"}),
        Action(id="write", tool_name="write_file", arguments={"path": "output.txt", "content": "text"}),
    ]
    events = []
    bus = EventBus()
    bus.subscribe_sync(events.append)
    run = Run(goal="inspect notes", policy=UserPolicy(
        autonomy_threshold=RiskLevel.RED, allow_write=False,
    ))
    runner = build_runner(workspace, ScriptedProvider([
        LLMResponse(tool_calls=actions), LLMResponse(content="Read completed; write was not authorized."),
    ]), event_bus=bus)

    asyncio.run(runner.run(run))

    decisions = {event.action_id: event for event in events if event.type in {"action.skipped", "action.denied"}}
    assert decisions["repeat"].type == "action.skipped"
    assert decisions["repeat"].risk == RiskLevel.WHITE
    assert decisions["repeat"].detail == "duplicate_read"
    assert decisions["write"].type == "action.denied"
    assert decisions["write"].risk == RiskLevel.GREEN
    assert "禁用" in decisions["write"].detail


@pytest.mark.parametrize("policy, expected_reason", [
    (UserPolicy(allow_read=False), "禁用"),
    (UserPolicy(capability_rules={"local_read": "ask"}), "user_declined"),
])
def test_cached_read_still_respects_current_policy(workspace, policy, expected_reason):
    source = workspace / "private.txt"
    source.write_text("previously approved content", encoding="utf-8")
    repeat = Action(id="repeat", tool_name="read_file", arguments={"path": "private.txt"})
    runner = build_runner(workspace, ScriptedProvider([
        LLMResponse(tool_calls=[repeat]), LLMResponse(content="No read was authorized."),
    ]))
    signature = runner._read_only_signature(repeat)
    run = Run(goal="inspect private file", policy=policy, messages=[
        ChatMessage(role="user", content="inspect private file"),
        ChatMessage(role="assistant", tool_calls=[Action(
            id="earlier", tool_name="read_file", arguments={"path": "private.txt"},
        )]),
        ChatMessage(role="tool", name="read_file", tool_call_id="earlier", content="previously approved content",
                    metadata={"status": "succeeded", "path": str(source), "read_only_signature": signature}),
    ])

    asyncio.run(runner.run(run))

    result = next(message for message in run.messages if message.tool_call_id == repeat.id)
    assert result.metadata["status"] == "denied"
    assert expected_reason in result.metadata["reason"]
    assert "previously approved content" not in result.content


def test_repeated_skipped_or_denied_rounds_stop_before_iteration_limit(workspace):
    actions = [Action(
        id=f"blocked-{index}", tool_name="write_file",
        arguments={"path": "blocked.txt", "content": "no"},
    ) for index in range(3)]
    provider = FlakyProvider([LLMResponse(tool_calls=[action]) for action in actions])
    runner = build_runner(workspace, provider)
    run = Run(goal="write a file", policy=UserPolicy(allow_write=False))

    with pytest.raises(ModelRequestError, match="连续三轮只提出被跳过或未获授权的操作"):
        asyncio.run(runner.run(run))

    assert provider.calls == 3
    assert run.status is RunStatus.FAILED
    assert [message.metadata["status"] for message in run.messages if message.role == "tool"] == [
        "denied", "denied", "denied",
    ]
    assert not (workspace / "blocked.txt").exists()


def test_rewind_keeps_reasoning_for_retained_action_only(workspace):
    (workspace / "notes.txt").write_text("evidence", encoding="utf-8")
    read = Action(id="read", tool_name="read_file", arguments={"path": "notes.txt"})
    write = Action(id="write", tool_name="write_file", arguments={
        "path": "answer.txt", "content": "done",
    })
    store = RunStore(workspace / "data")
    bus = EventBus()
    bus.subscribe_sync(store.append_event)
    runner = build_runner(workspace, ScriptedProvider([
        LLMResponse(tool_calls=[read], reasoning_content="Read the local evidence first."),
        LLMResponse(tool_calls=[write], reasoning_content="Save the answer."),
        LLMResponse(content="done"),
    ]), event_bus=bus)
    run = Run(goal="summarize notes", policy=UserPolicy(autonomy_threshold=RiskLevel.RED))

    asyncio.run(runner.run(run))

    reasoning_before = [event for event in store.load_events(run.id) if event.type == "model.reasoning"]
    assert [event.action_id for event in reasoning_before] == [read.id, write.id]
    store.prune_events_to_actions(run.id, {read.id})
    reasoning_after = [event for event in store.load_events(run.id) if event.type == "model.reasoning"]
    assert [event.detail for event in reasoning_after] == ["Read the local evidence first."]


def test_declined_approval_is_recorded_after_the_approval_request(workspace):
    action = Action(id="write", tool_name="write_file", arguments={"path": "output.txt", "content": "text"})
    events = []
    bus = EventBus()
    bus.subscribe_sync(events.append)
    run = Run(goal="write output", policy=UserPolicy(capability_rules={"workspace_create": "ask"}))
    runner = build_runner(workspace, ScriptedProvider([
        LLMResponse(tool_calls=[action]), LLMResponse(content="The write was declined."),
    ]), event_bus=bus)

    asyncio.run(runner.run(run))

    relevant = [event for event in events if event.action_id == "write"]
    assert [event.type for event in relevant] == ["approval.required", "action.denied"]
    assert relevant[-1].detail == "user_declined"
    assert not (workspace / "output.txt").exists()


def test_read_file_guard_reuses_a_range_already_covered_by_an_unchanged_read(workspace):
    (workspace / "notes.txt").write_text("abcdefghij", encoding="utf-8")
    actions = [
        Action(id="first", tool_name="read_file", arguments={"path": "notes.txt", "max_chars": 6}),
        Action(id="covered", tool_name="read_file", arguments={"path": "notes.txt", "offset": 2, "max_chars": 3}),
        Action(id="next", tool_name="read_file", arguments={"path": "notes.txt", "offset": 6, "max_chars": 4}),
    ]
    runner = build_runner(workspace, ScriptedProvider([LLMResponse(tool_calls=actions), LLMResponse(content="done")]))
    run = Run(goal="read notes", policy=UserPolicy(autonomy_threshold=RiskLevel.RED))

    asyncio.run(runner.run(run))

    results = {message.tool_call_id: message for message in run.messages if message.role == "tool"}
    assert results["covered"].metadata["status"] == "skipped"
    assert results["covered"].metadata["reason"] == "covered_read"
    assert results["covered"].metadata["reused_from_action_id"] == "first"
    assert results["covered"].content.endswith("cde")
    assert results["next"].metadata["status"] == "succeeded"


def test_repeat_read_does_not_duplicate_content_already_in_model_context(workspace):
    (workspace / "notes.txt").write_text("abcdef", encoding="utf-8")
    first = Action(id="first", tool_name="read_file", arguments={"path": "notes.txt"})
    repeat = Action(id="repeat", tool_name="read_file", arguments={"path": "notes.txt"})
    provider = ScriptedProvider([
        LLMResponse(tool_calls=[first]),
        LLMResponse(tool_calls=[repeat]),
        LLMResponse(content="done"),
    ])
    run = Run(goal="read notes", policy=UserPolicy(autonomy_threshold=RiskLevel.RED))

    asyncio.run(build_runner(workspace, provider, context_window=8192).run(run))

    result = next(message for message in run.messages if message.tool_call_id == "repeat")
    assert result.metadata["status"] == "skipped"
    assert result.metadata["reused_from_action_id"] == "first"
    assert "abcdef" not in result.content


def test_read_beyond_eof_is_not_mistaken_for_a_covered_range(workspace):
    (workspace / "notes.txt").write_text("abcdefghij", encoding="utf-8")
    actions = [
        Action(id="first", tool_name="read_file", arguments={"path": "notes.txt", "max_chars": 20}),
        Action(id="past_end", tool_name="read_file", arguments={"path": "notes.txt", "offset": 12, "max_chars": 2}),
    ]
    run = Run(goal="read notes", policy=UserPolicy(autonomy_threshold=RiskLevel.RED))
    runner = build_runner(workspace, ScriptedProvider([
        LLMResponse(tool_calls=actions), LLMResponse(content="The requested offset is outside the file."),
    ]))

    asyncio.run(runner.run(run))

    result = next(message for message in run.messages if message.tool_call_id == "past_end")
    assert result.metadata["status"] == "failed"
    assert "offset exceeds" in result.content


def test_search_guard_pauses_after_three_searches_until_a_source_is_opened(workspace):
    registry = ToolRegistry()
    registry.register(SearchTool())
    registry.register(FetchTool())
    actions = [
        Action(id="s1", tool_name="web_search", arguments={"query": "first topic"}),
        Action(id="s2", tool_name="web_search", arguments={"query": "second topic"}),
        Action(id="s3", tool_name="web_search", arguments={"query": "third topic"}),
        Action(id="s4", tool_name="web_search", arguments={"query": "fourth topic"}),
        Action(id="s1_repeat", tool_name="web_search", arguments={"query": "first topic"}),
        Action(id="page", tool_name="fetch_url", arguments={"url": "https://example.test/source"}),
        Action(id="s5", tool_name="web_search", arguments={"query": "fifth topic"}),
    ]
    runner = AgentRunner(
        provider=ScriptedProvider([LLMResponse(tool_calls=actions), LLMResponse(content="done")]),
        registry=registry,
        context_builder=ContextBuilder(ContextBudget(8192, 0.7)),
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
        max_tool_calls=7,
    )
    run = Run(goal="summarize", policy=UserPolicy(autonomy_threshold=RiskLevel.RED))

    asyncio.run(runner.run(run))

    results = {message.tool_call_id: message for message in run.messages if message.role == "tool"}
    assert results["s4"].metadata["reason"] == "search_streak"
    assert results["s1_repeat"].metadata["reason"] == "duplicate_read"
    assert results["s1_repeat"].metadata["reused_from_action_id"] == "s1"
    assert "Results for: test" in results["s1_repeat"].content
    assert results["page"].metadata["status"] == "succeeded"
    assert results["s5"].metadata["status"] == "succeeded"


def test_search_tool_is_restored_after_opening_a_source(workspace):
    class RecordingProvider(ScriptedProvider):
        def __init__(self, responses):
            super().__init__(responses)
            self.offered_tools = []

        async def chat(self, messages, tools):
            self.offered_tools.append([tool.name for tool in tools])
            return await super().chat(messages, tools)

    registry = ToolRegistry()
    registry.register(SearchTool())
    registry.register(FetchTool())
    provider = RecordingProvider(
        [
            LLMResponse(tool_calls=[
                Action(id=f"search_{number}", tool_name="web_search", arguments={"query": f"topic {number}"})
                for number in range(3)
            ]),
            LLMResponse(tool_calls=[
                Action(id="source", tool_name="fetch_url", arguments={"url": "https://example.test/source"})
            ]),
            LLMResponse(content="done"),
        ]
    )
    runner = AgentRunner(
        provider=provider,
        registry=registry,
        context_builder=ContextBuilder(ContextBudget(8192, 0.7)),
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
        max_tool_calls=4,
    )

    asyncio.run(runner.run(Run(goal="summarize", policy=UserPolicy(autonomy_threshold=RiskLevel.RED))))

    assert "web_search" in provider.offered_tools[0]
    assert "web_search" not in provider.offered_tools[1]
    assert "fetch_url" in provider.offered_tools[1]
    assert "web_search" in provider.offered_tools[2]


def test_disabled_capabilities_are_not_offered_to_the_model(workspace):
    class RecordingProvider(ScriptedProvider):
        def __init__(self):
            super().__init__([LLMResponse(content="done")])
            self.offered_tools = []
            self.permission_notes = []

        async def chat(self, messages, tools):
            self.offered_tools.append([tool.name for tool in tools])
            self.permission_notes.append([message.content for message in messages if message.role == "system"])
            return await super().chat(messages, tools)

    class ExternalTool:
        name = "mcp__demo__read"

        def definition(self):
            return ToolDefinition(name=self.name, description="external", parameters={})

    provider = RecordingProvider()
    runner = build_runner(workspace, provider)
    runner._registry.register(SearchTool())
    runner._registry.register(FetchTool())
    runner._registry.register(ExternalTool())
    run = Run(goal="reply", policy=UserPolicy(allow_read=False, allow_write=False, allow_web=False))

    asyncio.run(runner.run(run))

    assert provider.offered_tools == [[]]
    assert "Local file reading is disabled" in " ".join(provider.permission_notes[0])
    assert "Web search and URL fetching are disabled" in " ".join(provider.permission_notes[0])
    assert run.status is RunStatus.COMPLETED


def test_denied_read_capabilities_are_hidden_and_explained(workspace):
    class RecordingProvider(ScriptedProvider):
        def __init__(self):
            super().__init__([LLMResponse(content="I cannot inspect the file with current permissions.")])
            self.tools = []
            self.notes = []

        async def chat(self, messages, tools):
            self.tools = [tool.name for tool in tools]
            self.notes = [message.content for message in messages if message.role == "system"]
            return await super().chat(messages, tools)

    provider = RecordingProvider()
    runner = build_runner(workspace, provider)
    runner._registry.register(SearchTool())
    runner._registry.register(FetchTool())
    run = Run(goal="inspect the file", policy=UserPolicy(capability_rules={
        "local_read": "deny", "web_read": "deny",
    }))

    asyncio.run(runner.run(run))

    assert provider.tools == ["write_file"]
    assert "Local file reading is disabled" in " ".join(provider.notes)
    assert "Web search and URL fetching are disabled" in " ".join(provider.notes)


def test_near_identical_search_is_reused_but_changed_year_is_not(workspace):
    registry = ToolRegistry()
    registry.register(SearchTool())
    actions = [
        Action(id="first", tool_name="web_search", arguments={"query": "competition application guidelines for 2025"}),
        Action(id="similar", tool_name="web_search", arguments={"query": "competition application guideline for 2025"}),
        Action(
            id="new_year",
            tool_name="web_search",
            arguments={"query": "competition application guidelines for 2026"},
        ),
    ]
    runner = AgentRunner(
        provider=ScriptedProvider([LLMResponse(tool_calls=actions), LLMResponse(content="done")]),
        registry=registry,
        context_builder=ContextBuilder(ContextBudget(8192, 0.7)),
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
    )
    run = Run(goal="research", policy=UserPolicy(autonomy_threshold=RiskLevel.RED))

    asyncio.run(runner.run(run))

    results = {message.tool_call_id: message for message in run.messages if message.role == "tool"}
    assert results["similar"].metadata["reason"] == "similar_search"
    assert results["similar"].metadata["reused_from_action_id"] == "first"
    assert "Results for: test" in results["similar"].content
    assert results["new_year"].metadata["status"] == "succeeded"


def test_local_attachment_is_read_before_web_and_has_small_search_budget(workspace):
    (workspace / "rules.txt").write_text("Local rules", encoding="utf-8")
    (workspace / "other.txt").write_text("Unrelated notes", encoding="utf-8")
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    registry.register(SearchTool())
    actions = [
        Action(id="premature", tool_name="web_search", arguments={"query": "external ideas"}),
        Action(id="unrelated", tool_name="read_file", arguments={"path": "other.txt"}),
        Action(id="still_premature", tool_name="web_search", arguments={"query": "more external ideas"}),
        Action(id="read", tool_name="read_file", arguments={"path": "rules.txt"}),
        Action(id="first", tool_name="web_search", arguments={"query": "idea one"}),
        Action(id="second", tool_name="web_search", arguments={"query": "idea two"}),
        Action(id="excess", tool_name="web_search", arguments={"query": "idea three"}),
    ]
    runner = AgentRunner(
        provider=ScriptedProvider([LLMResponse(tool_calls=actions), LLMResponse(content="done")]),
        registry=registry,
        context_builder=ContextBuilder(ContextBudget(8192, 0.7)),
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
        max_tool_calls=7,
    )
    run = Run(
        goal="根据附件整理材料",
        policy=UserPolicy(autonomy_threshold=RiskLevel.RED),
        messages=[ChatMessage(
            role="user",
            content="根据附件整理材料",
            metadata={"attachments": [{
                "kind": "file", "path": "rules.txt", "storage_path": str(workspace / "rules.txt"),
            }]},
        )],
    )

    asyncio.run(runner.run(run))

    results = {message.tool_call_id: message for message in run.messages if message.role == "tool"}
    assert run.research_strategy.mode == "local_first"
    assert results["premature"].metadata["reason"] == "local_first"
    assert results["unrelated"].metadata["status"] == "succeeded"
    assert results["still_premature"].metadata["reason"] == "local_first"
    assert results["read"].metadata["status"] == "succeeded"
    assert results["first"].metadata["status"] == "succeeded"
    assert results["second"].metadata["status"] == "succeeded"
    assert results["excess"].metadata["reason"] == "search_budget"


def test_browser_tool_respects_offline_and_local_first_modes(workspace):
    runner = build_runner(workspace, ScriptedProvider([]))
    runner._tool_context.computer_use_available = lambda _run_id: True
    browse = Action(tool_name="computer_use", arguments={"action": "navigate", "url": "https://example.test"})

    offline = Run(goal="只用本地文件，不要联网")
    assert not runner._web_tool_offered(offline, "computer_use")
    assert runner._guard_read_only_action(offline, browse)[1] == "local_only"

    attached = Run(
        goal="根据附件整理材料",
        messages=[ChatMessage(
            role="user", content="根据附件整理材料",
            metadata={"attachments": [{"kind": "file", "path": "rules.txt"}]},
        )],
    )
    assert not runner._web_tool_offered(attached, "computer_use")
    assert runner._guard_read_only_action(attached, browse)[1] == "local_first"

    research = Run(goal="调查最新公开资料")
    assert runner._web_tool_offered(research, "computer_use")


def test_workspace_source_is_read_before_optional_web_search(workspace):
    runner = build_runner(workspace, ScriptedProvider([]))
    prompt = "先查看工作区文件，再搜索网页核实缺失信息"
    run = Run(goal=prompt, messages=[ChatMessage(role="user", content=prompt)])
    search = Action(tool_name="web_search", arguments={"query": "missing facts"})

    assert not runner._web_tool_offered(run, "web_search")
    assert runner._guard_read_only_action(run, search)[1] == "local_first"
    run.messages.append(ChatMessage(role="tool", name="list_files", content="files", metadata={
        "status": "succeeded", "path": str(workspace),
    }))
    assert not runner._web_tool_offered(run, "web_search")
    run.messages.append(ChatMessage(role="tool", name="read_file", content="source", metadata={
        "status": "succeeded", "path": str(workspace / "source.txt"),
    }))
    assert runner._web_tool_offered(run, "web_search")


def test_folder_task_waits_for_a_file_read_before_offering_web_tools(workspace):
    runner = build_runner(workspace, ScriptedProvider([]))
    prompt = "请处理这个文件夹中的文件，整理一份报告"
    run = Run(goal=prompt, messages=[ChatMessage(role="user", content=prompt)])

    assert not runner._web_tool_offered(run, "web_search")
    run.messages.append(ChatMessage(role="tool", name="list_files", metadata={
        "status": "succeeded", "path": str(workspace),
    }))
    assert not runner._web_tool_offered(run, "web_search")
    run.messages.append(ChatMessage(role="tool", name="read_file", metadata={
        "status": "succeeded", "path": str(workspace / "source.txt"),
    }))
    assert runner._web_tool_offered(run, "web_search")


def test_write_only_invalidates_listing_of_its_own_directory(workspace):
    edited_dir = workspace / "edited"
    untouched_dir = workspace / "untouched"
    edited_dir.mkdir()
    untouched_dir.mkdir()
    runner = build_runner(workspace, ScriptedProvider([]))
    edited_listing = runner._read_only_signature(Action(tool_name="list_files", arguments={"path": "edited"}))
    untouched_listing = runner._read_only_signature(Action(tool_name="list_files", arguments={"path": "untouched"}))
    run = Run(goal="整理工作区", messages=[
        ChatMessage(role="user", content="整理工作区"),
        ChatMessage(role="tool", name="list_files", metadata={
            "status": "succeeded", "read_only_signature": edited_listing,
        }),
        ChatMessage(role="tool", name="list_files", metadata={
            "status": "succeeded", "read_only_signature": untouched_listing,
        }),
        ChatMessage(role="tool", name="write_file", metadata={
            "status": "succeeded", "path": str(edited_dir / "note.txt"),
        }),
    ])

    assert runner._completed_read_only_signatures(run) == {untouched_listing}


def test_followup_reuses_workspace_read_only_while_file_is_unchanged(workspace):
    source = workspace / "source.txt"
    source.write_text("Original source", encoding="utf-8")
    runner = build_runner(workspace, ScriptedProvider([]))
    signature = runner._read_only_signature(Action(tool_name="read_file", arguments={"path": "source.txt"}))
    run = Run(goal="查看工作区文件", messages=[
        ChatMessage(role="user", content="查看工作区文件"),
        ChatMessage(role="tool", name="read_file", content="Original source", metadata={
            "status": "succeeded", "path": str(source), "read_only_signature": signature,
        }),
        ChatMessage(role="user", content="根据工作区文件搜索网页核实信息"),
    ])

    assert runner._web_tool_offered(run, "web_search")
    source.write_text("Updated source with more content", encoding="utf-8")
    assert not runner._web_tool_offered(run, "web_search")


def test_rewind_reuses_only_unchanged_reads_from_retained_steps(workspace):
    source = workspace / "source.txt"
    source.write_text("Original source", encoding="utf-8")
    runner = build_runner(workspace, ScriptedProvider([]))
    reread = Action(tool_name="read_file", arguments={"path": "source.txt"})
    signature = runner._read_only_signature(reread)
    run = Run(
        goal="读取本地文件",
        workflow_steps=[WorkflowStep(id="retained", title="读取文件", summary="读取来源", action_ids=["old-read"])],
        workflow_revisions=[WorkflowRevision(
            id="revision-1", from_step_id="redo", feedback="调整方向", reused_step_ids=["retained"],
        )],
        messages=[
            ChatMessage(role="user", content="读取本地文件"),
            ChatMessage(role="tool", name="read_file", tool_call_id="old-read", content="Original source", metadata={
                "status": "succeeded", "path": str(source), "read_only_signature": signature,
            }),
            ChatMessage(role="user", content="调整方向", metadata={"rewind_revision_id": "revision-1"}),
        ],
    )

    assert runner._guard_read_only_action(run, reread)[1] == "duplicate_read"
    assert runner._successful_result_for_signature(run, signature).tool_call_id == "old-read"
    ordinary_followup = run.model_copy(deep=True)
    ordinary_followup.messages[-1].metadata.clear()
    assert runner._guard_read_only_action(ordinary_followup, reread) is None
    explicit_reread = run.model_copy(deep=True)
    explicit_reread.messages[-1].content = "请重新读取这个文件，再根据反馈继续"
    assert runner._guard_read_only_action(explicit_reread, reread) is None
    source.write_text("Changed source with additional text", encoding="utf-8")
    assert runner._guard_read_only_action(run, reread) is None


def test_rewind_reuses_a_covered_range_from_a_retained_read(workspace):
    source = workspace / "source.txt"
    source.write_text("abcdefghij", encoding="utf-8")
    runner = build_runner(workspace, ScriptedProvider([]))
    original = Action(id="old-read", tool_name="read_file", arguments={
        "path": "source.txt", "max_chars": 6,
    })
    subset = Action(tool_name="read_file", arguments={
        "path": "source.txt", "offset": 2, "max_chars": 3,
    })
    run = Run(
        goal="读取本地文件",
        workflow_steps=[WorkflowStep(id="retained", title="读取文件", summary="读取来源", action_ids=["old-read"])],
        workflow_revisions=[WorkflowRevision(
            id="revision-1", from_step_id="redo", feedback="调整方向", reused_step_ids=["retained"],
        )],
        messages=[
            ChatMessage(role="user", content="读取本地文件"),
            ChatMessage(role="assistant", tool_calls=[original]),
            ChatMessage(role="tool", name="read_file", tool_call_id="old-read", content="abcdef", metadata={
                "status": "succeeded", "path": str(source), "truncated": True, "next_offset": 6,
                "read_file_range": runner._read_file_range(original),
                "read_only_signature": runner._read_only_signature(original),
            }),
            ChatMessage(role="user", content="调整方向", metadata={"rewind_revision_id": "revision-1"}),
        ],
    )

    assert runner._guard_read_only_action(run, subset)[1] == "covered_read"
    assert runner._covered_read_file_result(run, runner._read_file_range(subset)).tool_call_id == "old-read"
    ordinary_followup = run.model_copy(deep=True)
    ordinary_followup.messages[-1].metadata.clear()
    assert runner._guard_read_only_action(ordinary_followup, subset) is None
    explicit_reread = run.model_copy(deep=True)
    explicit_reread.messages[-1].content = "请重新读取这一段"
    assert runner._guard_read_only_action(explicit_reread, subset) is None
    source.write_text("changed content", encoding="utf-8")
    assert runner._guard_read_only_action(run, subset) is None


def test_followup_about_earlier_attachment_requires_an_attachment_read(workspace):
    source = workspace / "rules.txt"
    source.write_text("Rules", encoding="utf-8")
    other = workspace / "other.txt"
    other.write_text("Other", encoding="utf-8")
    runner = build_runner(workspace, ScriptedProvider([]))
    run = Run(
        goal="根据附件整理材料",
        messages=[
            ChatMessage(role="user", content="根据附件整理材料", metadata={
                "attachments": [{"kind": "file", "path": "rules.txt"}],
            }),
            ChatMessage(role="tool", name="list_files", content="rules.txt", metadata={"status": "succeeded"}),
            ChatMessage(role="user", content="请优先使用已上传的附件，避免不必要的网页搜索"),
            ChatMessage(role="tool", name="read_file", content="Other", metadata={
                "status": "succeeded", "path": str(other),
            }),
        ],
    )

    browse = Action(tool_name="web_search", arguments={"query": "external source"})
    assert runner._guard_read_only_action(run, browse)[1] == "local_first"
    run.messages.append(ChatMessage(role="tool", name="read_file", content="Rules", metadata={
        "status": "succeeded", "path": str(source),
    }))
    assert runner._guard_read_only_action(run, browse) is None

    retained = run.model_copy(deep=True)
    retained.messages.pop()
    stat = source.stat()
    retained.messages.insert(1, ChatMessage(role="tool", name="read_file", content="Rules", metadata={
        "status": "succeeded", "path": str(source),
        "read_only_signature": f"read_file:{source}:{stat.st_mtime_ns}:{stat.st_size}:{{}}",
    }))
    assert runner._guard_read_only_action(retained, browse) is None
    unverified = retained.model_copy(deep=True)
    unverified.messages[1].metadata.pop("read_only_signature")
    assert runner._guard_read_only_action(unverified, browse)[1] == "local_first"
    source.write_text("Changed rules", encoding="utf-8")
    assert runner._guard_read_only_action(retained, browse)[1] == "local_first"


def test_changed_attachment_read_does_not_unlock_web_research(workspace):
    source = workspace / "rules.txt"
    source.write_text("Original rules", encoding="utf-8")
    runner = build_runner(workspace, ScriptedProvider([]))
    run = Run(
        goal="根据附件整理材料",
        messages=[ChatMessage(role="user", content="根据附件整理材料", metadata={
            "attachments": [{"kind": "file", "path": "rules.txt"}],
        })],
    )
    browse = Action(tool_name="web_search", arguments={"query": "external source"})
    read = Action(tool_name="read_file", arguments={"path": "rules.txt"})
    run.messages.append(ChatMessage(role="tool", name="read_file", content="Original rules", metadata={
        "status": "succeeded", "path": str(source),
        "read_only_signature": runner._read_only_signature(read),
    }))
    assert runner._guard_read_only_action(run, browse) is None

    source.write_text("Updated rules and more detail", encoding="utf-8")
    assert runner._guard_read_only_action(run, browse)[1] == "local_first"

    run.messages[-1].metadata["source_changed_around_read"] = True
    assert runner._guard_read_only_action(run, browse)[1] == "local_first"


def test_truncated_model_answer_is_continued_before_completion(workspace):
    runner = build_runner(
        workspace,
        ScriptedProvider(
            [
                LLMResponse(content="第一部分，", finish_reason="length"),
                LLMResponse(content="第二部分。", finish_reason="stop"),
            ]
        ),
    )
    run = Run(goal="summarize")
    final = asyncio.run(runner.run(run))
    assert final == "第一部分，第二部分。"
    assert run.status is RunStatus.COMPLETED
    assert run.messages[-1].content == final


def test_attachment_research_can_open_a_search_result(workspace):
    (workspace / "rules.txt").write_text("Local rules", encoding="utf-8")
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    registry.register(SearchTool())
    registry.register(FetchTool())
    premature = Action(id="premature", tool_name="web_search", arguments={"query": "official rules"})
    read = Action(id="read", tool_name="read_file", arguments={"path": "rules.txt"})
    search = Action(id="search", tool_name="web_search", arguments={"query": "official rules"})
    fetch = Action(id="fetch", tool_name="fetch_url", arguments={"url": "https://example.test/rules"})
    runner = AgentRunner(
        provider=ScriptedProvider([
            LLMResponse(tool_calls=[premature, read, search, fetch]), LLMResponse(content="done"),
        ]),
        registry=registry,
        context_builder=ContextBuilder(ContextBudget(8192, 0.7)),
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
    )
    run = Run(
        goal="Use the attached rules and research the latest official details",
        policy=UserPolicy(autonomy_threshold=RiskLevel.RED),
        messages=[
            ChatMessage(
                role="user",
                content="Use the attached rules and research the latest official details",
                metadata={"attachments": [{"kind": "file", "name": "rules.txt", "path": "rules.txt"}]},
            )
        ],
    )
    asyncio.run(runner.run(run))
    results = {message.tool_call_id: message for message in run.messages if message.role == "tool"}
    assert run.research_strategy.requires_local_evidence
    assert results["premature"].metadata["reason"] == "local_first"
    assert results["read"].metadata["status"] == "succeeded"
    assert results["search"].metadata["status"] == "succeeded"
    assert results["fetch"].metadata["status"] == "succeeded"


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


def test_skipped_read_does_not_spend_the_execution_budget(workspace):
    (workspace / "source.txt").write_text("Useful source", encoding="utf-8")
    actions = [
        Action(id="read_once", tool_name="read_file", arguments={"path": "source.txt"}),
        Action(id="read_again", tool_name="read_file", arguments={"path": "source.txt"}),
        Action(id="write_result", tool_name="write_file", arguments={"path": "answer.txt", "content": "done"}),
    ]
    runner = build_runner(
        workspace,
        ScriptedProvider([LLMResponse(tool_calls=actions), LLMResponse(content="Finished")]),
        max_tool_calls=2,
    )
    run = Run(goal="read and write an answer", policy=UserPolicy(autonomy_threshold=RiskLevel.GREEN))

    assert asyncio.run(runner.run(run)) == "Finished"
    results = {message.tool_call_id: message for message in run.messages if message.role == "tool"}
    assert results["read_again"].metadata["status"] == "skipped"
    assert results["write_result"].metadata["status"] == "succeeded"
    assert (workspace / "answer.txt").read_text(encoding="utf-8") == "done"


def test_completion_review_detects_external_edit_after_agent_write(workspace):
    target = workspace / "answer.txt"
    write = Action(id="write", tool_name="write_file", arguments={"path": "answer.txt", "content": "agent result"})

    class EditingProvider:
        def __init__(self):
            self.calls = 0
            self.review_seen = False

        async def chat(self, messages, _tools):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(tool_calls=[write])
            if self.calls == 2:
                target.write_text("external edit", encoding="utf-8")
                return LLMResponse(content="The agent result is saved.")
            self.review_seen = any(
                message.role == "system" and "Written files changed" in (message.content or "")
                for message in messages
            )
            return LLMResponse(content="The file was changed externally; I did not overwrite it.")

    provider = EditingProvider()
    run = Run(goal="save answer", policy=UserPolicy(autonomy_threshold=RiskLevel.GREEN))

    final = asyncio.run(build_runner(workspace, provider).run(run))

    assert provider.calls == 3
    assert provider.review_seen
    assert "changed externally" in final
    assert target.read_text(encoding="utf-8") == "external edit"
    assert "已写文件内容发生变化" in run.stage_notes["completion_review"]


def test_failed_read_is_not_retried_until_the_file_changes(workspace):
    actions = [
        Action(id="missing_first", tool_name="read_file", arguments={"path": "later.txt"}),
        Action(id="missing_repeat", tool_name="read_file", arguments={"path": "later.txt"}),
        Action(id="create_later", tool_name="write_file", arguments={"path": "later.txt", "content": "Ready"}),
        Action(id="read_after_create", tool_name="read_file", arguments={"path": "later.txt"}),
    ]
    runner = build_runner(
        workspace,
        ScriptedProvider([LLMResponse(tool_calls=actions), LLMResponse(content="Finished")]),
        max_tool_calls=3,
    )
    run = Run(goal="read and prepare a file", policy=UserPolicy(autonomy_threshold=RiskLevel.GREEN))

    assert asyncio.run(runner.run(run)) == "Finished"
    results = {message.tool_call_id: message for message in run.messages if message.role == "tool"}
    assert results["missing_first"].metadata["status"] == "failed"
    assert results["missing_repeat"].metadata["reason"] == "failed_read"
    assert results["create_later"].metadata["status"] == "succeeded"
    assert results["read_after_create"].metadata["status"] == "succeeded"
    assert results["read_after_create"].content == "Ready"


@pytest.mark.parametrize("raises_error", [False, True])
def test_failed_url_is_not_fetched_again_in_the_same_turn(workspace, raises_error):
    class FailingFetchTool:
        name = "fetch_url"

        def __init__(self):
            self.attempts = []

        def definition(self) -> ToolDefinition:
            return ToolDefinition(name=self.name, description="fetch", parameters={})

        async def execute(self, arguments, _context, action_id) -> ToolResult:
            self.attempts.append(arguments["url"])
            if raises_error:
                raise ToolValidationError("Could not fetch URL")
            return ToolResult(action_id=action_id, tool_name=self.name, ok=False, content="Unavailable")

    tool = FailingFetchTool()
    registry = ToolRegistry()
    registry.register(tool)
    actions = [
        Action(id="first", tool_name="fetch_url", arguments={"url": "https://example.test/one"}),
        Action(id="repeat", tool_name="fetch_url", arguments={"url": "https://example.test/one"}),
        Action(id="different", tool_name="fetch_url", arguments={"url": "https://example.test/two"}),
    ]
    runner = AgentRunner(
        provider=ScriptedProvider([LLMResponse(tool_calls=actions), LLMResponse(content="done")]),
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
        max_tool_calls=2,
    )
    run = Run(goal="research latest sources", policy=UserPolicy(autonomy_threshold=RiskLevel.RED))

    assert asyncio.run(runner.run(run)) == "done"
    results = {message.tool_call_id: message for message in run.messages if message.role == "tool"}
    assert results["first"].metadata["status"] == "failed"
    assert results["repeat"].metadata["reason"] == "failed_fetch"
    assert results["different"].metadata["status"] == "failed"
    assert tool.attempts == ["https://example.test/one", "https://example.test/two"]


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


def test_provider_wide_search_failure_stops_repeated_searches(workspace):
    class UnavailableSearchTool:
        name = "web_search"

        def definition(self) -> ToolDefinition:
            return ToolDefinition(name=self.name, description="search", parameters={})

        async def execute(self, arguments, _context, action_id) -> ToolResult:
            return ToolResult(
                action_id=action_id, tool_name=self.name, ok=False,
                content="All search providers failed.",
                metadata={"query": arguments["query"], "provider": None, "failures": ["provider unavailable"]},
            )

    registry = ToolRegistry()
    registry.register(UnavailableSearchTool())
    actions = [
        Action(id="outage_first", tool_name="web_search", arguments={"query": "first source"}),
        Action(id="outage_second", tool_name="web_search", arguments={"query": "second source"}),
        Action(id="outage_third", tool_name="web_search", arguments={"query": "third source"}),
    ]
    runner = AgentRunner(
        provider=ScriptedProvider([LLMResponse(tool_calls=actions), LLMResponse(content="Search is unavailable.")]),
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
        max_tool_calls=1,
    )
    run = Run(goal="research recent facts", policy=UserPolicy(autonomy_threshold=RiskLevel.RED))

    assert asyncio.run(runner.run(run)) == "Search is unavailable."
    results = {message.tool_call_id: message for message in run.messages if message.role == "tool"}
    assert results["outage_first"].metadata["status"] == "failed"
    assert results["outage_second"].metadata["reason"] == "search_unavailable"
    assert results["outage_third"].metadata["reason"] == "search_unavailable"
    assert not runner._web_tool_offered(run, "web_search")
    assert runner._web_tool_offered(run, "fetch_url")
    run.messages.append(ChatMessage(role="user", content="Retry research now"))
    assert runner._web_tool_offered(run, "web_search")


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
