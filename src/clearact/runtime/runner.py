from __future__ import annotations

import asyncio
from datetime import datetime

import httpx

from clearact.context.builder import ContextBuilder
from clearact.domain.enums import DecisionOutcome, RunStatus, Stage
from clearact.domain.models import Action, ChatMessage, Run, WorkflowPlanItem, WorkflowStep
from clearact.providers.base import LLMProvider
from clearact.runtime.approvals import ApprovalGate
from clearact.runtime.checkpoints import create_checkpoint
from clearact.runtime.event_bus import EventBus
from clearact.runtime.events import (
    action_completed,
    action_failed,
    action_started,
    approval_required,
    model_reasoning,
    model_recovered,
    model_retrying,
    run_completed,
    run_started,
)
from clearact.runtime.executor import ToolExecutor
from clearact.runtime.model_retry import (
    ModelRequestError,
    ModelServiceUnavailableError,
    describe_model_error,
    is_transient_model_error,
)
from clearact.runtime.policy import PolicyEngine
from clearact.runtime.risk import RiskEvaluator
from clearact.runtime.stage_mapper import StageMapper
from clearact.runtime.state import transition
from clearact.storage.checkpoint_store import CheckpointStore
from clearact.storage.run_store import RunStore
from clearact.tools.base import ToolContext
from clearact.tools.registry import ToolRegistry


