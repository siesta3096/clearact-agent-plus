from dataclasses import dataclass
from typing import Literal

from clearact.context.budget import ContextBudget
from clearact.context.compact import compact_messages, message_groups
from clearact.context.tool_filter import ToolFilter
from clearact.domain.models import ChatMessage, ToolDefinition


@dataclass(frozen=True)
class ContextBuildReport:
    reasoning_messages_omitted: int = 0
    reasoning_characters_omitted: int = 0
    estimated_before: int = 0
    estimated_after: int = 0
    history_messages_omitted: int = 0


class ContextBuilder:
    def __init__(self, budget: ContextBudget, tool_filter: ToolFilter | None = None, *,
                 reasoning_history: Literal["auto", "full"] = "auto", history_target_units: int = 32_000) -> None:
        if reasoning_history not in ("auto", "full"):
            raise ValueError("reasoning_history must be auto or full")
        if history_target_units < 1:
            raise ValueError("history_target_units must be positive")
        self._budget = budget
        self._tool_filter = tool_filter or ToolFilter()
        self._reasoning_history = reasoning_history
        self._history_target_units = min(history_target_units, budget.limit)
        self.last_build_report = ContextBuildReport()

    def build(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        recent_tool_names: set[str],
    ) -> tuple[list[ChatMessage], list[ToolDefinition]]:
        selected_tools = self._tool_filter.select(tools, recent_tool_names)
        self.last_build_report = ContextBuildReport()
        # Reject invalid history even when it is small enough to avoid compaction.
        message_groups(messages)
        estimated_before = self._budget.estimate(messages, selected_tools)
        estimated_after = estimated_before
        selected_messages = messages
        # History is a durable ledger; the model request is a bounded working view.
        # Strict reasoning-replay services can opt into full without rewriting that ledger.
        reasoning = [message for message in messages
                     if self._reasoning_history == "auto" and message.role == "assistant" and message.reasoning_content]
        if reasoning:
            selected_messages = [message.model_copy(update={"reasoning_content": None})
                                 if message.role == "assistant" and message.reasoning_content else message
                                 for message in messages]
            estimated_after = self._budget.estimate(selected_messages, selected_tools)
        if estimated_after > self._history_target_units:
            selected_messages = compact_messages(selected_messages, budget=self._budget, tools=selected_tools,
                                                 target_units=self._history_target_units)
            estimated_after = self._budget.estimate(selected_messages, selected_tools)
        self.last_build_report = ContextBuildReport(
            reasoning_messages_omitted=len(reasoning),
            reasoning_characters_omitted=sum(len(message.reasoning_content) for message in reasoning),
            estimated_before=estimated_before,
            estimated_after=estimated_after,
            history_messages_omitted=len(messages) - len(selected_messages),
        )
        return selected_messages, selected_tools
