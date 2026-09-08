import asyncio
import json

import pytest

from clearact import webapp


def test_settings_endpoint_hides_api_keys_and_updates_safe_fields(tmp_path, monkeypatch):
    config = {
        "defaultProfile": "demo",
        "profiles": {
            "demo": {
                "provider": "ollama",
                "model": "old",
                "baseUrl": "http://localhost:1",
                "contextWindow": 10,
                "apiKey": "secret",
            }
        },
        "agent": {"maxIterations": 2, "maxToolCallsPerRun": 3},
        "workspace": {"defaultRoot": "workspace"},
        "storage": {"dataRoot": "data"},
        "network": {},
        "policy": {"defaultAutonomy": "green", "defaultViewMode": "simple"},
        "web": {"host": "127.0.0.1", "port": 9999},
    }
    (tmp_path / "config").mkdir()
    (tmp_path / "workspace").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "config" / "tools.yaml").write_text("tools: {}", encoding="utf-8")
    (tmp_path / "config" / "risk_rules.yaml").write_text("{}", encoding="utf-8")
    (tmp_path / "clearact.json").write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(webapp.cli, "_project_root", lambda: tmp_path)

    visible = asyncio.run(webapp.get_settings())
    assert "apiKey" not in visible["profiles"]["demo"]

    request = webapp.SettingsRequest(
        default_autonomy="red",
        max_iterations=20,
        max_tool_calls=30,
        default_profile="demo",
        profiles={"demo": {"model": "new", "baseUrl": "http://localhost:2", "contextWindow": 20}},
    )
    assert asyncio.run(webapp.update_settings(request)) == {"saved": True}
    saved = json.loads((tmp_path / "clearact.json").read_text(encoding="utf-8"))
    assert saved["profiles"]["demo"]["apiKey"] == "secret"
    assert saved["profiles"]["demo"]["model"] == "new"


def test_mcp_import_accepts_standard_shape_and_hides_secrets(tmp_path, monkeypatch):
    config = {
        "defaultProfile": "demo",
        "profiles": {"demo": {"provider": "ollama", "model": "d", "baseUrl": "http://localhost", "contextWindow": 10}},
        "agent": {"maxIterations": 2, "maxToolCallsPerRun": 3},
        "workspace": {"defaultRoot": "workspace"},
        "storage": {"dataRoot": "data"},
        "network": {},
        "policy": {"defaultAutonomy": "green", "defaultViewMode": "simple"},
        "web": {},
    }
    (tmp_path / "config").mkdir()
    (tmp_path / "workspace").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "config" / "tools.yaml").write_text("tools: {}", encoding="utf-8")
    (tmp_path / "config" / "risk_rules.yaml").write_text("{}", encoding="utf-8")
    (tmp_path / "clearact.json").write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(webapp.cli, "_project_root", lambda: tmp_path)
    response = asyncio.run(
        webapp.import_mcp_servers(
            webapp.MCPImportRequest(
                config={
                    "mcpServers": {"demo": {"command": "python", "args": ["-m", "demo"], "env": {"TOKEN": "secret"}}}
                }
            )
        )
    )
    assert response["imported"] == ["demo"]
    assert response["servers"]["demo"]["envKeys"] == ["TOKEN"]
    assert "secret" not in json.dumps(response)
    saved = json.loads((tmp_path / "clearact.json").read_text(encoding="utf-8"))
    assert saved["mcp"]["servers"]["demo"]["env"]["TOKEN"] == "secret"


def test_rewind_request_discards_selected_step_and_later_history(tmp_path, monkeypatch):
    from clearact.domain.models import Action, ChatMessage, Run, UserPolicy
    from clearact.storage.run_store import RunStore

    config = {
        "defaultProfile": "demo",
        "profiles": {
            "demo": {
                "provider": "ollama",
                "model": "demo",
                "baseUrl": "http://localhost:1",
                "contextWindow": 10,
            }
        },
        "agent": {"maxIterations": 2, "maxToolCallsPerRun": 3},
        "workspace": {"defaultRoot": "workspace"},
        "storage": {"dataRoot": "data"},
        "network": {},
        "policy": {"defaultAutonomy": "green", "defaultViewMode": "simple"},
        "web": {},
    }
    (tmp_path / "config").mkdir()
    (tmp_path / "workspace").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "config" / "tools.yaml").write_text("tools: {}", encoding="utf-8")
    (tmp_path / "config" / "risk_rules.yaml").write_text("{}", encoding="utf-8")
    (tmp_path / "clearact.json").write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(webapp.cli, "_project_root", lambda: tmp_path)
    first = Action(tool_name="web_search", arguments={"query": "first"})
    second = Action(tool_name="write_file", arguments={"path": "a"})
    run = Run(
        goal="report",
        policy=UserPolicy(),
        messages=[
            ChatMessage(role="system", content="s"),
            ChatMessage(role="user", content="original"),
            ChatMessage(role="assistant", tool_calls=[first]),
            ChatMessage(role="tool", tool_call_id=first.id, name="web_search", content="result"),
            ChatMessage(role="assistant", tool_calls=[second]),
        ],
    )
    store = RunStore(tmp_path / "data")
    store.save_run(run)

    async def fake_run(*args, **kwargs):
        return None

    monkeypatch.setattr(webapp.cli, "_run", fake_run)

    request = webapp.StartRunRequest(
        goal="Use official sources instead.",
        run_id=run.id,
        rewind_action_id=first.id,
    )
    response = asyncio.run(webapp.start_run(request))

    saved = store.load_run(run.id)
    assert response["run_id"] == run.id
    assert [message.content for message in saved.messages] == ["s", "original", "Use official sources instead."]


def test_runs_endpoint_rejects_unbounded_limits(monkeypatch):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as error:
        asyncio.run(webapp.runs(limit=101))
    assert error.value.status_code == 422
