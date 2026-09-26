import json

from clearact.domain.models import ChatMessage, ToolDefinition
from clearact.providers.attachments import attachment_prompt


class ContextBudgetError(ValueError):
    """The essential conversation cannot fit the configured input budget."""


class ContextBudget:
    def __init__(self, context_window: int, ratio: float) -> None:
        if context_window <= 0 or not 0 < ratio < 1:
            raise ValueError("Context window must be positive and the input budget ratio must be between 0 and 1.")
        self._limit = int(context_window * ratio)
        if self._limit < 1:
            raise ValueError("The configured input context budget must be at least one token.")

    @property
    def limit(self) -> int:
        return self._limit

    def estimate(self, messages: list[ChatMessage], tools: list[ToolDefinition]) -> int:
        # Providers use different tokenizers. UTF-8 bytes give a deliberately
        # conservative estimate, including CJK, reasoning and tool arguments.
        # Runtime metadata is not sent to the model; attached file references
        # are converted to user-visible prompt text before provider submission.
        # Count the larger OpenAI-compatible tool-call shape,
        # including JSON-string escaping of arguments, rather than content alone.
        payload_messages = []
        for message in messages:
            payload = message.model_dump(mode="json", exclude={"tool_calls", "metadata"}, exclude_none=True)
            if message.role == "user" and message.metadata.get("attachments"):
                payload["content"] = attachment_prompt(message)
            if message.tool_calls:
                payload["tool_calls"] = [
                    {
                        "id": action.id,
                        "type": "function",
                        "function": {
                            "name": action.tool_name,
                            "arguments": json.dumps(action.arguments, ensure_ascii=False),
                        },
                    }
                    for action in message.tool_calls
                ]
            payload_messages.append(payload)
        request = {
            "messages": payload_messages,
            "tools": [{"type": "function", "function": tool.model_dump(mode="json")} for tool in tools],
        }
        serialized = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
        # Reserve protocol framing on top of the serialized fields. The ratio
        # reserves the rest of the context window for model output.
        return len(serialized.encode("utf-8")) + 16 * len(messages)

    def exceeds(self, messages: list[ChatMessage], tools: list[ToolDefinition]) -> bool:
        return self.estimate(messages, tools) > self._limit
