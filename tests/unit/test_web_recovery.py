import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from clearact import webapp
from clearact.domain.enums import RiskLevel, RunStatus
from clearact.domain.models import (
    Action,
    ChatMessage,
    RiskAssessment,
    Run,
    RunEvent,
    UserPolicy,
    WorkflowPlanItem,
    WorkflowStep,
)
from clearact.storage.run_store import RunStore
from clearact.storage.snapshots import SnapshotStore


def test_restored_file_contents_uses_the_oldest_reversed_snapshot(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "source.txt"
    snapshots = SnapshotStore(tmp_path / "data", workspace)
    first = snapshots.save_before_write(target, "original", True)
    second = snapshots.save_before_write(target, "intermediate", True)

    assert webapp._restored_file_contents(snapshots, [second, first]) == {target: "original"}


@pytest.fixture
def web_environment(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = SimpleNamespace(
        workspace_root=workspace,
        data_root=tmp_path / "data",
        default_autonomy=RiskLevel.GREEN,
        models={"default_profile": "demo", "profiles": {"demo": {}, "other": {}}},
        agent=SimpleNamespace(max_iterations=3, max_tool_calls_per_run=5),
    )
    monkeypatch.setattr(webapp.cli, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(webapp, "load_settings", lambda root: settings)
    monkeypatch.setattr(webapp, "_active_tasks", {})
    monkeypatch.setattr(webapp, "_active_runs", {})
    monkeypatch.setattr(webapp, "_pending_approvals", {})
    return settings, RunStore(settings.data_root)


@pytest.mark.parametrize("status", [
    RunStatus.CREATED, RunStatus.RUNNING, RunStatus.WAITING_APPROVAL, RunStatus.PAUSED,
])
def test_startup_marks_abandoned_tasks_interrupted_without_replaying(web_environment, monkeypatch, status):
    _, store = web_environment
    successful = Action(tool_name="write_file", arguments={"path": "finished.txt", "content": "done"})
    pending = Action(tool_name="mcp__mail__send", arguments={"body": "mail"})
    successful_result = ChatMessage(
        role="tool", name=successful.tool_name, tool_call_id=successful.id, content="written",
        metadata={"status": "succeeded", "snapshot_id": "snap_original"},
    )
    run = Run(
        goal="Produce a report and send it", status=status,
        messages=[
            ChatMessage(role="user", content="Write the report"),
            ChatMessage(role="assistant", tool_calls=[successful, pending]),
            successful_result,
        ],
        workflow_steps=[
            WorkflowStep(title="完成写入", summary="Saved file", status="completed", action_ids=[successful.id]),
            WorkflowStep(title="发送", summary="Send mail", status="active", action_ids=[pending.id]),
        ],
    )
    store.save_run(run)
    store.append_event(RunEvent(
        type="action.completed", run_id=run.id, action_id=successful.id, title="File saved",
    ))
    browser_closes = []

    async def close_all():
        browser_closes.append(True)

    async def no_automatic_execution(*args, **kwargs):
        raise AssertionError("Restart reconciliation must never invoke an execution provider")

    monkeypatch.setattr(webapp, "_computer_use_sessions", SimpleNamespace(close_all=close_all))
    monkeypatch.setattr(webapp.cli, "_run", no_automatic_execution)

    async def scenario():
        async with webapp.app.router.lifespan_context(webapp.app):
            saved = store.load_run(run.id)
            assert saved.status == RunStatus.INTERRUPTED
            assert saved.messages[2] == successful_result
            interrupted = saved.messages[3]
            assert interrupted.role == "tool" and interrupted.tool_call_id == pending.id
            assert interrupted.metadata == {
                "status": "interrupted", "reason": "service_restart", "side_effects": "unknown", "replayed": False,
            }
            assert "not replayed" in interrupted.content
            assert "MCP" in saved.messages[4].content
            assert saved.messages[-1].role == "system"
            assert "generic follow-up" in saved.messages[-1].content
            assert saved.workflow_steps[0].status == "completed"
            assert saved.workflow_steps[1].status == "needs_review"
            assert not webapp._pending_approvals
            assert not webapp._active_tasks
            events = store.load_events(run.id)
            assert [event.type for event in events] == [
                "action.completed", "action.interrupted", "run.interrupted",
            ]
            assert events[-1].data["previous_status"] == status.value
            assert events[-1].data["automatic_resume"] is False
            assert events[-1].data["interrupted_action_ids"] == [pending.id]
            initial = saved.model_dump()
            assert webapp._interrupt_incomplete_runs(store) == 0
            assert store.load_run(run.id).model_dump() == initial
            assert store.load_events(run.id) == events

    asyncio.run(scenario())
    assert browser_closes == [True]


def test_recovery_closes_historical_tool_groups_before_followups(web_environment):
    _, store = web_environment
    first = Action(tool_name="run_command", arguments={"command": "process-file"})
    second = Action(tool_name="computer_use", arguments={"action": "click"})
    run = Run(goal="continue", status=RunStatus.RUNNING, messages=[
        ChatMessage(role="assistant", tool_calls=[first]),
        ChatMessage(role="user", content="continue"),
        ChatMessage(role="assistant", tool_calls=[second]),
    ])
    store.save_run(run)
    assert webapp._interrupt_incomplete_runs(store) == 1
    saved = store.load_run(run.id)
    assert [message.role for message in saved.messages[:5]] == ["assistant", "tool", "user", "assistant", "tool"]
    assert saved.messages[1].tool_call_id == first.id
    assert saved.messages[4].tool_call_id == second.id


def test_recovery_scans_all_saved_tasks_and_preserves_terminal_ledgers(web_environment, caplog):
    _, store = web_environment
    stale = Run(goal="Old abandoned task", status=RunStatus.RUNNING)
    store.save_run(stale)
    terminal = []
    for index in range(25):
        status = [RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.INTERRUPTED][index % 4]
        run = Run(goal=f"Finished {index}", status=status)
        store.save_run(run)
        terminal.append((run.id, run.model_dump()))
    corrupt = store._root / "run_corrupt.json"
    corrupt.write_text("{ incomplete", encoding="utf-8")
    mismatched = store._root / "run_mismatched.json"
    mismatched.write_text(Run(id="../outside", goal="Invalid identity").model_dump_json(), encoding="utf-8")

    assert webapp._interrupt_incomplete_runs(store) == 1
    assert store.load_run(stale.id).status == RunStatus.INTERRUPTED
    assert "run_corrupt.json" in caplog.text
    assert "run_mismatched.json" in caplog.text
    assert corrupt.read_text(encoding="utf-8") == "{ incomplete"
    assert not (store._root.parent / "outside.json").exists()
    for run_id, original in terminal:
        assert store.load_run(run_id).model_dump() == original
        assert store.load_events(run_id) == []


def test_recovery_does_not_interrupt_a_live_task(web_environment):
    _, store = web_environment
    run = Run(goal="Live task", status=RunStatus.RUNNING)
    store.save_run(run)

    async def scenario():
        task = asyncio.create_task(asyncio.Event().wait())
        webapp._active_tasks[run.id] = task
        try:
            assert webapp._interrupt_incomplete_runs(store) == 0
            assert store.load_run(run.id).status == RunStatus.RUNNING
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_interrupted_task_can_be_explicitly_resumed_with_a_closed_tool_history(web_environment, monkeypatch):
    _, store = web_environment
    action = Action(tool_name="run_command", arguments={"command": "create-report"})
    run = Run(goal="report", status=RunStatus.RUNNING, messages=[ChatMessage(role="assistant", tool_calls=[action])])
    store.save_run(run)
    webapp._interrupt_incomplete_runs(store)
    executions = []

    async def execute(*args, run, **kwargs):
        executions.append(run.id)
        assert run.status == RunStatus.CREATED
        assert run.messages[1].tool_call_id == action.id
        assert run.messages[1].metadata["status"] == "interrupted"
        assert run.messages[-1].content == "I verified the report; continue without rerunning the command"
        run.status = RunStatus.COMPLETED
        store.save_run(run)

    monkeypatch.setattr(webapp.cli, "_run", execute)

    async def scenario():
        response = await webapp.start_run(webapp.StartRunRequest(
            goal="I verified the report; continue without rerunning the command", run_id=run.id,
        ))
        assert response["run_id"] == run.id
        await finish_run(run.id)

    asyncio.run(scenario())
    assert executions == [run.id]
    assert store.load_run(run.id).status == RunStatus.COMPLETED


@pytest.mark.parametrize("missing", [True, False])
def test_lifespan_handles_missing_or_invalid_configuration_and_closes_browser(tmp_path, monkeypatch, caplog, missing):
    monkeypatch.setattr(webapp.cli, "_project_root", lambda: tmp_path)
    if not missing:
        (tmp_path / "clearact.json").write_text("{ invalid", encoding="utf-8")
    closes = []

    async def close_all():
        closes.append(True)

    monkeypatch.setattr(webapp, "_computer_use_sessions", SimpleNamespace(close_all=close_all))

    async def scenario():
        async with webapp.app.router.lifespan_context(webapp.app):
            assert not (tmp_path / "data").exists()

    asyncio.run(scenario())
    assert closes == [True]
    if not missing:
        assert "configuration is invalid" in caplog.text


async def finish_run(run_id):
    task = webapp._active_tasks.get(run_id)
    if task is not None:
        await task


def test_new_topic_starts_execution_without_extra_model_request(web_environment, monkeypatch):
    _, store = web_environment
    calls = []

    async def execute(*args, run, **kwargs):
        calls.append(run.id)
        run.status = RunStatus.COMPLETED
        store.save_run(run)

    def unexpected_provider(profile):
        raise AssertionError("Starting a topic must not request a separate model-generated title")

    monkeypatch.setattr(webapp.cli, "_run", execute)
    monkeypatch.setattr(webapp.cli, "_build_provider", unexpected_provider)

    async def scenario():
        goal = "Summarize the uploaded documents and identify follow-up tasks"
        response = await webapp.start_run(webapp.StartRunRequest(goal=goal))
        await finish_run(response["run_id"])
        listing = await webapp.runs()
        assert calls == [response["run_id"]]
        saved = store.load_run(response["run_id"])
        assert saved.title is None
        assert saved.messages[0].content == webapp.system_prompt(goal, "zh")
        assert listing[0]["title"] == goal[:28]

    asyncio.run(scenario())


def test_history_marks_completed_topics_that_still_need_review(web_environment):
    _, store = web_environment
    run = Run(
        goal="Inspect a file",
        status=RunStatus.COMPLETED,
        workflow_steps=[WorkflowStep(title="读取材料", summary="读取本地文件", status="needs_review")],
    )
    store.save_run(run)
    listing = asyncio.run(webapp.runs())
    assert listing[0]["status"] == "completed"
    assert listing[0]["needs_review"] is True

    run.workflow_steps[0].status = "completed"
    store.save_run(run)
    assert asyncio.run(webapp.runs())[0]["needs_review"] is False


def test_workspace_browser_lists_folders_and_path_can_be_validated(web_environment):
    settings, _ = web_environment
    child = settings.workspace_root / "reports"
    child.mkdir()

    async def scenario():
        transport = httpx.ASGITransport(app=webapp.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            browsed = await client.get("/api/workspaces/browse", params={"path": str(settings.workspace_root)})
            assert browsed.status_code == 200
            assert browsed.json()["path"] == str(settings.workspace_root)
            assert {item["name"] for item in browsed.json()["directories"]} == {"reports"}
            filtered = await client.get(
                "/api/workspaces/browse", params={"path": str(settings.workspace_root), "query": "missing"}
            )
            assert filtered.json()["directories"] == []
            accepted = await client.post("/api/workspaces/validate", json={"path": str(settings.workspace_root)})
            assert accepted.status_code == 200
            assert accepted.json()["path"] == str(settings.workspace_root)
            missing = await client.post(
                "/api/workspaces/validate", json={"path": str(settings.workspace_root / "missing")}
            )
            assert missing.status_code == 422
            invalid_browse = await client.get(
                "/api/workspaces/browse", params={"path": str(settings.workspace_root / "missing")}
            )
            assert invalid_browse.status_code == 422

    asyncio.run(scenario())


def test_run_detail_etag_tracks_files_and_approval_without_parsing_unchanged_history(
    web_environment, monkeypatch,
):
    _, store = web_environment
    run = Run(goal="Track this task")
    store.save_run(run)

    async def scenario():
        transport = httpx.ASGITransport(app=webapp.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            url = f"/api/runs/{run.id}"
            first = await client.get(url)
            assert first.status_code == 200
            first_etag = first.headers["etag"]

            def unexpected_parse(*args, **kwargs):
                raise AssertionError("An unchanged run should not be parsed again")

            with monkeypatch.context() as guard:
                guard.setattr(RunStore, "load_run", unexpected_parse)
                guard.setattr(RunStore, "load_events", unexpected_parse)
                unchanged = await client.get(url, headers={"If-None-Match": first_etag})
            assert unchanged.status_code == 304
            assert not unchanged.content

            run.title = "Updated title"
            store.save_run(run)
            updated = await client.get(url, headers={"If-None-Match": first_etag})
            assert updated.status_code == 200
            assert updated.json()["run"]["title"] == "Updated title"
            assert updated.headers["etag"] != first_etag

            store.append_event(RunEvent(type="progress", run_id=run.id, title="Step done"))
            event_update = await client.get(url, headers={"If-None-Match": updated.headers["etag"]})
            assert event_update.status_code == 200
            assert len(event_update.json()["events"]) == 1
            assert event_update.headers["etag"] != updated.headers["etag"]

            action = Action(id="act_pending", tool_name="write_file", arguments={"path": "out.txt"})
            future = asyncio.get_running_loop().create_future()
            webapp._pending_approvals[run.id] = {
                "action": action,
                "assessment": RiskAssessment(level=RiskLevel.YELLOW, reasons=["writes a file"]),
                "future": future,
            }
            approval = await client.get(url, headers={"If-None-Match": event_update.headers["etag"]})
            assert approval.status_code == 200
            assert approval.json()["approval"]["action_id"] == action.id
            future.set_result(True)
            decided = await client.get(url, headers={"If-None-Match": approval.headers["etag"]})
            assert decided.status_code == 200
            assert decided.json()["approval"] is None

            assert (await client.delete(url)).status_code == 200
            deleted = await client.get(url, headers={"If-None-Match": decided.headers["etag"]})
            assert deleted.status_code == 404

    asyncio.run(scenario())


def test_uploaded_attachment_is_copied_into_workspace_and_recorded(web_environment, monkeypatch):
    settings, store = web_environment

    async def execute(*args, **kwargs):
        return None

    monkeypatch.setattr(webapp.cli, "_run", execute)

    async def scenario():
        uploaded = await webapp.upload_attachments(
            webapp.UploadRequest(
                workdir=str(settings.workspace_root),
                files=[
                    webapp.UploadItem(
                        name="../notes.txt",
                        media_type="text/plain",
                        data_base64=base64.b64encode("附件内容".encode()).decode(),
                    )
                ],
            )
        )
        reference = uploaded["attachments"][0]
        response = await webapp.start_run(
            webapp.StartRunRequest(goal="总结附件", workdir=str(settings.workspace_root), attachments=[reference])
        )
        await finish_run(response["run_id"])
        return uploaded, store.load_run(response["run_id"])

    uploaded, run = asyncio.run(scenario())

    reference = uploaded["attachments"][0]
    assert reference["name"] == "notes.txt"
    assert reference["path"].startswith(".clearact/attachments/upload_")
    target = settings.workspace_root / reference["path"]
    assert target.read_text(encoding="utf-8") == "附件内容"
    attachment = next(message for message in run.messages if message.role == "user").metadata["attachments"][0]
    assert attachment["path"] == reference["path"]
    assert attachment["storage_path"] == str(target.resolve())


def test_run_rejects_attachment_path_outside_upload_area(web_environment, tmp_path):
    settings, _ = web_environment
    outside = tmp_path / "outside.txt"
    outside.write_text("not uploaded", encoding="utf-8")

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            webapp.start_run(
                webapp.StartRunRequest(
                    goal="read",
                    workdir=str(settings.workspace_root),
                    attachments=[{"name": "outside.txt", "path": "../outside.txt", "media_type": "text/plain"}],
                )
            )
        )

    assert error.value.status_code == 422


@pytest.mark.parametrize("rewind", [False, True])
def test_resume_keeps_original_directory_and_execution_settings(web_environment, tmp_path, monkeypatch, rewind):
    settings, store = web_environment
    selected = tmp_path / "selected"
    selected.mkdir()
    calls = []
    action = Action(tool_name="write_file", arguments={"path": "result.txt"})

    async def execute(goal, profile, autonomy, workdir, iterations, tool_calls, *, run, **kwargs):
        calls.append((profile, workdir, iterations, tool_calls, kwargs["interface_language"]))
        (Path(workdir) / "result.txt").write_text(goal, encoding="utf-8")
        run.messages.append(ChatMessage(role="assistant", tool_calls=[action]))
        run.status = RunStatus.COMPLETED
        store.save_run(run)

    monkeypatch.setattr(webapp.cli, "_run", execute)

    async def scenario():
        response = await webapp.start_run(webapp.StartRunRequest(
            goal="initial", workdir=str(selected), profile="demo", max_iterations=7,
            max_tool_calls=11, interface_language="en",
        ))
        await finish_run(response["run_id"])
        # Simulate a page reload and changed global preferences: the follow-up
        # sends only its topic ID and feedback, as stage restart does in the UI.
        settings.models["default_profile"] = "other"
        settings.agent.max_iterations = 99
        settings.agent.max_tool_calls_per_run = 100
        followup = webapp.StartRunRequest(
            goal="follow-up", run_id=response["run_id"], rewind_action_id=action.id if rewind else None,
        )
        await webapp.start_run(followup)
        await finish_run(response["run_id"])
        saved = store.load_run(response["run_id"])
        assert saved.execution.workdir == str(selected.resolve())
        assert saved.policy.allowed_scopes == [str(selected.resolve())]

    asyncio.run(scenario())
    assert calls == [("demo", str(selected.resolve()), 7, 11, "en")] * 2
    assert (selected / "result.txt").read_text(encoding="utf-8") == "follow-up"
    assert not (settings.workspace_root / "result.txt").exists()


@pytest.mark.parametrize("explicit_override", [False, True])
def test_legacy_run_recovers_authorized_directory_or_explicit_override(
    web_environment, tmp_path, monkeypatch, explicit_override,
):
    _, store = web_environment
    original, selected = tmp_path / "original", tmp_path / "selected"
    original.mkdir()
    selected.mkdir()
    run = Run(goal="legacy", policy=UserPolicy(allowed_scopes=[str(original), str(selected)]))
    store.save_run(run)
    path = store._root / f"{run.id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("execution")
    path.write_text(json.dumps(payload), encoding="utf-8")
    observed = []

    async def execute(*args, **kwargs):
        observed.append(args[3])

    monkeypatch.setattr(webapp.cli, "_run", execute)

    async def scenario():
        await webapp.start_run(webapp.StartRunRequest(
            goal="continue", run_id=run.id, workdir=str(selected) if explicit_override else None,
        ))
        await finish_run(run.id)

    asyncio.run(scenario())
    expected = str((selected if explicit_override else original).resolve())
    assert observed == [expected]
    assert store.load_run(run.id).execution.workdir == expected
    assert store.load_run(run.id).policy.allowed_scopes == [expected]


def test_resume_rejects_missing_original_directory(web_environment, tmp_path):
    _, store = web_environment
    run = Run(goal="legacy", policy=UserPolicy(allowed_scopes=[str(tmp_path / "removed")]))
    store.save_run(run)

    with pytest.raises(HTTPException) as error:
        asyncio.run(webapp.start_run(webapp.StartRunRequest(goal="continue", run_id=run.id)))

    assert error.value.status_code == 422
    assert store.load_run(run.id).messages == []


def test_initialization_failure_is_visible(web_environment, monkeypatch):
    _, store = web_environment

    async def execute(*args, run, **kwargs):
        # Persisted changes are newer than the object held by start_run.
        saved = store.load_run(run.id)
        saved.messages.append(ChatMessage(role="tool", content="initialization diagnostic"))
        store.save_run(saved)
        raise RuntimeError("Missing API key")

    monkeypatch.setattr(webapp.cli, "_run", execute)

    async def scenario():
        response = await webapp.start_run(webapp.StartRunRequest(goal="test", interface_language="en"))
        await finish_run(response["run_id"])
        saved = store.load_run(response["run_id"])
        assert saved.status == RunStatus.FAILED
        assert any(message.content == "initialization diagnostic" for message in saved.messages)
        assert saved.messages[-1].content == "Task failed: RuntimeError: Missing API key"
        failure = store.load_events(saved.id)[-1]
        assert failure.type == "run_failed"
        assert "Missing API key" in failure.detail

    asyncio.run(scenario())


def test_stop_keeps_cancellation_cleanup(web_environment, monkeypatch):
    _, store = web_environment

    async def scenario():
        executing = asyncio.Event()

        async def execute(*args, run, **kwargs):
            run.status = RunStatus.RUNNING
            store.save_run(run)
            executing.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                latest = store.load_run(run.id)
                latest.messages.append(ChatMessage(role="tool", content="cancelled tool result"))
                latest.status = RunStatus.CANCELLED
                store.save_run(latest)
                raise

        monkeypatch.setattr(webapp.cli, "_run", execute)
        response = await webapp.start_run(webapp.StartRunRequest(goal="test"))
        await executing.wait()
        await webapp.stop_run(response["run_id"])
        saved = store.load_run(response["run_id"])
        assert saved.status == RunStatus.CANCELLED
        assert saved.messages[-1].content == "cancelled tool result"

    asyncio.run(scenario())


def test_delete_completed_topic(web_environment, monkeypatch):
    _, store = web_environment

    async def scenario():
        async def execute(*args, run, **kwargs):
            run.status = RunStatus.COMPLETED
            store.save_run(run)

        monkeypatch.setattr(webapp.cli, "_run", execute)
        response = await webapp.start_run(webapp.StartRunRequest(goal="test"))
        await finish_run(response["run_id"])
        await webapp.delete_run(response["run_id"])
        with pytest.raises(FileNotFoundError):
            store.load_run(response["run_id"])

    asyncio.run(scenario())


def test_manual_rename_survives_current_and_follow_up_execution(web_environment, monkeypatch):
    _, store = web_environment

    async def scenario():
        started = asyncio.Event()
        finish = asyncio.Event()

        async def execute(*args, run, **kwargs):
            run.status = RunStatus.RUNNING
            store.save_run(run)
            started.set()
            await finish.wait()
            run.messages.append(ChatMessage(role="assistant", content="result"))
            run.status = RunStatus.COMPLETED
            store.save_run(run)

        monkeypatch.setattr(webapp.cli, "_run", execute)
        response = await webapp.start_run(webapp.StartRunRequest(goal="test"))
        await started.wait()
        await webapp.rename_run(response["run_id"], webapp.RenameRunRequest(title="My title"))
        finish.set()
        await finish_run(response["run_id"])
        await webapp.start_run(webapp.StartRunRequest(goal="follow-up", run_id=response["run_id"]))
        await finish_run(response["run_id"])
        saved = store.load_run(response["run_id"])
        assert saved.title == "My title"
        assert saved.status == RunStatus.COMPLETED
        assert saved.messages[-1].content == "result"

    asyncio.run(scenario())


def test_step_rewind_reuses_earlier_phase_and_records_branch(web_environment, monkeypatch):
    settings, store = web_environment
    source = settings.workspace_root / "source.txt"
    source.write_text("original", encoding="utf-8")
    stat = source.stat()
    read_metadata = {
        "status": "succeeded",
        "path": str(source),
        "truncated": False,
        "read_only_signature": f"read_file:{source}:{stat.st_mtime_ns}:{stat.st_size}:{{}}",
    }
    first = Action(id="first", tool_name="read_file", arguments={"path": "source.txt"})
    second = Action(id="second", tool_name="web_search", arguments={"query": "new facts"})
    run = Run(
        goal="report",
        policy=UserPolicy(),
        messages=[
            ChatMessage(role="system", content="system"),
            ChatMessage(role="user", content="report"),
            ChatMessage(role="assistant", tool_calls=[first]),
            ChatMessage(
                role="tool", name="read_file", tool_call_id=first.id,
                content="retained", metadata=read_metadata,
            ),
            ChatMessage(role="assistant", tool_calls=[second]),
            ChatMessage(role="tool", name="web_search", tool_call_id=second.id, content="discarded"),
        ],
        workflow_plan=[
            WorkflowPlanItem(id="read", title="读取资料", summary="读取本地资料"),
            WorkflowPlanItem(id="research", title="补充检索", summary="检索外部资料"),
        ],
        workflow_steps=[
            WorkflowStep(id="understand", title="理解任务", summary="plan", start_message_index=2),
            WorkflowStep(
                id="read-step",
                title="读取资料",
                summary="读取本地资料",
                plan_item_id="read",
                action_ids=[first.id],
                start_message_index=2,
            ),
            WorkflowStep(
                id="research-step",
                title="补充检索",
                summary="检索外部资料",
                plan_item_id="research",
                action_ids=[second.id],
                start_message_index=4,
            ),
        ],
    )
    store.save_run(run)
    store.append_event(RunEvent(type="action.completed", run_id=run.id, action_id=second.id, title="旧分支检索"))

    async def execute(*args, **kwargs):
        return None

    monkeypatch.setattr(webapp.cli, "_run", execute)

    async def scenario():
        preview = await webapp.rewind_preview(run.id, "research-step")
        assert preview["reused_steps"][-1]["title"] == "读取资料"
        assert preview["discarded_steps"][0]["title"] == "补充检索"
        assert preview["stale_source_paths"] == []
        legacy = run.model_copy(deep=True)
        legacy.messages[3].metadata.pop("read_only_signature")
        store.save_run(legacy)
        legacy_preview = await webapp.rewind_preview(run.id, "research-step")
        assert legacy_preview["unverified_source_paths"] == [str(source)]
        assert any("缺少可核验" in warning for warning in legacy_preview["warnings"])
        store.save_run(run)
        source.write_text("changed content", encoding="utf-8")
        preview = await webapp.rewind_preview(run.id, "research-step")
        assert preview["stale_source_paths"] == [str(source)]
        assert any("重新读取" in warning for warning in preview["warnings"])
        assert webapp._retained_file_read_issues(run, 4, {source: "changed content"}) == {
            str(source): "changed"
        }
        matching_restore = run.model_copy(deep=True)
        matching_restore.messages[3].content = "original"
        assert webapp._retained_file_read_issues(matching_restore, 4, {source: "original"}) == {}
        partial_read = matching_restore.model_copy(deep=True)
        partial_read.messages[3].metadata["truncated"] = True
        assert webapp._retained_file_read_issues(partial_read, 4, {source: "original"}) == {
            str(source): "unverified"
        }
        retained_write = run.model_copy(deep=True)
        retained_write.messages.insert(4, ChatMessage(
            role="tool", name="write_file", tool_call_id="retained-write",
            content="Updated", metadata={"status": "succeeded", "path": str(source)},
        ))
        assert webapp._retained_file_read_issues(retained_write, 5, {source: "original"}) == {
            str(source): "changed"
        }
        assert store.load_run(run.id).messages[-1].content == "discarded"
        await webapp.start_run(
            webapp.StartRunRequest(
                goal="只看官方来源",
                run_id=run.id,
                rewind_step_id="research-step",
                rewind_base_updated_at=preview["base_updated_at"],
            )
        )
        await finish_run(run.id)

    asyncio.run(scenario())
    saved = store.load_run(run.id)
    assert [message.content for message in saved.messages[:4]] == ["system", "report", None, "retained"]
    assert saved.messages[-2].role == "system" and str(source) in saved.messages[-2].content
    assert saved.messages[-1].content == "只看官方来源"
    assert saved.messages[-1].metadata["rewind_revision_id"] == saved.workflow_revisions[-1].id
    assert [step.id for step in saved.workflow_steps] == ["understand", "read-step"]
    assert saved.workflow_plan == []
    revision = saved.workflow_revisions[-1]
    assert revision.from_step_id == "research-step"
    assert revision.from_step_title == "补充检索"
    assert revision.archive_available
    assert revision.reused_step_ids == ["understand", "read-step"]
    assert revision.discarded_message_count == 2
    assert any(str(source) in warning for warning in revision.rollback_warnings)
    archive = asyncio.run(webapp.revision_archive(run.id, revision.id))
    assert archive["run"]["messages"][-1]["content"] == "discarded"
    assert archive["events"][-1]["title"] == "旧分支检索"
