from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4


class SnapshotStore:
    def __init__(self, data_root: Path, workspace_root: Path | None = None) -> None:
        self._root = data_root / "snapshots"
        self._workspace_root = workspace_root.resolve() if workspace_root else None
        self._root.mkdir(parents=True, exist_ok=True)

    def save_before_write(self, path: Path, content: str | None, existed: bool) -> str:
        snapshot_id = f"snapshot_{uuid4().hex[:12]}"
        content_path = self._root / f"{snapshot_id}.txt"
        metadata_path = self._root / f"{snapshot_id}.json"
        content_path.write_text(content or "", encoding="utf-8")
        metadata_path.write_text(
            json.dumps(
                {
                    "id": snapshot_id,
                    "path": str(path),
                    "existed": existed,
                    "created_at": datetime.now().isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return snapshot_id

    def restore(self, snapshot_id: str) -> Path:
        target, existed, content = self.load_before_write(snapshot_id)
        if existed:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        elif target.exists():
            target.unlink()
        return target

    def load_before_write(self, snapshot_id: str) -> tuple[Path, bool, str]:
        """Read an audited before-image without changing the workspace."""
        if not snapshot_id.startswith("snapshot_") or any(char in snapshot_id for char in "\\/"):
            raise ValueError("Invalid snapshot ID.")
        metadata_path = self._root / f"{snapshot_id}.json"
        content_path = self._root / f"{snapshot_id}.txt"
        if not metadata_path.is_file() or not content_path.is_file():
            raise FileNotFoundError(snapshot_id)
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        target = Path(metadata["path"]).resolve()
        if self._workspace_root:
            try:
                target.relative_to(self._workspace_root)
            except ValueError as exc:
                raise ValueError("Snapshot target is outside the authorized workspace.") from exc
        return target, bool(metadata["existed"]), content_path.read_text(encoding="utf-8")
