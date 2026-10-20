from dataclasses import dataclass, replace
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


class ContextBuilder:
    def __init__(self, budget: ContextBudget, tool_filter: ToolFilter | None = None, *,
                 reasoning_history: Literal["auto", "full"] = "auto") -> None:
        if reasoning_history not in ("auto", "full"):
            raise ValueError("reasoning_history must be auto or full")
        self._budget = budget
        self._tool_filter = tool_filter or ToolFilter()
        self._reasoning_history = reasoning_history
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
        selected_messages = messages
        if self._budget.exceeds(selected_messages, selected_tools):
            # Returned thinking is diagnostic history, not a tool result or user constraint.
            # Remove it from request copies before discarding useful code/results. Keep full
            # ledger messages intact. Strict reasoning-replay services can opt into full.
            if self._reasoning_history == "auto":
                reasoning = [message for message in messages
                             if message.role == "assistant" and message.reasoning_content]
                if reasoning:
                    selected_messages = [message.model_copy(update={"reasoning_content": None})
                                         if message.role == "assistant" and message.reasoning_content else message
                                         for message in messages]
                    self.last_build_report = ContextBuildReport(
                        reasoning_messages_omitted=len(reasoning),
                        reasoning_characters_omitted=sum(len(message.reasoning_content) for message in reasoning),
                        estimated_before=self._budget.estimate(messages, selected_tools),
                        estimated_after=self._budget.estimate(selected_messages, selected_tools),
                    )
        if self._budget.exceeds(selected_messages, selected_tools):
            selected_messages = compact_messages(selected_messages, budget=self._budget, tools=selected_tools)
        if self.last_build_report.reasoning_messages_omitted:
            self.last_build_report = replace(self.last_build_report,
                                             estimated_after=self._budget.estimate(selected_messages, selected_tools))
        return selected_messages, selected_tools
