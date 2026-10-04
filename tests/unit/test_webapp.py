import asyncio
import json

import pytest

from clearact import webapp
from clearact.domain.enums import RiskLevel
from clearact.domain.models import Action, RiskAssessment


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

    friendly = asyncio.run(
        webapp.save_mcp_server(
            webapp.MCPServerRequest(
                name="remote",
                transport="streamable_http",
                url="https://example.test/mcp",
                secret_kind="header",
                secret_name="Authorization",
                secret_value="Bearer private",
            )
        )
    )
    assert friendly["server"]["headerKeys"] == ["Authorization"]
    assert "private" not in json.dumps(friendly)
    asyncio.run(webapp.set_mcp_server_enabled("remote", False))
    saved = json.loads((tmp_path / "clearact.json").read_text(encoding="utf-8"))
    assert saved["mcp"]["servers"]["remote"]["enabled"] is False
    assert asyncio.run(webapp.delete_mcp_server("remote")) == {"deleted": True}


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


def test_settings_endpoint_returns_profile_fields_for_form_prefill(tmp_path, monkeypatch):
    config = {
        "defaultProfile": "demo",
        "profiles": {
            "demo": {
                "provider": "openai",
                "model": "gpt",
                "baseUrl": "https://api.example",
                "contextWindow": 4096,
                "apiKey": "secret",
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
    profile = asyncio.run(webapp.get_settings())["profiles"]["demo"]
    assert profile == {
        "provider": "openai",
        "model": "gpt",
        "baseUrl": "https://api.example",
        "contextWindow": 4096,
        "hasApiKey": True,
    }


def test_start_request_defers_to_configured_autonomy():
    request = webapp.StartRunRequest(goal="test")

    assert request.autonomy is None
    assert webapp._effective_autonomy(request, RiskLevel.GREEN) is RiskLevel.GREEN
    assert (
        webapp._effective_autonomy(webapp.StartRunRequest(goal="test", autonomy="yellow"), RiskLevel.GREEN)
        is RiskLevel.YELLOW
    )


def test_web_approval_gate_resumes_with_the_submitted_decision():
    async def execute():
        gate = webapp.WebApprovalGate("run_test")
        action = Action(id="act_test", tool_name="write_file", arguments={"path": "report.txt"})
        assessment = RiskAssessment(level=RiskLevel.YELLOW, reasons=["modifies a file"])
        waiting = asyncio.create_task(gate.request(action, assessment))
        await asyncio.sleep(0)

        pending = webapp._pending_approvals["run_test"]
        assert pending["action"] == action
        response = await webapp.decide_approval(
            "run_test", webapp.ApprovalDecisionRequest(action_id=action.id, approved=True)
        )
        assert response == {"accepted": True, "approved": True}
        assert await waiting is True
        assert "run_test" not in webapp._pending_approvals

    asyncio.run(execute())


def test_rewind_rolls_back_discarded_file_writes_and_flags_external_effects(tmp_path):
    from clearact.domain.models import ChatMessage, Run
    from clearact.storage.snapshots import SnapshotStore

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "report.txt"
    target.write_text("before", encoding="utf-8")
    snapshots = SnapshotStore(tmp_path / "data", workspace)
    snapshot_id = snapshots.save_before_write(target, "before", existed=True)
    target.write_text("after", encoding="utf-8")
    write = Action(id="write", tool_name="write_file", arguments={"path": "report.txt", "content": "after"})
    remote = Action(id="remote", tool_name="mcp__demo__publish", arguments={})
    run = Run(
        goal="revise",
        messages=[
            ChatMessage(role="assistant", tool_calls=[write, remote]),
            ChatMessage(
                role="tool",
                tool_call_id=write.id,
                name=write.tool_name,
                content="updated",
                metadata={"status": "succeeded", "snapshot_id": snapshot_id},
            ),
            ChatMessage(
                role="tool",
                tool_call_id=remote.id,
                name=remote.tool_name,
                content="published",
                metadata={"status": "succeeded"},
            ),
        ],
    )

    restored, warnings = webapp._rollback_discarded_effects(run, 0, snapshots)

    assert restored == [snapshot_id]
    assert target.read_text(encoding="utf-8") == "before"
    assert warnings == [f"External effect may remain: {remote.tool_name} ({remote.id})"]


def test_rewind_does_not_restore_a_snapshot_for_a_different_write_target(tmp_path):
    from clearact.domain.models import ChatMessage, Run
    from clearact.storage.snapshots import SnapshotStore

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = workspace / "first.txt"
    second = workspace / "second.txt"
    first.write_text("before first", encoding="utf-8")
    snapshots = SnapshotStore(tmp_path / "data", workspace)
    snapshot_id = snapshots.save_before_write(first, "before first", existed=True)
    first.write_text("agent version", encoding="utf-8")
    second.write_text("agent version", encoding="utf-8")
    write = Action(id="write", tool_name="write_file", arguments={
        "path": "second.txt", "content": "agent version",
    })
    run = Run(goal="revise", messages=[
        ChatMessage(role="assistant", tool_calls=[write]),
        ChatMessage(role="tool", name="write_file", tool_call_id=write.id, metadata={
            "status": "succeeded", "path": str(second), "snapshot_id": snapshot_id,
        }),
    ])

    restored, warnings = webapp._rollback_discarded_effects(run, 0, snapshots)

    assert restored == []
    assert "Snapshot path does not match" in warnings[0]
    assert first.read_text(encoding="utf-8") == "agent version"
    assert second.read_text(encoding="utf-8") == "agent version"


def test_rewind_preview_and_restore_leave_newer_user_edits_untouched(tmp_path):
    from clearact.domain.models import ChatMessage, Run
    from clearact.storage.snapshots import SnapshotStore

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "report.txt"
    target.write_text("before", encoding="utf-8")
    snapshots = SnapshotStore(tmp_path / "data", workspace)
    snapshot_id = snapshots.save_before_write(target, "before", existed=True)
    target.write_text("user edit", encoding="utf-8")
    action = Action(tool_name="write_file", arguments={"path": "report.txt", "content": "agent version"})
    run = Run(goal="report", messages=[
        ChatMessage(role="assistant", tool_calls=[action]),
        ChatMessage(role="tool", name="write_file", tool_call_id=action.id,
                    metadata={"status": "succeeded", "snapshot_id": snapshot_id}),
    ])

    preview, warnings = webapp._rollback_discarded_effects(run, 0, snapshots, dry_run=True)
    restored, actual_warnings = webapp._rollback_discarded_effects(run, 0, snapshots)

    assert preview == restored == []
    assert warnings == actual_warnings
    assert "File changed after this action" in warnings[0]
    assert target.read_text(encoding="utf-8") == "user edit"


def test_rewind_rejects_file_changes_after_preview(tmp_path, monkeypatch):
    from fastapi import HTTPException

    from clearact.domain.models import ChatMessage, Run, RunExecutionSettings, WorkflowStep
    from clearact.storage.run_store import RunStore
    from clearact.storage.snapshots import SnapshotStore

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "tools.yaml").write_text("tools: {}", encoding="utf-8")
    (tmp_path / "config" / "risk_rules.yaml").write_text("{}", encoding="utf-8")
    (tmp_path / "clearact.json").write_text(json.dumps({
        "defaultProfile": "demo",
        "profiles": {"demo": {"provider": "ollama", "model": "d", "baseUrl": "http://localhost", "contextWindow": 10}},
        "agent": {"maxIterations": 2, "maxToolCallsPerRun": 3},
        "workspace": {"defaultRoot": "workspace"},
        "storage": {"dataRoot": "data"},
        "network": {}, "policy": {"defaultAutonomy": "green", "defaultViewMode": "simple"}, "web": {},
    }), encoding="utf-8")
    monkeypatch.setattr(webapp.cli, "_project_root", lambda: tmp_path)
    target = workspace / "report.txt"
    target.write_text("before", encoding="utf-8")
    snapshot_id = SnapshotStore(tmp_path / "data", workspace).save_before_write(target, "before", True)
    target.write_text("agent version", encoding="utf-8")
    action = Action(id="act_write", tool_name="write_file", arguments={
        "path": "report.txt", "content": "agent version",
    })
    step = WorkflowStep(title="Write report", summary="Create output", start_message_index=2)
    run = Run(goal="report", execution=RunExecutionSettings(workdir=str(workspace)), messages=[
        ChatMessage(role="system", content="instructions"),
        ChatMessage(role="user", content="report"),
        ChatMessage(role="assistant", tool_calls=[action]),
        ChatMessage(role="tool", name="write_file", tool_call_id=action.id, metadata={
            "status": "succeeded", "snapshot_id": snapshot_id,
        }),
    ], workflow_steps=[step])
    store = RunStore(tmp_path / "data")
    store.save_run(run)
    preview = asyncio.run(webapp.rewind_preview(run.id, step.id))
    assert preview["restore_paths"] == ["report.txt"]

    target.write_text("user edit", encoding="utf-8")
    request = webapp.StartRunRequest(
        goal="Try again", run_id=run.id, rewind_step_id=step.id,
        rewind_base_updated_at=preview["base_updated_at"],
        rewind_preview_token=preview["preview_token"],
    )
    with pytest.raises(HTTPException) as error:
        asyncio.run(webapp.start_run(request))

    assert error.value.status_code == 409
    assert target.read_text(encoding="utf-8") == "user edit"
    assert len(store.load_run(run.id).messages) == len(run.messages)


def test_file_preview_uses_recorded_before_and_after_and_rejects_outside_paths(tmp_path, monkeypatch):
    from fastapi import HTTPException

    from clearact.domain.models import ChatMessage, Run, RunExecutionSettings
    from clearact.storage.run_store import RunStore
    from clearact.storage.snapshots import SnapshotStore

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "tools.yaml").write_text("tools: {}", encoding="utf-8")
    (tmp_path / "config" / "risk_rules.yaml").write_text("{}", encoding="utf-8")
    (tmp_path / "clearact.json").write_text(json.dumps({
        "defaultProfile": "demo",
        "profiles": {"demo": {"provider": "ollama", "model": "d", "baseUrl": "http://localhost", "contextWindow": 10}},
        "agent": {"maxIterations": 2, "maxToolCallsPerRun": 3},
        "workspace": {"defaultRoot": "workspace"},
        "storage": {"dataRoot": "data"},
        "network": {}, "policy": {"defaultAutonomy": "green", "defaultViewMode": "simple"}, "web": {},
    }), encoding="utf-8")
    monkeypatch.setattr(webapp.cli, "_project_root", lambda: tmp_path)
    target = workspace / "note.txt"
    target.write_text("old\n", encoding="utf-8")
    stat = target.stat()
    read_version = f"{stat.st_mtime_ns}:{stat.st_size}"
    snapshot_id = SnapshotStore(tmp_path / "data", workspace).save_before_write(target, "old\n", True)
    target.write_text("later unrelated edit\n", encoding="utf-8")
    read_action = Action(id="act_read", tool_name="read_file", arguments={"path": "note.txt"})
    action = Action(id="act_preview", tool_name="write_file", arguments={"path": "note.txt", "content": "new\n"})
    run = Run(goal="edit", execution=RunExecutionSettings(workdir=str(workspace)), messages=[
        ChatMessage(role="assistant", tool_calls=[read_action]),
        ChatMessage(role="tool", tool_call_id=read_action.id, name="read_file", content="old\n", metadata={
            "status": "succeeded", "path": str(target), "read_file_range": {"version": read_version},
        }),
        webapp.ChatMessage(role="assistant", tool_calls=[action]),
        ChatMessage(role="tool", tool_call_id=action.id, name="write_file", content="written", metadata={
            "status": "succeeded", "path": str(target), "snapshot_id": snapshot_id,
        }),
    ])
    RunStore(tmp_path / "data").save_run(run)

    preview = asyncio.run(webapp.action_file_preview(run.id, action.id))
    assert "-old" in preview["content"] and "+new" in preview["content"]
    assert "later unrelated edit" not in preview["content"]
    read_preview = asyncio.run(webapp.action_file_preview(run.id, read_action.id))
    assert read_preview["content"] == "old\n"
    assert read_preview["source_state"] == "changed"
    run.messages[1].metadata["source_changed_around_read"] = True
    RunStore(tmp_path / "data").save_run(run)
    unstable_preview = asyncio.run(webapp.action_file_preview(run.id, read_action.id))
    assert unstable_preview["source_state"] == "unstable"
    run.messages[-1].metadata["path"] = str(tmp_path / "outside.txt")
    RunStore(tmp_path / "data").save_run(run)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(webapp.action_file_preview(run.id, action.id))
    assert exc.value.status_code == 403

    outside = tmp_path / "outside.txt"
    outside.write_text("outside before\n", encoding="utf-8")
    outside_stat = outside.stat()
    outside_version = f"{outside_stat.st_mtime_ns}:{outside_stat.st_size}"
    outside_snapshot = SnapshotStore(tmp_path / "data").save_before_write(
        outside, "outside before\n", True,
    )
    outside.write_text("later outside edit\n", encoding="utf-8")
    outside_read = Action(id="act_outside_read", tool_name="read_file", arguments={"path": str(outside)})
    outside_write = Action(id="act_outside_write", tool_name="write_file", arguments={
        "path": str(outside), "content": "outside after\n",
    })
    run.messages.extend([
        ChatMessage(role="assistant", tool_calls=[outside_read]),
        ChatMessage(role="tool", tool_call_id=outside_read.id, name="read_file", content="outside before\n", metadata={
            "status": "succeeded", "path": str(outside), "read_file_range": {"version": outside_version},
        }),
        ChatMessage(role="assistant", tool_calls=[outside_write]),
        ChatMessage(role="tool", tool_call_id=outside_write.id, name="write_file", metadata={
            "status": "succeeded", "path": str(outside), "snapshot_id": outside_snapshot,
        }),
    ])
    RunStore(tmp_path / "data").save_run(run)
    outside_read_preview = asyncio.run(webapp.action_file_preview(run.id, outside_read.id))
    assert outside_read_preview["content"] == "outside before\n"
    assert outside_read_preview["outside_workspace"] is True
    assert outside_read_preview["source_state"] == "changed"
    outside_write_preview = asyncio.run(webapp.action_file_preview(run.id, outside_write.id))
    assert "-outside before" in outside_write_preview["content"]
    assert "+outside after" in outside_write_preview["content"]
    assert outside_write_preview["outside_workspace"] is True
    assert "later outside edit" not in outside_write_preview["content"]


def test_mcp_catalog_only_returns_direct_https_streamable_http(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"servers": [
                {"server": {"name": "example/demo", "title": "Demo", "remotes": [
                    {"type": "streamable-http", "url": "https://example.test/mcp"},
                ]}},
                {"server": {"name": "example/template", "remotes": [
                    {"type": "streamable-http", "url": "https://{host}/mcp"},
                ]}},
                {"server": {"name": "example/local", "remotes": [
                    {"type": "streamable-http", "url": "http://127.0.0.1/mcp"},
                ]}},
            ]}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, params):
            assert url == "https://registry.modelcontextprotocol.io/v0.1/servers"
            assert params["search"] == "demo"
            return FakeResponse()

    monkeypatch.setattr(webapp.httpx, "AsyncClient", FakeClient)
    response = asyncio.run(webapp.search_mcp_catalog("demo"))
    assert [item["title"] for item in response["servers"]] == ["Demo"]
