import asyncio
import json

import httpx
import pytest

from clearact import cli, webapp
from clearact.domain.enums import RunStatus
from clearact.domain.models import Action, ChatMessage, LLMResponse, Run
from clearact.providers.ollama import OllamaProvider
from clearact.providers.openai_compatible import OpenAICompatibleProvider
from clearact.runtime.event_bus import EventBus
from clearact.runtime.model_retry import ModelRequestError
from clearact.settings import _model_settings
from tests.integration.test_runner import FlakyProvider, build_runner


@pytest.mark.parametrize("kind", ["ollama", "openai_compatible"])
@pytest.mark.parametrize("seconds", [None, 1800])
def test_configured_model_wait_reaches_real_http_transport_without_slow_connects(monkeypatch, kind, seconds):
    raw = {"provider": kind, "model": "test", "baseUrl": "http://model.test", "contextWindow": 32768,
           "apiKey": "test-key"}
    if seconds is not None:
        raw["requestTimeoutSeconds"] = seconds
    profile = _model_settings({"defaultProfile": "test", "profiles": {"test": raw}})["profiles"]["test"]
    provider = cli._build_provider(profile)
    received = []

    def handler(request):
        received.append(request)
        assert request.extensions["timeout"] == {"connect": 10, "read": seconds or 600, "write": 30, "pool": 10}
        message = {"content": "Completed after generation"}
        payload = {"message": message} if kind == "ollama" else {"choices": [{"message": message}]}
        return httpx.Response(200, json=payload)

    client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(transport=transport, **kwargs))
    result = asyncio.run(provider.chat([ChatMessage(role="user", content="Generate a long answer")], []))
    assert result.content == "Completed after generation"
    assert len(received) == 1


@pytest.mark.parametrize("bad", [0, -1, 7201, True, "600", None, float("inf"), float("nan"), 10**1000])
def test_invalid_model_wait_is_rejected_before_connecting(bad):
    with pytest.raises(ValueError, match="requestTimeoutSeconds"):
        OpenAICompatibleProvider("test", "http://model.test", "key", request_timeout_seconds=bad)
    with pytest.raises(ValueError, match="requestTimeoutSeconds"):
        OllamaProvider("test", "http://model.test", request_timeout_seconds=bad)


@pytest.mark.parametrize("language", ["zh", "en"])
def test_read_timeout_does_not_queue_duplicate_generations_or_replay_completed_writes(workspace, language):
    request = httpx.Request("POST", "http://model.test/v1/chat/completions")
    write = Action(id="write-once", tool_name="write_file", arguments={"path": "kept.txt", "content": "kept"})
    provider = FlakyProvider([LLMResponse(tool_calls=[write]), httpx.ReadTimeout("still generating", request=request),
                              LLMResponse(content="Must not be requested automatically")])
    events = []
    bus = EventBus()
    bus.subscribe_sync(events.append)
    runner = build_runner(workspace, provider, event_bus=bus)
    run = Run(goal="Create a file")
    run.execution.interface_language = language
    expected = "未自动重发" if language == "zh" else "not resent"
    with pytest.raises(ModelRequestError, match=expected):
        asyncio.run(runner.run(run))
    assert provider.calls == 2
    assert run.status is RunStatus.FAILED
    assert (workspace / "kept.txt").read_text(encoding="utf-8") == "kept"
    assert not any(event.type == "model.retrying" for event in events)
    assert sum(event.type == "model.waiting" for event in events) == 2
    assert sum(event.type == "model.responded" for event in events) == 1


def test_user_can_cancel_while_waiting_without_a_second_request(workspace):
    async def scenario():
        entered = asyncio.Event()

        class WaitingProvider:
            request_timeout_seconds = 600
            calls = 0

            async def chat(self, messages, tools):
                self.calls += 1
                entered.set()
                await asyncio.Future()

        provider = WaitingProvider()
        events = []
        bus = EventBus()
        bus.subscribe_sync(events.append)
        runner = build_runner(workspace, provider, event_bus=bus)
        run = Run(goal="Wait for a long response")
        task = asyncio.create_task(runner.run(run))
        await entered.wait()
        assert run.status is RunStatus.RUNNING
        assert events[-1].type == "model.waiting"
        assert events[-1].data["timeout_seconds"] == 600
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert run.status is RunStatus.CANCELLED
        assert provider.calls == 1
        assert not any(event.type == "model.retrying" for event in events)

    asyncio.run(scenario())


def test_invalid_timeout_in_settings_api_does_not_modify_saved_config(tmp_path, monkeypatch):
    config = {"defaultProfile": "test", "profiles": {"test": {"model": "original"}}}
    path = tmp_path / "clearact.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(webapp.cli, "_project_root", lambda: tmp_path)
    update = webapp.SettingsRequest(default_profile="test", default_autonomy="green", max_iterations=80,
                                   max_tool_calls=240, profiles={"test": {"requestTimeoutSeconds": -1}})
    with pytest.raises(webapp.HTTPException) as error:
        asyncio.run(webapp.update_settings(update))
    assert error.value.status_code == 422
    assert json.loads(path.read_text(encoding="utf-8")) == config
