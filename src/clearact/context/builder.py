from clearact.context.budget import ContextBudget
from clearact.context.compact import compact_messages, message_groups
from clearact.context.tool_filter import ToolFilter
from clearact.domain.models import ChatMessage, ToolDefinition


class ContextBuilder:
    def __init__(self, budget: ContextBudget, tool_filter: ToolFilter | None = None) -> None:
        self._budget = budget
        self._tool_filter = tool_filter or ToolFilter()

    def build(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        recent_tool_names: set[str],
    ) -> tuple[list[ChatMessage], list[ToolDefinition]]:
        selected_tools = self._tool_filter.select(tools, recent_tool_names)
        # Reject invalid history even when it is small enough to avoid compaction.
        message_groups(messages)
        selected_messages = messages
        if self._budget.exceeds(selected_messages, selected_tools):
            selected_messages = compact_messages(selected_messages, budget=self._budget, tools=selected_tools)
        return selected_messages, selected_tools
