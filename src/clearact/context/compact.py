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
    target_units: int | None = None,
) -> list[ChatMessage]:
    """Retain essential messages and recent complete transactions within budget.

    Original system instructions, every user request, and the latest transaction
    are never shortened or split. An intermediate user request may contain an
    enduring constraint, so only older assistant/tool content can be omitted.
    Omitted content is not promoted into a fabricated system summary.
    A target_units value is a soft working-context target; essential messages may
    exceed it provided they still fit the actual budget.
    """
    if keep_recent < 1:
        raise ValueError("keep_recent must be at least one transaction.")
    groups = message_groups(messages)
    required = {index for index, group in enumerate(groups) if group[0].role == "system"}
    required.update(index for index, group in enumerate(groups) if group[0].role == "user")
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
    target = min(budget.limit, target_units) if target_units is not None else budget.limit
    for index in sorted(selected - required):
        if budget.estimate(result, definitions) <= target:
            return result
        selected.remove(index)
        result = retained_messages()
    if budget.exceeds(result, definitions):
        estimate = budget.estimate(result, definitions)
        raise ContextBudgetError(
            f"Essential context exceeds the input budget ({estimate} conservative estimated units, "
            f"limit {budget.limit}). "
            "The system instructions, all user requests, latest tool transaction and tool definitions "
            "cannot be shortened safely. Increase the context window or reduce the request/tool output size.",
            estimated=estimate, limit=budget.limit,
        )
    return result
