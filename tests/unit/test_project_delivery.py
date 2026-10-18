import asyncio
import hashlib
import io
import zipfile
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from clearact import webapp
from clearact.domain.models import Action, ChatMessage, DeliveryFile, DeliveryReport, Run, RunExecutionSettings
from clearact.storage.run_store import RunStore
from clearact.tools.base import ToolContext
from clearact.tools.filesystem import WriteFileTool


@pytest.fixture
def project_environment(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    settings = SimpleNamespace(workspace_root=root, data_root=tmp_path / "data")
    monkeypatch.setattr(webapp, "load_settings", lambda _: settings)
    return root, RunStore(settings.data_root), TestClient(webapp.app)


def project_run(root):
    run = Run(goal="Create project", execution=RunExecutionSettings(workdir=str(root)))
    files = []
    for path, content in [("index.html", "<h1>Game</h1>"), ("src/story.js", "export const story = []; ")]:
        action = Action(tool_name="write_file", arguments={"path": path, "content": content})
        result = asyncio.run(WriteFileTool().execute(action.arguments, ToolContext(root), action.id))
        run.messages.extend([ChatMessage(role="assistant", tool_calls=[action]),
                             ChatMessage(role="tool", name=action.tool_name, tool_call_id=action.id,
                                         metadata={**result.metadata, "status": "succeeded"})])
        files.append(DeliveryFile(path=path, action_id=action.id, sha256=result.metadata["content_sha256"],
                                  size_bytes=len(content.encode()), change="created"))
    run.delivery_report = DeliveryReport(passed=True, files=files)
    return run


def test_zip_contains_only_recorded_files_and_keeps_project_paths(project_environment):
    root, store, client = project_environment
    run = project_run(root)
    (root / "private.txt").write_text("private", encoding="utf-8")
    store.save_run(run)
    response = client.get(f"/api/runs/{run.id}/project-download")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert set(archive.namelist()) == {"index.html", "src/story.js"}
        assert archive.read("index.html") == (root / "index.html").read_bytes()
    run.delivery_history = [run.delivery_report]
    run.delivery_report = None
    store.save_run(run)
    assert client.get(f"/api/runs/{run.id}/project-download?turn=-1").status_code == 200
    assert client.get(f"/api/runs/{run.id}/project-download?turn=99").status_code == 404


@pytest.mark.parametrize("mutation", ["changed", "forged_hash", "outside", "failed", "unrecorded", "read_denied"])
def test_zip_refuses_changed_forged_outside_or_denied_files(project_environment, mutation):
    root, store, client = project_environment
    run = project_run(root)
    if mutation in {"changed", "forged_hash"}:
        (root / "index.html").write_bytes(b"external edit")
        if mutation == "forged_hash":
            run.delivery_report.files[0].sha256 = hashlib.sha256(b"external edit").hexdigest()
    elif mutation == "outside":
        run.delivery_report.files[0].path = "../outside.txt"
    elif mutation == "failed":
        run.messages[1].metadata["status"] = "failed"
    elif mutation == "unrecorded":
        run.delivery_report.files[0].action_id = "invented"
    else:
        run.policy.allow_read = False
    store.save_run(run)
    response = client.get(f"/api/runs/{run.id}/project-download")
    assert response.status_code in {403, 409}


def test_screenshot_serves_only_recorded_unchanged_preview(project_environment):
    root, store, client = project_environment
    action = Action(id="preview", tool_name="preview_project", arguments={})
    target = root / ".clearact" / "previews" / (hashlib.sha256(action.id.encode()).hexdigest() + ".png")
    target.parent.mkdir(parents=True)
    image = b"\x89PNG\r\nactual preview fixture"
    target.write_bytes(image)
    run = Run(goal="Preview", execution=RunExecutionSettings(workdir=str(root)), messages=[
        ChatMessage(role="assistant", tool_calls=[action]),
        ChatMessage(role="tool", name="preview_project", tool_call_id=action.id,
                    metadata={"status": "failed", "screenshot_path": str(target),
                              "content_sha256": hashlib.sha256(image).hexdigest()}),
    ])
    store.save_run(run)
    url = f"/api/runs/{run.id}/actions/preview/screenshot"
    assert client.get(url).content == image
    target.write_bytes(b"user edit")
    assert client.get(url).status_code == 409
    assert client.get(f"/api/runs/{run.id}/actions/unknown/screenshot").status_code == 404