class AgentRunner:
    def __init__(
        self,
        provider: LLMProvider,
        registry: ToolRegistry,
        context_builder: ContextBuilder,
        risk_evaluator: RiskEvaluator,
        policy_engine: PolicyEngine,
        approval_gate: ApprovalGate,
        executor: ToolExecutor,
        event_bus: EventBus,
        run_store: RunStore,
        checkpoint_store: CheckpointStore,
        tool_context: ToolContext,
        stage_mapper: StageMapper,
        max_iterations: int,
        max_tool_calls: int,
        model_retry_attempts: int = 3,
        retry_base_delay: float = 1.0,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._context_builder = context_builder
        self._risk_evaluator = risk_evaluator
        self._policy_engine = policy_engine
        self._approval_gate = approval_gate
        self._executor = executor
        self._event_bus = event_bus
        self._run_store = run_store
        self._checkpoint_store = checkpoint_store
        self._tool_context = tool_context
        self._stage_mapper = stage_mapper
        self._max_iterations = max_iterations
        self._max_tool_calls = max_tool_calls
        self._model_retry_attempts = max(1, model_retry_attempts)
        self._retry_base_delay = max(0.0, retry_base_delay)

    async def run(self, run: Run) -> str:
        try:
            return await self._run_loop(run)
        except asyncio.CancelledError:
            self._record_unfinished_actions(
                run,
                status="cancelled",
                content=(
                    "Action was cancelled before a final result was available. "
                    "Its side effects may be incomplete; verify before retrying."
                ),
                metadata={"reason": "run_cancelled", "side_effects": "unknown"},
            )
            if run.status in {RunStatus.CREATED, RunStatus.RUNNING, RunStatus.WAITING_APPROVAL, RunStatus.PAUSED}:
                transition(run, RunStatus.CANCELLED)
            self._run_store.save_run(run)
            raise
        except Exception as exc:
            self._record_unfinished_actions(
                run,
                status="failed",
                content=(
                    "Action was not completed because the run stopped: "
                    f"{type(exc).__name__}: {exc}. Verify side effects before retrying."
                ),
                metadata={"reason": "run_failed", "error_type": type(exc).__name__, "error": str(exc)},
            )
            if run.status in {RunStatus.CREATED, RunStatus.RUNNING, RunStatus.WAITING_APPROVAL, RunStatus.PAUSED}:
                transition(run, RunStatus.FAILED)
            self._fail_active_step(run)
            self._run_store.save_run(run)
            raise

    async def _run_loop(self, run: Run) -> str:
        transition(run, RunStatus.RUNNING)
        self._run_store.save_run(run)
        await self._event_bus.publish(run_started(run))
        if not run.workflow_steps:
            run.workflow_steps.append(
                WorkflowStep(
                    id="understand",
                    title="理解与拆解任务",
                    summary="正在识别目标、约束与完成条件。",
                    kind="analysis",
                    status="active",
                    start_message_index=0,
                )
            )
            self._run_store.save_run(run)
        recent_tool_names: set[str] = set()
        tool_call_count = 0

        for _ in range(self._max_iterations):
            definitions = self._registry.definitions()
            if not run.workflow_plan and "declare_workflow_plan" in self._registry.names():
                definitions = [tool for tool in definitions if tool.name == "declare_workflow_plan"]
            messages, tools = self._context_builder.build(run.messages, definitions, recent_tool_names)
            response = await self._chat_with_retries(run, messages, tools)
            reasoning = (response.reasoning_content or "").strip() or None
            if not response.tool_calls:
                final = response.content or "任务已结束，但模型没有提供最终说明。"
                run.messages.append(ChatMessage(role="assistant", content=final, reasoning_content=reasoning))
                self._complete_active_step(run)
                # Save before publishing so a polling UI never sees an event
                # whose referenced reasoning is absent from the run ledger.
                self._run_store.save_run(run)
                if reasoning:
                    await self._event_bus.publish(model_reasoning(run, reasoning, Stage.DELIVER))
                transition(run, RunStatus.COMPLETED)
                self._run_store.save_run(run)
                await self._event_bus.publish(run_completed(run))
                return final

            # Persist and execute precisely the tool plan accepted from the model.
            # A hidden per-turn search cap made the UI show planned searches that
            # never ran and breaks parity with nanobot's default tool semantics.
            accepted_actions = list(response.tool_calls)
            assistant_message_index = len(run.messages)
            run.messages.append(
                ChatMessage(
                    role="assistant",
                    content=response.content,
                    reasoning_content=reasoning,
                    tool_calls=accepted_actions,
                )
            )
            # Persist model decisions immediately so the web UI can show live progress.
            self._run_store.save_run(run)
            if reasoning:
                first_action = accepted_actions[0]
                assessment = self._risk_evaluator.assess(first_action)
                stage, _, _ = self._stage_mapper.map(first_action, assessment.level)
                await self._event_bus.publish(model_reasoning(run, reasoning, stage))
            recent_tool_names = set()
            try:
                for action in accepted_actions:
                    recent_tool_names.add(action.tool_name)
                    if action.tool_name == "declare_workflow_plan":
                        self._record_workflow_plan(run, action)
                        run.messages.append(
                            ChatMessage(
                                role="tool",
                                name=action.tool_name,
                                tool_call_id=action.id,
                                content="Workflow plan recorded. Begin its first phase.",
                                metadata={"status": "succeeded", "kind": "workflow_plan"},
                            )
                        )
                        self._run_store.save_run(run)
                        continue
                    if action.tool_name == "declare_workflow_step":
                        self._activate_workflow_step(run, action, assistant_message_index)
                        run.messages.append(
                            ChatMessage(
                                role="tool",
                                name=action.tool_name,
                                tool_call_id=action.id,
                                content="Workflow step recorded. Continue with this phase.",
                            )
                        )
                        self._run_store.save_run(run)
                        continue
                    tool_call_count += 1
                    if tool_call_count > self._max_tool_calls:
                        raise RuntimeError(f"Agent reached the maximum tool-call limit ({self._max_tool_calls}).")
                    self._attach_action_to_step(run, action, assistant_message_index)
                    assessment = self._risk_evaluator.assess(action)
                    decision = self._policy_engine.decide(assessment, run.policy, action.tool_name)
                    stage, title, detail = self._stage_mapper.map(action, assessment.level)
                    if decision.outcome == DecisionOutcome.DENY:
                        result_content = f"Action denied: {decision.reason}"
                        run.messages.append(
                            ChatMessage(
                                role="tool",
                                name=action.tool_name,
                                tool_call_id=action.id,
                                content=result_content,
                                metadata={"status": "denied", "reason": decision.reason},
                            )
                        )
                        self._run_store.save_run(run)
                        continue
                    if decision.outcome == DecisionOutcome.REQUIRE_APPROVAL:
                        transition(run, RunStatus.WAITING_APPROVAL)
                        self._run_store.save_run(run)
                        await self._event_bus.publish(approval_required(run, action, assessment.level, decision.reason))
                        granted = await self._approval_gate.request(action, assessment)
                        if run.status == RunStatus.WAITING_APPROVAL:
                            transition(run, RunStatus.RUNNING)
                            self._run_store.save_run(run)
                        if not granted:
                            result_content = (
                                "User declined this action. Continue with a safe alternative or explain the limitation."
                            )
                            run.messages.append(
                                ChatMessage(
                                    role="tool",
                                    name=action.tool_name,
                                    tool_call_id=action.id,
                                    content=result_content,
                                )
                            )
                            self._run_store.save_run(run)
                            continue
                    await self._event_bus.publish(action_started(run, action, stage, assessment.level, title, detail))
                    # The event is persisted by the bus; persist the decision too before
                    # awaiting the tool so polling clients do not appear stalled.
                    self._run_store.save_run(run)
                    try:
                        result = await self._executor.execute(action, self._tool_context)
                    except Exception as exc:
                        # Persist and emit every failed attempt just like successful
                        # ones. Otherwise polling clients lose the current tool result
                        # until a later model turn happens to save the run.
                        result_content = f"Tool execution failed: {type(exc).__name__}: {exc}"
                        run.messages.append(
                            ChatMessage(
                                role="tool",
                                name=action.tool_name,
                                tool_call_id=action.id,
                                content=result_content,
                                metadata={"status": "failed", "error_type": type(exc).__name__, "error": str(exc)},
                            )
                        )
                        self._run_store.save_run(run)
                        await self._event_bus.publish(
                            action_failed(run, action, stage, assessment.level, result_content[:240])
                        )
                        continue
                    result_status = "succeeded" if result.ok else "failed"
                    run.messages.append(
                        ChatMessage(
                            role="tool",
                            name=action.tool_name,
                            tool_call_id=action.id,
                            content=result.content,
                            metadata={**result.metadata, "status": result_status},
                        )
                    )
                    if result.ok:
                        checkpoint = create_checkpoint(run.id, action)
                        self._checkpoint_store.save(checkpoint)
                    self._run_store.save_run(run)
                    event_factory = action_completed if result.ok else action_failed
                    await self._event_bus.publish(
                        event_factory(run, action, stage, assessment.level, result.content[:240])
                    )
            except asyncio.CancelledError:
                self._record_unfinished_actions(
                    run,
                    status="cancelled",
                    content=(
                        "Action was cancelled before a final result was available. "
                        "Its side effects may be incomplete; verify before retrying."
                    ),
                    metadata={"reason": "run_cancelled", "side_effects": "unknown"},
                )
                self._run_store.save_run(run)
                raise
            except Exception as exc:
                self._record_unfinished_actions(
                    run,
                    status="failed",
                    content=(
                        "Action was not completed because the run stopped: "
                        f"{type(exc).__name__}: {exc}. Verify side effects before retrying."
                    ),
                    metadata={"reason": "run_failed", "error_type": type(exc).__name__, "error": str(exc)},
                )
                self._run_store.save_run(run)
                raise

        raise RuntimeError(f"Agent reached the maximum iteration limit ({self._max_iterations}).")

    async def _chat_with_retries(self, run: Run, messages, tools):
        failures = 0
        while True:
            try:
                response = await self._provider.chat(messages, tools)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                if not is_transient_model_error(exc):
                    if isinstance(exc, httpx.HTTPStatusError):
                        language = run.execution.interface_language or "zh"
                        raise ModelRequestError(describe_model_error(exc, language)) from exc
                    raise
                if failures >= self._model_retry_attempts:
                    language = run.execution.interface_language or "zh"
                    reason = describe_model_error(exc, language)
                    if language == "zh":
                        message = f"{reason}，已自动尝试 {failures} 次仍未恢复。请稍后重试或更换模型服务。"
                    else:
                        message = (
                            f"{reason}. The request still failed after {failures} attempts. "
                            "Try again later or use another model service."
                        )
                    raise ModelServiceUnavailableError(message) from exc
                delay = self._retry_base_delay * (2 ** (failures - 1))
                language = run.execution.interface_language or "zh"
                detail = describe_model_error(exc, language)
                # The ledger is saved before sleeping, so polling clients keep
                # the current task and all completed phases while it reconnects.
                self._run_store.save_run(run)
                await self._event_bus.publish(
                    model_retrying(
                        run,
                        attempt=failures + 1,
                        max_attempts=self._model_retry_attempts,
                        delay_seconds=delay,
                        detail=detail,
                        error_type=type(exc).__name__,
                    )
                )
                await asyncio.sleep(delay)
                continue
            if failures:
                self._run_store.save_run(run)
                await self._event_bus.publish(model_recovered(run, failures + 1))
            return response

    def _record_unfinished_actions(
        self,
        run: Run,
        *,
        status: str,
        content: str,
        metadata: dict[str, str],
    ) -> None:
        """Close the current model tool plan before persisting an interrupted run."""
        completed_ids: set[str] = set()
        for message in reversed(run.messages):
            if message.role == "tool":
                if message.tool_call_id:
                    completed_ids.add(message.tool_call_id)
                continue
            if message.role == "assistant" and message.tool_calls:
                for action in message.tool_calls:
                    if action.id not in completed_ids:
                        run.messages.append(
                            ChatMessage(
                                role="tool",
                                name=action.tool_name,
                                tool_call_id=action.id,
                                content=content,
                                metadata={**metadata, "status": status},
                            )
                        )
                return
            return

    def _record_workflow_plan(self, run: Run, action: Action) -> None:
        """Validate and persist a public plan; never expose hidden model reasoning."""
        raw_steps = action.arguments.get("steps", [])
        planned: list[WorkflowPlanItem] = []
        seen: set[str] = set()
        if isinstance(raw_steps, list):
            for index, value in enumerate(raw_steps[:12]):
                if not isinstance(value, dict):
                    continue
                title = str(value.get("title", "")).strip()
                summary = str(value.get("summary", "")).strip()
                raw_id = str(value.get("id", "")).strip() or f"phase-{index + 1}"
                item_id = raw_id[:64]
                if not title or not summary or item_id in seen:
                    continue
                seen.add(item_id)
                planned.append(
                    WorkflowPlanItem(
                        id=item_id,
                        title=title[:80],
                        summary=summary[:500],
                        kind=str(value.get("kind", "general"))[:24],
                    )
                )
        if not planned:
            planned = [WorkflowPlanItem(id="execute", title="完成任务", summary="完成目标并核验结果。")]
        run.workflow_plan = planned
        overview = str(action.arguments.get("summary", "")).strip()
        run.stage_notes["understand"] = overview[:1200] or "已根据目标生成可执行计划。"
        if run.workflow_steps and run.workflow_steps[0].id == "understand":
            run.workflow_steps[0].summary = run.stage_notes["understand"]
            run.workflow_steps[0].status = "completed"

    def _activate_workflow_step(self, run: Run, action: Action, message_index: int) -> None:
        title = str(action.arguments.get("title", "")).strip()
        summary = str(action.arguments.get("summary", "")).strip()
        if not title or not summary:
            return
        self._complete_active_step(run)
        plan_item_id = str(action.arguments.get("plan_item_id", "")).strip() or None
        planned = next((item for item in run.workflow_plan if item.id == plan_item_id), None)
        run.workflow_steps.append(
            WorkflowStep(
                title=(planned.title if planned else title)[:80],
                summary=(planned.summary if planned else summary)[:500],
                plan_item_id=planned.id if planned else plan_item_id,
                kind=(planned.kind if planned else str(action.arguments.get("kind", "general")))[:24],
                status="active",
                start_message_index=message_index,
            )
        )

    def _attach_action_to_step(self, run: Run, action: Action, message_index: int) -> None:
        current = next((step for step in reversed(run.workflow_steps) if step.status == "active"), None)
        if current is None or current.id == "understand":
            assessment = self._risk_evaluator.assess(action)
            stage, title, detail = self._stage_mapper.map(action, assessment.level)
            self._complete_active_step(run)
            current = WorkflowStep(
                title=title,
                summary=detail,
                kind=stage.value,
                status="active",
                start_message_index=message_index,
            )
            run.workflow_steps.append(current)
        if action.id not in current.action_ids:
            current.action_ids.append(action.id)

    @staticmethod
    def _complete_active_step(run: Run) -> None:
        for step in reversed(run.workflow_steps):
            if step.status == "active":
                step.status = "completed"
                step.completed_at = datetime.now()
                return

    @staticmethod
    def _fail_active_step(run: Run) -> None:
        for step in reversed(run.workflow_steps):
            if step.status == "active":
                step.status = "failed"
                step.completed_at = datetime.now()
                return
