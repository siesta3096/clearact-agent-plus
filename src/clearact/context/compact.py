from clearact.context.budget import ContextBudget, ContextBudgetError
from clearact.domain.models import ChatMessage, ToolDefinition


class ContextProtocolError(ValueError):
    """Conversation history contains an incomplete or unmatched tool call."""


def message_groups(messages: list[ChatMessage]) -> list[list[ChatMessage]]:
    """Group each assistant tool plan and all its results into one transaction."""
    groups: list[list[ChatMessage]] = []
    pending: set[str] = set()
    for message in messages:
        if message.role == "tool":
            if message.tool_calls or message.tool_call_id not in pending:
                raise ContextProtocolError(f"Tool result has no pending call: {message.tool_call_id!r}.")
            groups[-1].append(message)
            pending.remove(message.tool_call_id)
            continue
        if pending:
            raise ContextProtocolError(f"Tool calls have no results: {', '.join(sorted(pending))}.")
        groups.append([message])
        if message.tool_calls:
            if message.role != "assistant":
                raise ContextProtocolError("Only assistant messages can declare tool calls.")
            pending = {action.id for action in message.tool_calls}
            if len(pending) != len(message.tool_calls):
                raise ContextProtocolError("A tool plan contains duplicate call IDs.")
    if pending:
        raise ContextProtocolError(f"Tool calls have no results: {', '.join(sorted(pending))}.")
    return groups


def compact_messages(
    messages: list[ChatMessage],
    keep_recent: int = 6,
    *,
    budget: ContextBudget | None = None,
    tools: list[ToolDefinition] | None = None,
) -> list[ChatMessage]:
    """Retain essential messages and recent complete transactions within budget.

    Original system instructions, the original and latest user requests, and
    the latest transaction are never shortened or split. Older content is
    omitted, not promoted into a fabricated system summary.
    """
    if keep_recent < 1:
        raise ValueError("keep_recent must be at least one transaction.")
    groups = message_groups(messages)
    required = {index for index, group in enumerate(groups) if group[0].role == "system"}
    user_groups = [index for index, group in enumerate(groups) if group[0].role == "user"]
    if user_groups:
        required.update((user_groups[0], user_groups[-1]))
    tool_groups = [index for index, group in enumerate(groups) if group[0].tool_calls]
    if tool_groups:
        required.add(tool_groups[-1])
    if groups:
        required.add(len(groups) - 1)
    selected = required | set(range(max(0, len(groups) - keep_recent), len(groups)))

    def retained_messages() -> list[ChatMessage]:
        return [message for index, group in enumerate(groups) if index in selected for message in group]

    result = retained_messages()
    if budget is None:
        return result
    definitions = tools or []
    for index in sorted(selected - required):
        if not budget.exceeds(result, definitions):
            return result
        selected.remove(index)
        result = retained_messages()
    if budget.exceeds(result, definitions):
        estimate = budget.estimate(result, definitions)
        raise ContextBudgetError(
            f"Essential context exceeds the input budget ({estimate} estimated tokens, limit {budget.limit}). "
            "The system instructions, original/latest request, latest tool transaction and tool definitions "
            "cannot be shortened safely. Increase the context window or reduce the request/tool output size."
        )
    return result
