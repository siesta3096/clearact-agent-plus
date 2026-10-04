from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from clearact.domain.models import Run, RunEvent


class RunStore:
    def __init__(self, data_root: Path) -> None:
        self._root = data_root / "runs"
        self._root.mkdir(parents=True, exist_ok=True)

    def save_run(self, run: Run) -> None:
        run.updated_at = datetime.now()
        path = self._root / f"{run.id}.json"
        temp_path = path.with_suffix(".json.tmp")
        temp_path.write_text(run.model_dump_json(indent=2), encoding="utf-8")
        temp_path.replace(path)

    def load_run(self, run_id: str) -> Run:
        if not run_id.startswith("run_") or any(char in run_id for char in "\\/"):
            raise ValueError("Invalid run ID.")
        path = self._root / f"{run_id}.json"
        return Run.model_validate_json(path.read_text(encoding="utf-8"))

    def list_runs(self, limit: int = 20) -> list[Run]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        runs = [Run.model_validate_json(path.read_text(encoding="utf-8")) for path in self._root.glob("run_*.json")]
        return sorted(runs, key=lambda run: run.updated_at, reverse=True)[:limit]

    def append_event(self, event: RunEvent) -> None:
        path = self._root / f"{event.run_id}.events.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.model_dump(mode="json"), ensure_ascii=False) + "\n")

    def load_events(self, run_id: str) -> list[RunEvent]:
        if not run_id.startswith("run_") or any(char in run_id for char in "\\/"):
            raise ValueError("Invalid run ID.")
        path = self._root / f"{run_id}.events.jsonl"
        if not path.exists():
            return []
        return [RunEvent.model_validate_json(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def _revision_path(self, run_id: str, revision_id: str) -> Path:
        if (
            not run_id.startswith("run_")
            or not revision_id.startswith("rev_")
            or any(char in value for value in (run_id, revision_id) for char in "\\/")
        ):
            raise ValueError("Invalid run or revision ID.")
        return self._root / "history" / run_id / f"{revision_id}.json"

    def save_revision_archive(self, run: Run, revision_id: str) -> None:
        """Keep the complete previous branch before trimming the active ledger."""
        path = self._revision_path(run.id, revision_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "run": run.model_dump(mode="json"),
            "events": [event.model_dump(mode="json") for event in self.load_events(run.id)],
        }
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    def load_revision_archive(self, run_id: str, revision_id: str) -> dict:
        return json.loads(self._revision_path(run_id, revision_id).read_text(encoding="utf-8"))

    def delete_revision_archives(self, run_id: str) -> None:
        directory = self._revision_path(run_id, "rev_placeholder").parent
        if directory.is_dir():
            for path in directory.glob("rev_*.json"):
                path.unlink()
            for path in directory.glob("rev_*.json.tmp"):
                path.unlink()
            if not any(directory.iterdir()):
                directory.rmdir()

    def truncate_events_before_action(self, run_id: str, action_id: str) -> None:
        """Drop the selected action and every subsequent event for a stage rewind."""
        events = self.load_events(run_id)
        cutoff = next((index for index, event in enumerate(events) if event.action_id == action_id), len(events))
        path = self._root / f"{run_id}.events.jsonl"
        kept = events[:cutoff]
        path.write_text(
            "".join(json.dumps(event.model_dump(mode="json"), ensure_ascii=False) + "\n" for event in kept),
            encoding="utf-8",
        )

    def prune_events_to_actions(self, run_id: str, action_ids: set[str]) -> None:
        """Keep run-level history plus events belonging to retained actions."""
        events = self.load_events(run_id)
        kept = [
            event
            for event in events
            if event.action_id in action_ids
            or (event.action_id is None and event.type in {"run.started"})
        ]
        path = self._root / f"{run_id}.events.jsonl"
        path.write_text(
            "".join(json.dumps(event.model_dump(mode="json"), ensure_ascii=False) + "\n" for event in kept),
            encoding="utf-8",
        )
