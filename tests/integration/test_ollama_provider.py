import asyncio
import json

import httpx

from clearact.domain.models import ChatMessage, ToolDefinition
from clearact.providers.ollama import OllamaProvider


def test_ollama_provider_translates_native_tool_calls(monkeypatch):
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            request=request,
            json={
                "message": {
                    "content": "I will create it.",
                    "tool_calls": [
                        {"id": "call_123", "function": {"name": "write_file", "arguments": {"path": "answer.txt"}}}
                    ],
                },
                "prompt_eval_count": 12,
                "eval_count": 4,
                "done_reason": "stop",
            },
        )

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            assert timeout == 90

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json, headers=None):
            captured["headers"] = headers
            return await handler(httpx.Request("POST", url, json=json, headers=headers))

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    provider = OllamaProvider("qwen", "http://127.0.0.1:11434/", api_key="local-secret")
    tools = [ToolDefinition(name="write_file", description="write", parameters={"type": "object"})]

    response = asyncio.run(provider.chat([ChatMessage(role="user", content="create a file")], tools))

    assert response.content == "I will create it."
    assert response.tool_calls[0].id == "call_123"
    assert response.tool_calls[0].tool_name == "write_file"
    assert response.tool_calls[0].arguments == {"path": "answer.txt"}
    assert response.usage == {"prompt_tokens": 12, "completion_tokens": 4}
    assert captured["headers"] == {"Authorization": "Bearer local-secret"}
