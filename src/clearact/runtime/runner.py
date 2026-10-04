from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

import httpx

from clearact.context.builder import ContextBuilder
from clearact.domain.enums import DecisionOutcome, RunStatus, Stage
from clearact.domain.errors import ToolValidationError
from clearact.domain.models import Action, ChatMessage, Run, RunEvent, WorkflowPlanItem, WorkflowStep
from clearact.providers.base import LLMProvider
from clearact.runtime.approvals import ApprovalGate
from clearact.runtime.checkpoints import create_checkpoint
from clearact.runtime.event_bus import EventBus
from clearact.runtime.events import (
    action_completed,
    action_failed,
    action_not_run,
    action_started,
    approval_required,
    completion_review_requested,
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
from clearact.runtime.research_strategy import classify_research_strategy, local_source_requested
from clearact.runtime.risk import RiskEvaluator
from clearact.runtime.stage_mapper import StageMapper
from clearact.runtime.state import transition
from clearact.storage.checkpoint_store import CheckpointStore
from clearact.storage.run_store import RunStore
from clearact.tools.base import ToolContext
from clearact.tools.filesystem import MAX_READ_CHARS
from clearact.tools.registry import ToolRegistry

_SIMPLE_CHAT = re.compile(
    r"^\s*(?:你好[呀啊]?|嗨|哈[喽啰]|早上好|下午好|晚上好|谢谢(?:你)?|再见|"
    r"hi|hello|hey|thanks|thank\s+you|good\s+(?:morning|evening)|bye)[!！?？。,.，\s]*$",
    re.IGNORECASE,
)
_EXPLICIT_REREAD = ("重新读取", "重新阅读", "再读一遍", "reread", "re-read", "read again")


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
        run.stage_notes.pop("completion_review", None)
        run.research_strategy = classify_research_strategy(run)
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
        invalid_plan_attempts = 0
        stalled_rounds = 0
        completion_review_sent = False
        completion_review_instruction: ChatMessage | None = None
        # A follow-up may reuse an older plan; only audit phases planned in this turn.
        review_new_plan = not run.workflow_plan

        for iteration in range(self._max_iterations):
            definitions = self._registry.definitions()
            if not run.workflow_plan and "declare_workflow_plan" in self._registry.names():
                definitions = [tool for tool in definitions if tool.name == "declare_workflow_plan"]
            else:
                definitions = [
                    tool for tool in definitions
                    if tool.name != "declare_workflow_plan"
                    and self._policy_tool_offered(run, tool.name)
                    and self._web_tool_offered(run, tool.name)
                    and (
                        tool.name != "revise_workflow_plan"
                        or (
                            completion_review_instruction is None
                            and (self._unreached_plan_items(run) or len(run.workflow_plan) < 12)
                            and not self._plan_revision_without_new_result(run)
                        )
                    )
                ]
            context_messages = [*run.messages]
            if run.workflow_plan:
                context_messages.append(ChatMessage(
                    role="system",
                    content=(
                        "A workflow plan already exists. Do not call declare_workflow_plan again; "
                        "keep reached phases and use revise_workflow_plan only to change future phases."
                    ),
                ))
            policy_guidance = self._policy_guidance(run)
            if policy_guidance:
                context_messages.append(ChatMessage(role="system", content=policy_guidance))
            if completion_review_instruction:
                context_messages.append(completion_review_instruction)
            messages, tools = self._context_builder.build(context_messages, definitions, recent_tool_names)
            # Freeze the provider's input view: an uncompacted build may return
            # the mutable run.messages list, which grows as tools finish.
            messages = list(messages)
            response = await self._chat_with_retries(run, messages, tools)
            response = await self._finish_truncated_response(run, messages, response)
            if not run.workflow_plan and "declare_workflow_plan" in self._registry.names():
                if not response.tool_calls and (response.content or "").strip() and _SIMPLE_CHAT.fullmatch(run.goal):
                    # A greeting needs no plan/tool round trips. Show one honest
                    # visible phase instead of rejecting a normal chat reply.
                    run.workflow_plan = [
                        WorkflowPlanItem(id="reply", title="直接回复", summary="简单对话，无需调用工具。")
                    ]
                    active_step = run.workflow_steps[-1]
                    active_step.title = "直接回复"
                    active_step.summary = "简单对话，无需调用工具。"
                    active_step.plan_item_id = "reply"
                elif not response.tool_calls or any(
                    action.tool_name != "declare_workflow_plan" for action in response.tool_calls
                ):
                    raise ModelRequestError(
                        "模型没有按要求返回任务步骤。请确认所选模型支持工具调用，或更换模型后重试。"
                    )
            reasoning = (response.reasoning_content or "").strip() or None
            if not response.tool_calls:
                if not (response.content or "").strip():
                    raise ModelRequestError("模型没有返回可用的答复或工具调用，任务尚未完成。")
                pending = self._unreached_plan_items(run) if review_new_plan else []
                missing = self._missing_written_files(run)
                changed = self._changed_written_files(run)
                unworked = self._unworked_phase_titles(run)
                if (
                    (pending or missing or changed or unworked)
                    and not completion_review_sent
                    and iteration + 1 < self._max_iterations
                ):
                    completion_review_sent = True
                    pending_titles = [item.title for item in pending]
                    completion_review_instruction = ChatMessage(
                        role="system",
                        content=(
                            "Completion check: Your draft final answer was not sent. "
                            f"Unreached planned phases: {', '.join(pending_titles) or 'none'}. "
                            f"Written files no longer present: {', '.join(missing) or 'none'}. "
                            f"Written files changed since the last agent write: {', '.join(changed) or 'none'}. "
                            f"Declared work phases with no actions: {', '.join(unworked) or 'none'}. "
                            "Continue any necessary work with the available tools, or explicitly explain "
                            "why a planned phase is no longer needed. Do not overwrite external edits without "
                            "permission; disclose them if they prevent completion. Verify files before claiming "
                            "they contain your result. "
                            "Then give an honest final answer."
                        ),
                    )
                    await self._event_bus.publish(completion_review_requested(
                        run, pending_titles, missing, changed, unworked,
                    ))
                    continue
                if pending or missing or changed or unworked:
                    warning = (
                        (f"未进入计划阶段：{', '.join(item.title for item in pending)}。" if pending else "")
                        + (f"未找到已写文件：{', '.join(missing)}。" if missing else "")
                        + (f"已写文件内容发生变化：{', '.join(changed)}。" if changed else "")
                        + (f"阶段尚无实际操作：{', '.join(unworked)}。" if unworked else "")
                    )
                    run.stage_notes["completion_review"] = warning
                else:
                    run.stage_notes.pop("completion_review", None)
                final = response.content
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

            # Persist every model-proposed action. Guarded actions still receive
            # a visible tool result, so the UI and model ledger stay in sync.
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
                await self._event_bus.publish(model_reasoning(run, reasoning, stage, first_action.id))
            recent_tool_names = set()
            try:
                for action in accepted_actions:
                    recent_tool_names.add(action.tool_name)
                    if action.tool_name == "declare_workflow_plan":
                        if run.workflow_plan:
                            run.messages.append(ChatMessage(
                                role="tool",
                                name=action.tool_name,
                                tool_call_id=action.id,
                                content=(
                                    "Plan unchanged: a workflow plan already exists. "
                                    "Use revise_workflow_plan to change future phases."
                                ),
                                metadata={"status": "skipped", "reason": "existing_plan"},
                            ))
                            self._run_store.save_run(run)
                            continue
                        recorded = self._record_workflow_plan(run, action)
                        run.messages.append(
                            ChatMessage(
                                role="tool",
                                name=action.tool_name,
                                tool_call_id=action.id,
                                content=("Workflow plan recorded. Begin its first phase." if recorded else
                                         "Plan not recorded: provide at least one phase with a unique id, "
                                         "non-empty title, and summary."),
                                metadata={"status": "succeeded" if recorded else "failed", "kind": "workflow_plan"},
                            )
                        )
                        self._run_store.save_run(run)
                        if not recorded:
                            invalid_plan_attempts += 1
                            if invalid_plan_attempts >= 3:
                                raise ModelRequestError(
                                    "模型连续三次未能生成有效任务步骤。请确认模型支持工具调用，或更换模型后重试。"
                                )
                        continue
                    if action.tool_name == "declare_workflow_step":
                        step_outcome = self._activate_workflow_step(run, action, assistant_message_index)
                        step_result = {
                            "activated": (
                                "Workflow step recorded. Continue with this phase.", "succeeded", "workflow_step",
                            ),
                            "duplicate": (
                                "This phase is already active; continue its work instead of repeating it.",
                                "skipped", "duplicate_phase",
                            ),
                            "invalid": (
                                "Phase not recorded: provide a non-empty title and summary.",
                                "failed", "invalid_phase",
                            ),
                            "unknown_plan_item": (
                                "Phase not recorded: use a planned phase id, or revise the remaining plan first.",
                                "failed", "unknown_plan_item",
                            ),
                        }[step_outcome]
                        run.messages.append(
                            ChatMessage(
                                role="tool",
                                name=action.tool_name,
                                tool_call_id=action.id,
                                content=step_result[0],
                                metadata={"status": step_result[1], "reason": step_result[2]},
                            )
                        )
                        self._run_store.save_run(run)
                        continue
                    if action.tool_name == "revise_workflow_plan":
                        if self._plan_revision_without_new_result(run):
                            run.messages.append(ChatMessage(
                                role="tool", name=action.tool_name, tool_call_id=action.id,
                                content=("Plan unchanged: first make progress with a task tool or wait for new "
                                         "user feedback before revising it again."),
                                metadata={"status": "skipped", "reason": "no_new_result"},
                            ))
                            self._run_store.save_run(run)
                            continue
                        change = self._revise_workflow_plan(run, action)
                        run.messages.append(ChatMessage(
                            role="tool", name=action.tool_name, tool_call_id=action.id,
                            content=(
                                "Remaining phases updated; continue with the revised plan." if change else
                                "Plan unchanged: provide a reason and future phases different from the current plan."
                            ),
                            metadata={"status": "succeeded" if change else "failed", **(change or {})},
                        ))
                        self._run_store.save_run(run)
                        if change:
                            review_new_plan = True
                            await self._event_bus.publish(RunEvent(
                                type="workflow.plan_revised", run_id=run.id, action_id=action.id,
                                stage=Stage.UNDERSTAND, title="后续任务步骤已调整",
                                detail=change["reason"], data=change,
                            ))
                        continue
                    self._attach_action_to_step(run, action, assistant_message_index)
                    assessment = self._risk_evaluator.assess(action)
                    stage, title, detail = self._stage_mapper.map(action, assessment.level)
                    decision = self._policy_engine.decide(assessment, run.policy, action.tool_name)
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
                        await self._event_bus.publish(action_not_run(
                            run, action, stage, assessment.level, outcome="denied", reason=decision.reason,
                        ))
                        continue
                    guard = self._guard_read_only_action(run, action)
                    if guard and decision.outcome == DecisionOutcome.REQUIRE_APPROVAL and guard[1] in {
                        "duplicate_read", "similar_search", "covered_read",
                    }:
                        # Approval applies to viewing cached content too. Re-execute after confirmation
                        # so a previously approved result cannot bypass the current policy.
                        guard = None
                    if guard:
                        guard_reason, guard_kind = guard
                        reuse_metadata: dict[str, str] = {}
                        if guard_kind in {"duplicate_read", "similar_search", "covered_read"}:
                            source = (
                                self._successful_result_for_signature(run, self._read_only_signature(action))
                                if guard_kind == "duplicate_read"
                                else self._similar_search_result(run, action)
                                if guard_kind == "similar_search"
                                else self._covered_read_file_result(run, self._read_file_range(action))
                            )
                            if source:
                                reuse_metadata["reused_from_action_id"] = source.tool_call_id or ""
                                if not any(message is source for message in messages):
                                    reused_content = source.content or ""
                                    if guard_kind == "covered_read":
                                        requested = self._read_file_range(action)
                                        previous = source.metadata.get("read_file_range")
                                        if requested and isinstance(previous, dict):
                                            start = int(requested["offset"]) - int(previous["offset"])
                                            length = int(requested["end"]) - int(requested["offset"])
                                            reused_content = reused_content[start:start + length]
                                    guard_reason = (
                                        f"Reused result from action {source.tool_call_id}; no new tool call was made.\n"
                                        f"{reused_content}"
                                    )
                        run.messages.append(
                            ChatMessage(
                                role="tool",
                                name=action.tool_name,
                                tool_call_id=action.id,
                                content=guard_reason,
                                metadata={"status": "skipped", "reason": guard_kind, **reuse_metadata},
                            )
                        )
                        self._run_store.save_run(run)
                        await self._event_bus.publish(action_not_run(
                            run, action, stage, assessment.level, outcome="skipped", reason=guard_kind,
                        ))
                        continue
                    if tool_call_count >= self._max_tool_calls:
                        raise RuntimeError(f"Agent reached the maximum tool-call limit ({self._max_tool_calls}).")
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
                                    metadata={"status": "denied", "reason": "user_declined"},
                                )
                            )
                            self._run_store.save_run(run)
                            await self._event_bus.publish(action_not_run(
                                run, action, stage, assessment.level,
                                outcome="denied", reason="user_declined",
                            ))
                            continue
                    tool_call_count += 1
                    await self._event_bus.publish(action_started(
                        run, action, stage, assessment.level, title, detail,
                        policy_outcome=decision.outcome.value,
                        policy_reason=decision.reason,
                    ))
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
                        failed_read_signature = (
                            self._failed_local_read_signature(action)
                            if isinstance(exc, ToolValidationError) else None
                        )
                        failed_fetch_signature = (
                            self._read_only_signature(action) if action.tool_name == "fetch_url" else None
                        )
                        run.messages.append(
                            ChatMessage(
                                role="tool",
                                name=action.tool_name,
                                tool_call_id=action.id,
                                content=result_content,
                                metadata={
                                    "status": "failed", "error_type": type(exc).__name__, "error": str(exc),
                                    **({"failed_read_signature": failed_read_signature}
                                       if failed_read_signature else {}),
                                    **({"failed_fetch_signature": failed_fetch_signature}
                                       if failed_fetch_signature else {}),
                                },
                            )
                        )
                        self._run_store.save_run(run)
                        await self._event_bus.publish(
                            action_failed(run, action, stage, assessment.level, result_content[:240])
                        )
                        continue
                    result_status = "succeeded" if result.ok else "failed"
                    read_only_signature = self._read_only_signature(action) if result.ok else None
                    read_file_range = self._read_file_range(action) if result.ok else None
                    if result.ok and action.tool_name in {"read_file", "read_pdf"}:
                        source_version = result.metadata.get("source_version")
                        current_version = read_file_range.get("version") if read_file_range else None
                        if action.tool_name == "read_pdf":
                            try:
                                stat = Path(result.metadata["path"]).stat()
                                current_version = f"{stat.st_mtime_ns}:{stat.st_size}"
                            except (KeyError, OSError, TypeError):
                                current_version = None
                        if not isinstance(source_version, str) or source_version != current_version:
                            read_only_signature = None
                            read_file_range = None
                            if "source_version" in result.metadata:
                                result.metadata["source_changed_around_read"] = True
                                result.content += (
                                    "\n\n[File changed after this read; verify it again before relying on this result.]"
                                )
                    run.messages.append(
                        ChatMessage(
                            role="tool",
                            name=action.tool_name,
                            tool_call_id=action.id,
                            content=result.content,
                            metadata={
                                **result.metadata,
                                "status": result_status,
                                **({"read_only_signature": read_only_signature} if read_only_signature else {}),
                                **({"read_file_range": read_file_range} if read_file_range else {}),
                                **({"failed_fetch_signature": self._read_only_signature(action)}
                                   if not result.ok and action.tool_name == "fetch_url" else {}),
                            },
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

            round_results = [
                message for message in run.messages[assistant_message_index + 1:]
                if message.role == "tool"
            ]
            if round_results and all(
                message.metadata.get("status") in {"skipped", "denied"} for message in round_results
            ):
                stalled_rounds += 1
                if stalled_rounds >= 3:
                    language = run.execution.interface_language or "zh"
                    raise ModelRequestError(
                        "模型连续三轮只提出被跳过或未获授权的操作，已停止以避免空转。"
                        "请调整任务说明或权限后重试。" if language == "zh" else
                        "The model proposed only skipped or unauthorized actions for three rounds. "
                        "Stopped to avoid a loop; revise the task or permissions and retry."
                    )
            else:
                stalled_rounds = 0

        raise RuntimeError(f"Agent reached the maximum iteration limit ({self._max_iterations}).")

    @staticmethod
    def _unreached_plan_items(run: Run) -> list[WorkflowPlanItem]:
        reached = {step.plan_item_id for step in run.workflow_steps}
        return [item for item in run.workflow_plan if item.id not in reached]

    @staticmethod
    def _unworked_phase_titles(run: Run) -> list[str]:
        current_turn = next(
            (index for index in range(len(run.messages) - 1, -1, -1) if run.messages[index].role == "user"),
            -1,
        )
        return [
            step.title for step in run.workflow_steps
            if step.kind in {"research", "files", "external"}
            and step.status in {"active", "needs_review"}
            and not step.action_ids
            and step.start_message_index is not None
            and step.start_message_index >= current_turn
        ]

    @staticmethod
    def _missing_written_files(run: Run) -> list[str]:
        last_user_index = max(
            (index for index, message in enumerate(run.messages) if message.role == "user"),
            default=-1,
        )
        written = {
            str(message.metadata["path"])
            for message in run.messages[last_user_index + 1:]
            if message.role == "tool" and message.name == "write_file"
            and message.metadata.get("status") == "succeeded"
            and isinstance(message.metadata.get("path"), str)
        }
        return sorted(path for path in written if not Path(path).is_file())

    @staticmethod
    def _changed_written_files(run: Run) -> list[str]:
        """Check the last successful agent write for each file in this user turn."""
        last_user_index = max(
            (index for index, message in enumerate(run.messages) if message.role == "user"),
            default=-1,
        )
        current_turn = run.messages[last_user_index + 1:]
        actions = {
            action.id: action
            for message in current_turn if message.role == "assistant"
            for action in message.tool_calls if action.tool_name == "write_file"
        }
        expected: dict[str, str] = {}
        for message in current_turn:
            if message.role != "tool" or message.name != "write_file":
                continue
            if message.metadata.get("status") != "succeeded":
                continue
            path = message.metadata.get("path")
            action = actions.get(message.tool_call_id or "")
            content = action.arguments.get("content") if action else None
            if isinstance(path, str) and isinstance(content, str):
                expected[path] = content
        changed: list[str] = []
        for path, content in expected.items():
            target = Path(path)
            if not target.is_file():
                continue  # The missing-file check reports this separately.
            try:
                if target.read_text(encoding="utf-8") != content:
                    changed.append(path)
            except (OSError, UnicodeError):
                changed.append(path)
        return sorted(changed)

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

    async def _finish_truncated_response(self, run: Run, messages: list[ChatMessage], response):
        """Continue text cut off by the provider before treating it as a final answer."""
        reason = (response.finish_reason or "").casefold()
        if reason in {"content_filter", "error", "cancelled", "abort"}:
            raise ModelRequestError(f"模型未能完成答复（finish_reason={reason}）。")
        if reason == "tool_calls" and not response.tool_calls:
            raise ModelRequestError("模型报告了工具调用，但没有返回可执行的工具参数。")
        if reason not in {"length", "max_tokens", "max_output_tokens", "token_limit"}:
            return response
        if response.tool_calls:
            raise ModelRequestError("模型的工具调用因输出长度限制而截断；请增加模型输出预算后重试。")

        combined = response.content or ""
        for _ in range(2):
            continuation = [
                *messages,
                ChatMessage(role="assistant", content=combined[-4000:]),
                ChatMessage(
                    role="user",
                    content=(
                        "Continue the interrupted answer from exactly where it stopped. "
                        "Do not repeat completed text."
                    ),
                ),
            ]
            continuation, _ = self._context_builder.build(continuation, [], set())
            next_response = await self._chat_with_retries(run, continuation, [])
            next_reason = (next_response.finish_reason or "").casefold()
            if next_response.tool_calls or next_reason in {
                "content_filter", "error", "cancelled", "abort", "tool_calls"
            }:
                raise ModelRequestError("模型续写未能正常完成，任务没有被标记为完成。")
            combined += next_response.content or ""
            if next_reason not in {"length", "max_tokens", "max_output_tokens", "token_limit"}:
                return next_response.model_copy(update={"content": combined})
        raise ModelRequestError("模型答复连续被输出长度限制截断；请提高模型输出预算后重试。")

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

    def _guard_read_only_action(self, run: Run, action: Action) -> tuple[str, str] | None:
        """Prevent unproductive inspection loops without discarding evidence."""
        strategy = run.research_strategy or classify_research_strategy(run)
        if action.tool_name in {"web_search", "fetch_url", "computer_use"} and strategy.mode == "local_only":
            return (
                "Skipped: the user asked to work without web access. Use local and attached evidence only.",
                "local_only",
            )
        if (
            action.tool_name in {"web_search", "fetch_url", "computer_use"}
            and strategy.requires_local_evidence
            and not self._local_evidence_seen(run)
        ):
            return (
                "Skipped: inspect the supplied local file or workspace first. Use web sources only if a "
                "specific external fact is still missing after that inspection.",
                "local_first",
            )
        if action.tool_name == "web_search" and self._search_provider_unavailable(run):
            return (
                "Skipped: all configured search providers failed earlier in this turn. "
                "Use local evidence or a known authoritative URL with fetch_url; explain the limitation if needed.",
                "search_unavailable",
            )
        failed_read_signature = self._failed_local_read_signature(action)
        if failed_read_signature and self._failed_read_seen(run, failed_read_signature):
            return (
                "Skipped: this read already failed while the file and request were unchanged. "
                "Correct the path or options, use another source, or continue with the available evidence.",
                "failed_read",
            )
        if action.tool_name == "fetch_url" and self._failed_fetch_seen(run, self._read_only_signature(action)):
            return (
                "Skipped: this URL already failed to open in the current turn. Try another source "
                "or continue with the evidence already available.",
                "failed_fetch",
            )
        signature = self._read_only_signature(action)
        if signature and signature in self._completed_read_only_signatures(run):
            return (
                "Skipped: this exact read-only operation already completed earlier in the task. "
                "Use its recorded result, request a different range, or proceed to the next task step.",
                "duplicate_read",
            )
        if action.tool_name == "web_search" and self._similar_search_result(run, action):
            return (
                "Skipped: a near-identical search already returned results. Open one of those pages "
                "or continue with the evidence instead of rephrasing the same query.",
                "similar_search",
            )
        if (
            action.tool_name == "web_search"
            and strategy.total_search_limit is not None
            and self._search_total(run) >= strategy.total_search_limit
        ):
            return (
                "Search budget reached for this task type. Continue from the collected evidence or ask the "
                "user to authorize broader research in a follow-up.",
                "search_budget",
            )
        if action.tool_name == "web_search" and self._search_streak(run) >= strategy.search_streak_limit:
            return (
                "Search paused: several searches have already returned results. Open a relevant result "
                "with fetch_url, use the evidence already collected, or finish the task. A successful "
                "source fetch will allow another search if it is still needed.",
                "search_streak",
            )
        read_range = self._read_file_range(action)
        if read_range and self._covered_read_file_result(run, read_range):
            return (
                "Skipped: this file range is already covered by an earlier read of the unchanged file. "
                "Use that result or continue from its next_offset.",
                "covered_read",
            )
        return None

    @staticmethod
    def _policy_tool_offered(run: Run, tool_name: str) -> bool:
        if tool_name in {"list_files", "read_file", "read_pdf"}:
            return run.policy.allow_read and run.policy.capability_rules.get("local_read") != "deny"
        if tool_name == "write_file":
            return run.policy.allow_write
        if tool_name in {"web_search", "fetch_url"}:
            return run.policy.allow_web and run.policy.capability_rules.get("web_read") != "deny"
        if tool_name == "computer_use" or tool_name.startswith("mcp__"):
            return run.policy.allow_web
        return True

    @staticmethod
    def _policy_guidance(run: Run) -> str | None:
        disabled: list[str] = []
        if not AgentRunner._policy_tool_offered(run, "read_file"):
            disabled.append("Local file reading is disabled: do not claim to have inspected files or attachments.")
        if not run.policy.allow_write:
            disabled.append("File writing is disabled: do not claim to have saved or modified files.")
        if not AgentRunner._policy_tool_offered(run, "web_search"):
            disabled.append("Web search and URL fetching are disabled: do not claim to have verified external facts.")
        if not run.policy.allow_web:
            disabled.append("Browser control and external MCP services are disabled.")
        if not disabled:
            return None
        return "Current permission limits: " + " ".join(disabled) + " Explain any blocked requirement; do not guess."

    def _web_tool_offered(self, run: Run, tool_name: str) -> bool:
        if tool_name not in {"computer_use", "web_search", "fetch_url"}:
            return True
        if not self._policy_tool_offered(run, tool_name):
            return False
        strategy = run.research_strategy or classify_research_strategy(run)
        if tool_name == "computer_use":
            return bool(
                strategy.mode != "local_only"
                and (not strategy.requires_local_evidence or self._local_evidence_seen(run))
                and self._tool_context.computer_use_available
                and self._tool_context.computer_use_available(run.id)
            )
        if strategy.mode == "local_only":
            return False
        if strategy.requires_local_evidence and not self._local_evidence_seen(run):
            return False
        if tool_name == "web_search":
            if self._search_provider_unavailable(run):
                return False
            if self._search_streak(run) >= strategy.search_streak_limit:
                return False
            if strategy.total_search_limit is not None and self._search_total(run) >= strategy.total_search_limit:
                return False
        return True

    @staticmethod
    def _search_total(run: Run) -> int:
        current_turn = next(
            (index for index in range(len(run.messages) - 1, -1, -1) if run.messages[index].role == "user"),
            -1,
        )
        return sum(
            message.role == "tool"
            and message.name == "web_search"
            and message.metadata.get("status") == "succeeded"
            for message in run.messages[current_turn + 1:]
        )

    @staticmethod
    def _search_provider_unavailable(run: Run) -> bool:
        """A provider-wide failure has already exhausted search fallbacks this turn."""
        for message in reversed(run.messages):
            if message.role == "user":
                break
            if message.role != "tool" or message.name != "web_search":
                continue
            if message.metadata.get("status") == "skipped":
                continue
            if message.metadata.get("status") == "succeeded":
                return False
            return (
                message.metadata.get("status") == "failed"
                and message.metadata.get("provider") is None
                and bool(message.metadata.get("failures"))
            )
        return False

    @staticmethod
    def _failed_read_seen(run: Run, signature: str) -> bool:
        for message in reversed(run.messages):
            if message.role == "user":
                break
            if message.role == "tool" and message.metadata.get("failed_read_signature") == signature:
                return True
        return False

    @staticmethod
    def _failed_fetch_seen(run: Run, signature: str | None) -> bool:
        if not signature:
            return False
        for message in reversed(run.messages):
            if message.role == "user":
                break
            if (
                message.role == "tool" and message.name == "fetch_url"
                and message.metadata.get("status") == "failed"
                and message.metadata.get("failed_fetch_signature") == signature
            ):
                return True
        return False

    def _failed_local_read_signature(self, action: Action) -> str | None:
        if action.tool_name not in {"read_file", "read_pdf"}:
            return None
        existing = self._read_only_signature(action)
        if existing:
            return existing
        path = action.arguments.get("path")
        if not isinstance(path, str):
            return None
        candidate = Path(path).expanduser()
        target = (candidate if candidate.is_absolute() else self._tool_context.workspace_root / candidate).resolve()
        return f"{action.tool_name}:{target}:missing:{json.dumps(action.arguments, sort_keys=True)}"

    def _local_evidence_seen(self, run: Run) -> bool:
        users = [message for message in run.messages if message.role == "user"]
        latest_attachments = users[-1].metadata.get("attachments") if users else []
        file_attachments = [
            item for item in (latest_attachments or [])
            if isinstance(item, dict) and item.get("kind") != "image"
        ]
        latest_text = (users[-1].content or "").casefold() if users else (run.goal or "").casefold()
        if not file_attachments and any(
            term in latest_text for term in ("附件", "上传", "pdf", "attached", "attachment", "uploaded")
        ):
            file_attachments = [
                item for user in users[:-1] for item in (user.metadata.get("attachments") or [])
                if isinstance(item, dict) and item.get("kind") != "image"
            ]
            # A retained earlier read can be reused; only a newly uploaded file
            # requires a fresh read in this user turn.
            prior_attachment = bool(file_attachments)
        else:
            prior_attachment = False
        attachment_paths: set[Path] = set()
        for item in file_attachments:
            raw_path = item.get("storage_path") or item.get("path")
            if not isinstance(raw_path, str) or not raw_path:
                continue
            candidate = Path(raw_path).expanduser()
            if not candidate.is_absolute():
                candidate = self._tool_context.workspace_root / candidate
            attachment_paths.add(candidate.resolve())
        current_turn = next(
            (index for index in range(len(run.messages) - 1, -1, -1) if run.messages[index].role == "user"),
            -1,
        )
        first_message = current_turn + 1 if file_attachments and not prior_attachment else 0
        names = (
            {"read_file", "read_pdf"}
            if file_attachments or local_source_requested(latest_text)
            else {"list_files", "read_file", "read_pdf"}
        )
        for index in range(first_message, len(run.messages)):
            message = run.messages[index]
            if message.role != "tool" or message.name not in names:
                continue
            if message.metadata.get("status") != "succeeded":
                continue
            raw_path = message.metadata.get("path")
            if attachment_paths and (
                not isinstance(raw_path, str) or Path(raw_path).resolve() not in attachment_paths
            ):
                continue
            if message.metadata.get("source_changed_around_read"):
                continue
            if message.name in {"read_file", "read_pdf"} and (
                index < current_turn
                or isinstance(message.metadata.get("source_version"), str)
                or isinstance(message.metadata.get("read_only_signature"), str)
            ) and not self._retained_read_is_current(message):
                continue
            return True
        return False

    @staticmethod
    def _retained_read_is_current(message: ChatMessage) -> bool:
        """Only reuse earlier local evidence while its recorded file version still matches."""
        raw_path = message.metadata.get("path")
        if not isinstance(raw_path, str):
            return False
        read_range = message.metadata.get("read_file_range")
        version = read_range.get("version") if isinstance(read_range, dict) else None
        if not isinstance(version, str):
            signature = message.metadata.get("read_only_signature")
            if not isinstance(signature, str):
                return False
            try:
                prefix, _ = signature.rsplit(":{", 1)
                _, mtime, size = prefix.rsplit(":", 2)
                version = f"{int(mtime)}:{int(size)}"
            except ValueError:
                return False
        try:
            stat = Path(raw_path).stat()
        except OSError:
            return False
        return version == f"{stat.st_mtime_ns}:{stat.st_size}"

    @staticmethod
    def _search_streak(run: Run) -> int:
        """Count searches since the last successfully opened source in this user turn."""
        streak = 0
        for message in reversed(run.messages):
            if message.role == "user":
                break
            if message.role != "tool":
                continue
            status = message.metadata.get("status")
            if message.name == "fetch_url" and status == "succeeded":
                break
            if message.name == "web_search" and (
                status == "succeeded"
                or (status == "skipped" and message.metadata.get("reason") not in {
                    "local_first", "local_only", "search_budget"
                })
            ):
                streak += 1
        return streak

    @staticmethod
    def _similar_search_result(run: Run, action: Action) -> ChatMessage | None:
        raw_query = action.arguments.get("query")
        if not isinstance(raw_query, str):
            return None
        query = re.sub(r"[^\w]+", "", raw_query.casefold())
        # Short queries and changed year/version numbers can have very high
        # character similarity while asking materially different questions.
        if len(query) < 14:
            return None
        current_turn = next(
            (index for index in range(len(run.messages) - 1, -1, -1) if run.messages[index].role == "user"),
            -1,
        )
        for message in reversed(run.messages[current_turn + 1:]):
            if message.role != "tool" or message.name != "web_search":
                continue
            if message.metadata.get("status") != "succeeded":
                continue
            earlier = message.metadata.get("query")
            if not isinstance(earlier, str):
                continue
            previous = re.sub(r"[^\w]+", "", earlier.casefold())
            if len(previous) < 14 or re.findall(r"\d+", query) != re.findall(r"\d+", previous):
                continue
            if SequenceMatcher(None, query, previous).ratio() >= 0.94:
                return message
        return None

    def _read_file_range(self, action: Action) -> dict[str, str | int] | None:
        if action.tool_name != "read_file":
            return None
        path = action.arguments.get("path")
        offset = action.arguments.get("offset", 0)
        max_chars = action.arguments.get("max_chars", 12000)
        if not isinstance(path, str) or type(offset) is not int or type(max_chars) is not int:
            return None
        if offset < 0 or max_chars < 1:
            return None
        candidate = Path(path).expanduser()
        target = (
            candidate.resolve()
            if candidate.is_absolute()
            else (self._tool_context.workspace_root / candidate).resolve()
        )
        try:
            stat = target.stat()
        except OSError:
            return None
        return {
            "path": str(target),
            "version": f"{stat.st_mtime_ns}:{stat.st_size}",
            "offset": offset,
            "end": offset + min(max_chars, MAX_READ_CHARS),
        }

    @staticmethod
    def _covered_read_file_result(
        run: Run, candidate: dict[str, str | int] | None
    ) -> ChatMessage | None:
        if not candidate:
            return None
        current_turn = next(
            (index for index in range(len(run.messages) - 1, -1, -1) if run.messages[index].role == "user"),
            -1,
        )
        reused_action_ids = AgentRunner._rewind_reusable_action_ids(run, current_turn)
        for index in range(len(run.messages) - 1, -1, -1):
            message = run.messages[index]
            if message.role != "tool" or message.name != "read_file" or message.metadata.get("status") != "succeeded":
                continue
            if index <= current_turn and (
                message.tool_call_id not in reused_action_ids
                or not AgentRunner._retained_read_is_current(message)
            ):
                continue
            previous = message.metadata.get("read_file_range")
            if not isinstance(previous, dict):
                continue
            if previous.get("path") != candidate["path"] or previous.get("version") != candidate["version"]:
                continue
            start, end = previous.get("offset"), previous.get("end")
            if type(start) is int and type(end) is int:
                next_offset = message.metadata.get("next_offset")
                actual_end = next_offset if type(next_offset) is int else start + len(message.content or "")
                if start <= candidate["offset"] and (
                    actual_end >= candidate["end"]
                    or (message.metadata.get("truncated") is False and candidate["offset"] <= actual_end)
                ):
                    return message
        return None

    def _completed_read_only_signatures(self, run: Run) -> set[str]:
        current_turn = next(
            (index for index in range(len(run.messages) - 1, -1, -1) if run.messages[index].role == "user"),
            -1,
        )
        reused_action_ids = self._rewind_reusable_action_ids(run, current_turn)
        signatures: set[str] = set()
        for index, message in enumerate(run.messages):
            if message.role != "tool" or message.metadata.get("status") != "succeeded":
                continue
            if index <= current_turn and (
                message.tool_call_id not in reused_action_ids
                or message.name not in {"read_file", "read_pdf"}
                or not self._retained_read_is_current(message)
            ):
                continue
            if message.name == "write_file" and isinstance(message.metadata.get("path"), str):
                target = Path(message.metadata["path"]).resolve()
                prefixes = (
                    f"read_file:{target}:", f"read_pdf:{target}:", f"list_files:{target.parent}:",
                )
                signatures = {signature for signature in signatures if not signature.startswith(prefixes)}
            signature = message.metadata.get("read_only_signature")
            if isinstance(signature, str):
                signatures.add(signature)
        return signatures

    @staticmethod
    def _successful_result_for_signature(run: Run, signature: str | None) -> ChatMessage | None:
        if not signature:
            return None
        current_turn = next(
            (index for index in range(len(run.messages) - 1, -1, -1) if run.messages[index].role == "user"),
            -1,
        )
        reused_action_ids = AgentRunner._rewind_reusable_action_ids(run, current_turn)
        return next((message for index, message in reversed(list(enumerate(run.messages)))
                     if message.role == "tool" and message.metadata.get("status") == "succeeded"
                     and message.metadata.get("read_only_signature") == signature
                     and (index > current_turn or (
                         message.tool_call_id in reused_action_ids
                         and message.name in {"read_file", "read_pdf"}
                         and AgentRunner._retained_read_is_current(message)
                     ))), None)

    @staticmethod
    def _rewind_reusable_action_ids(run: Run, current_turn: int) -> set[str]:
        """Reuse only file reads in phases explicitly retained by this rewind."""
        if current_turn < 0:
            return set()
        feedback = (run.messages[current_turn].content or "").casefold()
        if any(term in feedback for term in _EXPLICIT_REREAD):
            return set()
        revision_id = run.messages[current_turn].metadata.get("rewind_revision_id")
        revision = next((item for item in run.workflow_revisions if item.id == revision_id), None)
        if revision is None:
            return set()
        retained_ids = set(revision.reused_step_ids)
        return {
            action_id
            for step in run.workflow_steps if step.id in retained_ids
            for action_id in step.action_ids
        }

    def _read_only_signature(self, action: Action | None) -> str | None:
        if action is None:
            return None
        if action.tool_name in {"list_files", "read_file", "read_pdf"}:
            path = action.arguments.get("path")
            if not isinstance(path, str):
                return None
            candidate = Path(path).expanduser()
            target = (
                candidate.resolve()
                if candidate.is_absolute()
                else (self._tool_context.workspace_root / candidate).resolve()
            )
            try:
                stat = target.stat()
            except OSError:
                return None
            if action.tool_name == "read_file":
                max_chars = action.arguments.get("max_chars", 12000)
                options = {
                    "offset": action.arguments.get("offset", 0),
                    "max_chars": min(max_chars, MAX_READ_CHARS)
                    if type(max_chars) is int and max_chars > 0 else max_chars,
                }
            elif action.tool_name == "read_pdf":
                max_chars = action.arguments.get("max_chars", 30000)
                options = {
                    "start_page": action.arguments.get("start_page", 1),
                    "page_offset": action.arguments.get("page_offset", 0),
                    "max_pages": action.arguments.get("max_pages"),
                    "max_chars": min(max_chars, MAX_READ_CHARS)
                    if type(max_chars) is int and max_chars > 0 else max_chars,
                }
            else:
                options = {}
            version = f"{stat.st_mtime_ns}:{stat.st_size}"
            return f"{action.tool_name}:{target}:{version}:{json.dumps(options, sort_keys=True)}"
        if action.tool_name == "web_search":
            query = action.arguments.get("query")
            if not isinstance(query, str):
                return None
            normalized_query = re.sub(r"\s+", " ", query).strip().casefold()
            return f"web_search:{normalized_query}"
        if action.tool_name == "fetch_url":
            url = action.arguments.get("url")
            return f"fetch_url:{url.strip()}" if isinstance(url, str) else None
        return None

    def _record_workflow_plan(self, run: Run, action: Action) -> bool:
        """Validate and persist a public plan; never expose hidden model reasoning."""
        planned = self._parse_workflow_items(action.arguments.get("steps", []))
        if not planned:
            return False
        run.workflow_plan = planned
        overview = str(action.arguments.get("summary", "")).strip()
        run.stage_notes["understand"] = overview[:1200] or "已根据目标生成可执行计划。"
        if run.workflow_steps and run.workflow_steps[0].id == "understand":
            run.workflow_steps[0].summary = run.stage_notes["understand"]
            run.workflow_steps[0].status = "completed"
        return True

    @staticmethod
    def _plan_revision_without_new_result(run: Run) -> bool:
        """Avoid consecutive plan changes that do not incorporate new task results."""
        for message in reversed(run.messages):
            if message.role == "user":
                break
            if message.role != "tool":
                continue
            if message.name == "revise_workflow_plan" and message.metadata.get("status") == "succeeded":
                return True
            if message.metadata.get("status") == "succeeded" and message.name not in {
                "declare_workflow_plan", "declare_workflow_step"
            }:
                return False
        return False

    @staticmethod
    def _parse_workflow_items(
        raw_steps: object, *, limit: int = 12, reserved_ids: set[str] | None = None
    ) -> list[WorkflowPlanItem]:
        planned: list[WorkflowPlanItem] = []
        seen: set[str] = set(reserved_ids or ())
        if not isinstance(raw_steps, list) or not raw_steps or len(raw_steps) > limit:
            return []
        for index, value in enumerate(raw_steps):
            if not isinstance(value, dict):
                return []
            raw_title, raw_summary = value.get("title"), value.get("summary")
            raw_done_when, raw_id = value.get("done_when"), value.get("id")
            raw_kind = value.get("kind") or "general"
            if (not isinstance(raw_title, str) or not isinstance(raw_summary, str)
                    or raw_done_when is not None and not isinstance(raw_done_when, str)
                    or raw_id is not None and not isinstance(raw_id, str)
                    or not isinstance(raw_kind, str)):
                return []
            title = raw_title.strip()
            summary = raw_summary.strip()
            done_when = (raw_done_when or "").strip()
            raw_id = (raw_id or "").strip() or f"phase-{index + 1}"
            item_id = raw_id[:64]
            if not title or not summary or item_id in seen:
                return []
            seen.add(item_id)
            planned.append(
                WorkflowPlanItem(
                    id=item_id,
                    title=title[:80],
                    summary=summary[:500],
                    done_when=done_when[:240] or None,
                    kind=raw_kind[:24],
                )
            )
        return planned

    def _revise_workflow_plan(self, run: Run, action: Action) -> dict[str, object] | None:
        reason = action.arguments.get("reason")
        if not isinstance(reason, str) or not reason.strip() or not run.workflow_plan:
            return None
        reached_ids = {step.plan_item_id for step in run.workflow_steps if step.plan_item_id}
        retained = [item for item in run.workflow_plan if item.id in reached_ids]
        future = self._parse_workflow_items(
            action.arguments.get("steps"), limit=12 - len(retained),
            reserved_ids={item.id for item in retained},
        )
        existing_future = [item for item in run.workflow_plan if item.id not in reached_ids]
        if not future or future == existing_future:
            return None
        old_future = [item.title for item in existing_future]
        run.workflow_plan = [*retained, *future]
        return {
            "reason": reason.strip()[:500],
            "kept": [item.title for item in retained],
            "replaced": old_future,
            "future": [item.title for item in future],
        }

    def _activate_workflow_step(self, run: Run, action: Action, message_index: int) -> str:
        raw_title, raw_summary = action.arguments.get("title"), action.arguments.get("summary")
        if not isinstance(raw_title, str) or not isinstance(raw_summary, str):
            return "invalid"
        title = raw_title.strip()
        summary = raw_summary.strip()
        if not title or not summary:
            return "invalid"
        raw_plan_id = action.arguments.get("plan_item_id")
        if raw_plan_id is not None and not isinstance(raw_plan_id, str):
            return "invalid"
        plan_item_id = (raw_plan_id or "").strip() or None
        planned = next((item for item in run.workflow_plan if item.id == plan_item_id), None)
        if planned is None:
            title_matches = [item for item in run.workflow_plan if item.title.casefold() == title.casefold()]
            if len(title_matches) == 1:
                planned = title_matches[0]
            elif plan_item_id or title_matches:
                return "unknown_plan_item"
        effective_plan_id = planned.id if planned else plan_item_id
        current_turn = next(
            (index for index in range(len(run.messages) - 1, -1, -1) if run.messages[index].role == "user"),
            -1,
        )
        if effective_plan_id and any(
            step.plan_item_id == effective_plan_id
            and step.start_message_index is not None
            and step.start_message_index >= current_turn
            for step in run.workflow_steps
        ):
            return "duplicate"
        self._complete_active_step(run)
        run.workflow_steps.append(
            WorkflowStep(
                title=(planned.title if planned else title)[:80],
                summary=(planned.summary if planned else summary)[:500],
                done_when=planned.done_when if planned else None,
                plan_item_id=effective_plan_id,
                kind=(planned.kind if planned else str(action.arguments.get("kind") or "general"))[:24],
                status="active",
                start_message_index=message_index,
            )
        )
        return "activated"

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
                outcomes = [
                    message.metadata.get("status")
                    for message in run.messages
                    if message.role == "tool" and message.tool_call_id in step.action_ids
                ]
                blocked = {"failed", "denied", "cancelled"}
                unworked_external_phase = step.kind in {"research", "files", "external"} and not step.action_ids
                step.status = (
                    "needs_review"
                    if unworked_external_phase or (
                        any(status in blocked for status in outcomes)
                        and not any(status == "succeeded" for status in outcomes)
                    )
                    else "completed"
                )
                step.completed_at = datetime.now()
                return

    @staticmethod
    def _fail_active_step(run: Run) -> None:
        for step in reversed(run.workflow_steps):
            if step.status == "active":
                step.status = "failed"
                step.completed_at = datetime.now()
                return
