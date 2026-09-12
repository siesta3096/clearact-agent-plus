from __future__ import annotations

import httpx

from clearact.domain.models import Action, ChatMessage, LLMResponse, ToolDefinition


class OllamaProvider:
    def __init__(self, model: str, base_url: str, api_key: str = "") -> None:
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    @staticmethod
    def _message_payload(message: ChatMessage) -> dict:
        payload = message.model_dump(exclude={"tool_calls"}, exclude_none=True)
        if message.tool_calls:
            payload["tool_calls"] = [
                {"function": {"name": action.tool_name, "arguments": action.arguments}} for action in message.tool_calls
            ]
        return payload

    async def chat(self, messages: list[ChatMessage], tools: list[ToolDefinition]) -> LLMResponse:
        payload = {
            "model": self._model,
            "stream": False,
            "messages": [self._message_payload(message) for message in messages],
            "tools": [{"type": "function", "function": tool.model_dump()} for tool in tools],
        }
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else None
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(f"{self._base_url}/api/chat", json=payload, headers=headers)
            response.raise_for_status()
        data = response.json()
        message = data.get("message", {})
        calls = []
        for call in message.get("tool_calls", []):
            action_data = {
                "tool_name": call["function"]["name"],
                "arguments": call["function"].get("arguments", {}),
            }
            if call.get("id"):
                action_data["id"] = call["id"]
            calls.append(Action(**action_data))
        return LLMResponse(
            content=message.get("content"),
            # Ollama exposes model thinking in this native field when supported.
            reasoning_content=message.get("thinking") or message.get("reasoning_content"),
            tool_calls=calls,
            usage={"prompt_tokens": data.get("prompt_eval_count", 0), "completion_tokens": data.get("eval_count", 0)},
            finish_reason=data.get("done_reason"),
        )
