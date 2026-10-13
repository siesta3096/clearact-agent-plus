from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from clearact.domain.errors import ToolValidationError
from clearact.tools.base import ToolContext

FILE_WRITE_TOOLS = frozenset({"write_file", "create_document", "create_spreadsheet"})
OFFICE_TOOLS = frozenset({"create_document", "create_spreadsheet"})
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class ArtifactVersion:
    existed: bool
    content_sha256: str | None = None
    stat_version: tuple[int, ...] | None = None


def _stat_version(value: os.stat_result) -> tuple[int, ...]:
    # Windows path.stat() and fstat() can report different creation times after atomic replacement.
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _existing_artifact(path: Path) -> tuple[bytes | None, ArtifactVersion]:
    try:
        initial = path.stat()
    except FileNotFoundError:
        return None, ArtifactVersion(existed=False)
    if not stat.S_ISREG(initial.st_mode):
        raise ToolValidationError("Target file changed or is not a regular file")
    if initial.st_size > MAX_ARTIFACT_BYTES:
        raise ToolValidationError("Generated files and their existing before-images must be at most 32 MB")
    try:
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            previous = handle.read(MAX_ARTIFACT_BYTES + 1)
            after_read = os.fstat(handle.fileno())
        current = path.stat()
    except FileNotFoundError as exc:
        raise ToolValidationError("Target file changed while checking its version") from exc
    if len(previous) > MAX_ARTIFACT_BYTES:
        raise ToolValidationError("Generated files and their existing before-images must be at most 32 MB")
    version = _stat_version(initial)
    if any(_stat_version(value) != version for value in (opened, after_read, current)):
        raise ToolValidationError("Target file changed while checking its version")
    return previous, ArtifactVersion(True, hashlib.sha256(previous).hexdigest(), version)


def capture_artifact_version(path: Path) -> ArtifactVersion:
    """Capture the file state approved for replacement before rendering starts."""
    return _existing_artifact(path)[1]


def save_artifact(
    context: ToolContext, path: Path, content: bytes, *, expected_version: ArtifactVersion | None = None,
) -> dict:
    """Check for concurrent edits, then snapshot and replace the file atomically.

    The version check protects a rendering window; it is not a cross-process compare-and-swap.
    """
    if len(content) > MAX_ARTIFACT_BYTES:
        raise ToolValidationError("Generated files and their existing before-images must be at most 32 MB")
    previous, current_version = _existing_artifact(path)
    if expected_version is not None and current_version != expected_version:
        raise ToolValidationError("Target file changed during generation; the user's content was preserved")
    existed = current_version.existed
    snapshot_id = (
        context.snapshot_store.save_before_write_bytes(path, previous, existed)
        if context.snapshot_store else None
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return {"path": str(path), "existed": existed, "snapshot_id": snapshot_id,
            "file_kind": path.suffix.lstrip("."), "size_bytes": len(content),
            "content_sha256": hashlib.sha256(content).hexdigest()}
