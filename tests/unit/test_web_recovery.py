import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from clearact import webapp
from clearact.domain.enums import RiskLevel, RunStatus
from clearact.domain.models import (
    Action,
    ChatMessage,
    LLMResponse,
    Run,
    UserPolicy,
    WorkflowPlanItem,
    WorkflowStep,
)
from clearact.storage.run_store import RunStore


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
    monkeypatch.setattr(webapp, "_title_tasks", {})

    class TitleProvider:
        async def chat(self, messages, tools):
            return LLMResponse(content="Generated title")

    monkeypatch.setattr(webapp.cli, "_build_provider", lambda profile: TitleProvider())
    return settings, RunStore(settings.data_root)


async def finish_run(run_id):
    task = webapp._active_tasks.get(run_id)
    title_task = webapp._title_tasks.get(run_id)
    if task is not None:
        await task
    if title_task is not None:
        await title_task


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


def test_initialization_failure_is_visible_and_title_cannot_restore_created(web_environment, monkeypatch):
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


def test_stop_keeps_cancellation_cleanup_and_cancels_slow_title(web_environment, monkeypatch):
    _, store = web_environment

    async def scenario():
        executing, title_started = asyncio.Event(), asyncio.Event()

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

        class SlowTitle:
            async def chat(self, messages, tools):
                title_started.set()
                await asyncio.Event().wait()

        monkeypatch.setattr(webapp.cli, "_run", execute)
        monkeypatch.setattr(webapp.cli, "_build_provider", lambda profile: SlowTitle())
        response = await webapp.start_run(webapp.StartRunRequest(goal="test"))
        await executing.wait()
        await title_started.wait()
        title_task = webapp._title_tasks[response["run_id"]]
        await webapp.stop_run(response["run_id"])
        saved = store.load_run(response["run_id"])
        assert saved.status == RunStatus.CANCELLED
        assert saved.messages[-1].content == "cancelled tool result"
        assert title_task.cancelled()

    asyncio.run(scenario())


def test_delete_cancels_title_and_does_not_recreate_topic(web_environment, monkeypatch):
    _, store = web_environment

    async def scenario():
        title_started = asyncio.Event()

        async def execute(*args, run, **kwargs):
            run.status = RunStatus.COMPLETED
            store.save_run(run)

        class SlowTitle:
            async def chat(self, messages, tools):
                title_started.set()
                await asyncio.Event().wait()

        monkeypatch.setattr(webapp.cli, "_run", execute)
        monkeypatch.setattr(webapp.cli, "_build_provider", lambda profile: SlowTitle())
        response = await webapp.start_run(webapp.StartRunRequest(goal="test"))
        task = webapp._active_tasks[response["run_id"]]
        await title_started.wait()
        await task
        title_task = webapp._title_tasks[response["run_id"]]
        await webapp.delete_run(response["run_id"])
        assert title_task.cancelled()
        with pytest.raises(FileNotFoundError):
            store.load_run(response["run_id"])

    asyncio.run(scenario())


def test_manual_rename_survives_title_response_and_further_execution(web_environment, monkeypatch):
    _, store = web_environment

    async def scenario():
        started, title_started = asyncio.Event(), asyncio.Event()
        finish, title_ready = asyncio.Event(), asyncio.Event()

        async def execute(*args, run, **kwargs):
            run.status = RunStatus.RUNNING
            store.save_run(run)
            started.set()
            await finish.wait()
            run.messages.append(ChatMessage(role="assistant", content="result"))
            run.status = RunStatus.COMPLETED
            store.save_run(run)

        class SlowTitle:
            async def chat(self, messages, tools):
                title_started.set()
                await title_ready.wait()
                return LLMResponse(content="Late automatic title")

        monkeypatch.setattr(webapp.cli, "_run", execute)
        monkeypatch.setattr(webapp.cli, "_build_provider", lambda profile: SlowTitle())
        response = await webapp.start_run(webapp.StartRunRequest(goal="test"))
        await started.wait()
        await title_started.wait()
        await webapp.rename_run(response["run_id"], webapp.RenameRunRequest(title="My title"))
        title_task = webapp._title_tasks[response["run_id"]]
        title_ready.set()
        await title_task
        finish.set()
        await finish_run(response["run_id"])
        saved = store.load_run(response["run_id"])
        assert saved.title == "My title"
        assert saved.status == RunStatus.COMPLETED
        assert saved.messages[-1].content == "result"

    asyncio.run(scenario())


def test_step_rewind_reuses_earlier_phase_and_records_branch(web_environment, monkeypatch):
    _, store = web_environment
    first = Action(id="first", tool_name="read_file", arguments={"path": "source.txt"})
    second = Action(id="second", tool_name="web_search", arguments={"query": "new facts"})
    run = Run(
        goal="report",
        policy=UserPolicy(),
        messages=[
            ChatMessage(role="system", content="system"),
            ChatMessage(role="user", content="report"),
            ChatMessage(role="assistant", tool_calls=[first]),
            ChatMessage(role="tool", name="read_file", tool_call_id=first.id, content="retained"),
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

    async def execute(*args, **kwargs):
        return None

    monkeypatch.setattr(webapp.cli, "_run", execute)

    async def scenario():
        await webapp.start_run(
            webapp.StartRunRequest(
                goal="只看官方来源",
                run_id=run.id,
                rewind_step_id="research-step",
            )
        )
        await finish_run(run.id)

    asyncio.run(scenario())
    saved = store.load_run(run.id)
    assert [message.content for message in saved.messages] == ["system", "report", None, "retained", "只看官方来源"]
    assert [step.id for step in saved.workflow_steps] == ["understand", "read-step"]
    assert saved.workflow_plan == []
    revision = saved.workflow_revisions[-1]
    assert revision.from_step_id == "research-step"
    assert revision.reused_step_ids == ["understand", "read-step"]
    assert revision.discarded_message_count == 2
