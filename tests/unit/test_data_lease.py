import asyncio
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from clearact import cli, webapp
from clearact.domain.enums import RunStatus
from clearact.domain.models import Run
from clearact.storage import lease
from clearact.storage.lease import DataLeaseError, data_lease
from clearact.storage.run_store import RunStore

_CHILD_CODE = """
import sys
from pathlib import Path
from clearact.storage.lease import DataLeaseError, data_lease

try:
    with data_lease(Path(sys.argv[1])):
        if len(sys.argv) > 2:
            Path(sys.argv[2]).write_text('ready', encoding='utf-8')
            sys.stdin.readline()
        print('acquired')
except DataLeaseError as exc:
    print(str(exc))
    sys.exit(2)
"""


def _child_environment() -> dict[str, str]:
    environment = os.environ.copy()
    source_root = str(Path(__file__).resolve().parents[2] / "src")
    environment["PYTHONPATH"] = source_root + os.pathsep + environment.get("PYTHONPATH", "")
    return environment


def _probe(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _CHILD_CODE, str(root)], cwd=root.parent,
        capture_output=True, text=True, encoding="utf-8", env=_child_environment(), timeout=10,
    )


@contextmanager
def _child_holding(root: Path) -> Iterator[subprocess.Popen]:
    ready = root.parent / "lease-child-ready.txt"
    ready.unlink(missing_ok=True)
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD_CODE, str(root), str(ready)], cwd=root.parent,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", env=_child_environment(),
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            if child.poll() is not None:
                raise AssertionError(f"Lease child failed: {child.communicate()}")
            if time.monotonic() >= deadline:
                raise AssertionError("Lease child did not acquire its temporary directory")
            time.sleep(0.02)
        yield child
    finally:
        if child.poll() is None:
            child.communicate(input="\n", timeout=10)
        else:
            child.communicate(timeout=10)


def test_reentrant_scopes_keep_other_processes_out_until_the_last_release(tmp_path):
    root = tmp_path / "data"
    with data_lease(root):
        with data_lease(root / "."):
            assert _probe(root).returncode == 2
        blocked = _probe(root)
        assert blocked.returncode == 2
        assert "Another ClearAct process" in blocked.stdout
    acquired = _probe(root)
    assert acquired.returncode == 0, acquired.stderr
    assert acquired.stdout.strip() == "acquired"
    assert (root / ".clearact.lock").exists()  # A stale pathname is not ownership.


def test_another_process_owns_the_directory_until_it_exits_or_crashes(tmp_path):
    root = tmp_path / "data"
    with _child_holding(root) as child:
        with pytest.raises(DataLeaseError, match="Another ClearAct process"):
            with data_lease(root):
                pytest.fail("An external owner must not be treated as a reentrant scope")
        child.kill()
        child.wait(timeout=10)
    with data_lease(root):
        assert _probe(root).returncode == 2
    assert _probe(root).returncode == 0


def test_threads_share_the_process_lease_but_retain_it_after_the_outer_scope_exits(tmp_path):
    root = tmp_path / "data"
    entered, release = Event(), Event()

    def task_scope():
        with data_lease(root):
            entered.set()
            assert release.wait(10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            with data_lease(root):
                task = pool.submit(task_scope)
                assert entered.wait(10)
            assert _probe(root).returncode == 2
        finally:
            release.set()
        task.result(timeout=10)
    assert _probe(root).returncode == 0


def test_failed_acquisition_and_failed_body_do_not_leave_an_owner(tmp_path, monkeypatch):
    root = tmp_path / "data"
    original_lock = lease._lock

    def interrupted_acquisition(handle):
        original_lock(handle)
        raise OSError("failure after lock acquisition")

    with monkeypatch.context() as guard:
        guard.setattr(lease, "_lock", interrupted_acquisition)
        with pytest.raises(DataLeaseError):
            with data_lease(root):
                pytest.fail("The failing acquisition must not enter")
    assert _probe(root).returncode == 0
    with pytest.raises(ValueError, match="task failure"):
        with data_lease(root):
            raise ValueError("task failure")
    assert _probe(root).returncode == 0


def test_web_conflict_is_reported_before_any_saved_task_is_interrupted(tmp_path, monkeypatch):
    root = tmp_path / "data"
    store = RunStore(root)
    run = Run(goal="An external process is still running", status=RunStatus.RUNNING)
    store.save_run(run)
    monkeypatch.setattr(webapp.cli, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(webapp, "load_settings", lambda _: SimpleNamespace(data_root=root))

    async def startup():
        async with webapp.app.router.lifespan_context(webapp.app):
            pytest.fail("A conflicting service must not start")

    with _child_holding(root):
        with pytest.raises(DataLeaseError):
            asyncio.run(startup())
    assert store.load_run(run.id).status == RunStatus.RUNNING
    assert store.load_events(run.id) == []


def test_cli_run_and_restore_refuse_an_external_owner_before_any_execution(tmp_path, monkeypatch):
    root = tmp_path / "data"
    monkeypatch.setattr(cli, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda _: SimpleNamespace(data_root=root))
    invoked = []

    async def execute(*args, **kwargs):
        invoked.append("execute")

    def restore(*args, **kwargs):
        invoked.append("restore")

    monkeypatch.setattr(cli, "_run_impl", execute)
    monkeypatch.setattr(cli.SnapshotStore, "restore", restore)
    with _child_holding(root):
        with pytest.raises(DataLeaseError):
            asyncio.run(cli._run("task", None, None))
        with pytest.raises(DataLeaseError):
            cli._restore_snapshot("snapshot_example")
    assert invoked == []


def test_web_lease_and_nested_cli_execution_share_ownership_until_shutdown(tmp_path, monkeypatch):
    root = tmp_path / "data"
    settings = SimpleNamespace(data_root=root)
    monkeypatch.setattr(cli, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda _: settings)
    monkeypatch.setattr(webapp, "load_settings", lambda _: settings)
    calls = []

    async def execute(goal, *args, settings, **kwargs):
        calls.append(goal)
        assert _probe(settings.data_root).returncode == 2

    async def close_all():
        assert _probe(root).returncode == 2

    monkeypatch.setattr(cli, "_run_impl", execute)
    monkeypatch.setattr(webapp, "_computer_use_sessions", SimpleNamespace(close_all=close_all))

    async def scenario():
        async with webapp.app.router.lifespan_context(webapp.app):
            await cli._run("nested task", None, None)
            assert _probe(root).returncode == 2

    asyncio.run(scenario())
    assert calls == ["nested task"]
    assert _probe(root).returncode == 0
