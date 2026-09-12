from __future__ import annotations

import json

import httpx

from clearact.domain.models import Action, ChatMessage, LLMResponse, ToolDefinition
from clearact.providers.attachments import openai_message_payload


class OpenAICompatibleProvider:
    def __init__(self, model: str, base_url: str, api_key: str) -> None:
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    async def summarize_stage(self, instruction: str, goal: str) -> str:
        """Generate a public process summary without exposing private reasoning traces."""
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": instruction},
                {"role": "user", "content": goal},
            ],
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(f"{self._base_url}/chat/completions", json=payload, headers=headers)
            response.raise_for_status()
        return response.json()["choices"][0]["message"].get("content") or ""

    @staticmethod
    def _message_payload(message: ChatMessage) -> dict:
        # reasoning_content is intentionally retained in history. Compatible
        # providers that support thinking-mode tool calls (for example DeepSeek)
        # use it to preserve the returned reasoning trace between tool turns.
        payload = openai_message_payload(message)
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
        return payload

    async def chat(self, messages: list[ChatMessage], tools: list[ToolDefinition]) -> LLMResponse:
        payload = {
            "model": self._model,
            "messages": [self._message_payload(message) for message in messages],
            "tools": [{"type": "function", "function": tool.model_dump()} for tool in tools],
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(f"{self._base_url}/chat/completions", json=payload, headers=headers)
            # Most reasoning-capable compatible APIs expect returned reasoning
            # in tool-call history. Some strict APIs reject the non-standard
            # field, so retry once without it while retaining the local record.
            if response.status_code == 400 and any(message.reasoning_content for message in messages):
                fallback_payload = dict(payload)
                fallback_payload["messages"] = [
                    {key: value for key, value in item.items() if key != "reasoning_content"}
                    for item in payload["messages"]
                ]
                response = await client.post(
                    f"{self._base_url}/chat/completions", json=fallback_payload, headers=headers
                )
            response.raise_for_status()
        data = response.json()
        choice = data["choices"][0]
        message = choice["message"]
        calls = []
        for call in message.get("tool_calls", []):
            action_data = {
                "tool_name": call["function"]["name"],
                "arguments": json.loads(call["function"]["arguments"]),
            }
            if call.get("id"):
                action_data["id"] = call["id"]
            calls.append(Action(**action_data))
        usage = data.get("usage", {})
        reasoning = message.get("reasoning_content") or message.get("reasoning")
        if isinstance(reasoning, list):
            reasoning = "\n".join(
                str(block.get("text") or block.get("content") or "") for block in reasoning if isinstance(block, dict)
            )
        return LLMResponse(
            content=message.get("content"),
            reasoning_content=reasoning if isinstance(reasoning, str) and reasoning.strip() else None,
            tool_calls=calls,
            usage={
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
            },
            finish_reason=choice.get("finish_reason"),
        )
