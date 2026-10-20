from clearact.context.budget import ContextBudget, ContextBudgetError
from clearact.domain.models import ChatMessage, ToolDefinition


class ContextProtocolError(ValueError):
    """Conversation history contains an incomplete or unmatched tool call."""


def working_read_calls(messages: list[ChatMessage], budget: ContextBudget, limit: int) -> set[str]:
    """Retain a bounded set of recently inspected files in the current user turn."""
    boundary = max((index for index, message in enumerate(messages) if message.role == "user"), default=-1)
    selected, paths, used = set(), set(), 0
    for group in reversed(message_groups(messages[boundary + 1:])):
        reads = [message for message in group if message.role == "tool" and message.name == "read_file"
                 and message.metadata.get("status") == "succeeded"
                 and isinstance(message.metadata.get("path"), str)]
        fresh = [message for message in reads if message.metadata["path"] not in paths]
        paths.update(message.metadata["path"] for message in reads)
        cost = budget.estimate(group, [])
        if fresh and used + cost <= limit:
            selected.update(message.tool_call_id for message in fresh)
            used += cost
    return selected


def excerpt_older_reads(messages: list[ChatMessage], protected: set[str] | None = None
                        ) -> tuple[list[ChatMessage], int, int]:
    """Keep located excerpts of older native text reads before removing whole transactions.

    The latest tool transaction is untouched. Original messages stay in the ledger;
    excerpts retain their tool role/call ID and point to missing source coordinates.
    """
    latest = max((index for index, message in enumerate(messages) if message.tool_calls), default=-1)
    actions = {action.id: action for message in messages for action in message.tool_calls}
    result, count, omitted = [], 0, 0
    for index, message in enumerate(messages):
        content = message.content or ""
        action = actions.get(message.tool_call_id or "")
        offset = action.arguments.get("offset", 0) if action else None
        if (index < latest and message.role == "tool" and message.name == "read_file"
                and message.tool_call_id not in (protected or set())
                and message.metadata.get("status") == "succeeded" and len(content) > 1600
                and action and action.tool_name == "read_file" and type(offset) is int):
            missing = len(content) - 800
            note = (f"\n[Earlier read_file excerpt: {missing} characters omitted. Original record retained. "
                    f"Re-read the same path at offset {offset + 600} for the missing middle; "
                    "do not infer omitted content.]\n")
            result.append(message.model_copy(update={
                "content": content[:600] + note + content[-200:],
                "metadata": {**message.metadata, "context_excerpted": True},
            }))
            count += 1
            omitted += missing
        else:
            result.append(message)
    return result, count, omitted


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
    protected_calls: set[str] | None = None,
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
    required.update(index for index, group in enumerate(groups)
                    if any(message.tool_call_id in (protected_calls or set()) for message in group))
    tool_groups = [index for index, group in enumerate(groups) if group[0].tool_calls]
    if tool_groups:
        required.add(tool_groups[-1])
    if groups:
        required.add(len(groups) - 1)
    excerpts = {index for index, group in enumerate(groups)
                if any(message.metadata.get("context_excerpted") for message in group)}
    selected = required | excerpts | set(range(max(0, len(groups) - keep_recent), len(groups)))

    def retained_messages() -> list[ChatMessage]:
        return [message for index, group in enumerate(groups) if index in selected for message in group]

    result = retained_messages()
    if budget is None:
        return result
    definitions = tools or []
    target = min(budget.limit, target_units) if target_units is not None else budget.limit
    for index in sorted(selected - required - excerpts):
        if budget.estimate(result, definitions) <= target:
            return result
        selected.remove(index)
        result = retained_messages()
    # Located source facts are worth retaining beyond the soft target, but remain
    # optional if required instructions and the latest transaction need the hard limit.
    for index in sorted((selected & excerpts) - required):
        if not budget.exceeds(result, definitions):
            break
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
