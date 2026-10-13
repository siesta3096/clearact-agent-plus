from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


class DataLeaseError(RuntimeError):
    """Another process owns the mutable state in this data directory."""


@dataclass
class _Lease:
    handle: BinaryIO
    references: int = 1


_mutex = threading.RLock()
_leases: dict[Path, _Lease] = {}


def _lock(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _after_fork() -> None:
    """Discard inherited references without unlocking the parent's open file."""
    global _mutex, _leases
    inherited = _leases
    _mutex = threading.RLock()
    _leases = {}
    for lease in inherited.values():
        lease.handle.close()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


@contextmanager
def data_lease(data_root: Path) -> Iterator[None]:
    """Own a data directory across processes; tasks in this process share ownership.

    The OS releases this lock on process exit, including an abrupt stop. Nested
    Web/CLI calls and concurrent tasks retain it until their last scope exits.
    The persistent lock file is never deleted: its existence is not ownership.
    """
    root = data_root.resolve()
    with _mutex:
        lease = _leases.get(root)
        if lease is not None:
            lease.references += 1
        else:
            root.mkdir(parents=True, exist_ok=True)
            handle = (root / ".clearact.lock").open("a+b")
            try:
                if handle.seek(0, os.SEEK_END) == 0:
                    handle.write(b"\0")
                    handle.flush()
                try:
                    _lock(handle)
                except OSError as exc:
                    raise DataLeaseError(
                        "Another ClearAct process is using this data directory. "
                        "Stop that process before starting another service, running a CLI task, or restoring files."
                    ) from exc
                lease = _Lease(handle)
                _leases[root] = lease
            except BaseException:
                handle.close()
                raise
    try:
        yield
    finally:
        with _mutex:
            if _leases.get(root) is lease:
                lease.references -= 1
                if lease.references == 0:
                    del _leases[root]
                    try:
                        _unlock(lease.handle)
                    finally:
                        lease.handle.close()
