from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from clearact.domain.models import ToolDefinition, ToolResult
from clearact.storage.snapshots import SnapshotStore


class ToolContext:
    def __init__(
        self,
        workspace_root: Path,
        snapshot_store: SnapshotStore | None = None,
        *,
        autonomy: str = "green",
        allow_localhost: bool = True,
        computer_session_resolver: Callable[[], Awaitable[Any | None]] | None = None,
        computer_use_available: Callable[[str], bool] | None = None,
    ) -> None:
        # Relative paths are always rooted here, irrespective of autonomy.
        self.workspace_root = workspace_root.resolve()
        self.autonomy = autonomy
        self.snapshot_store = snapshot_store
        self.allow_localhost = allow_localhost
        self.computer_session_resolver = computer_session_resolver
        self.computer_use_available = computer_use_available


class Tool(Protocol):
    name: str

    def definition(self) -> ToolDefinition: ...

    async def execute(self, arguments: dict, context: ToolContext, action_id: str) -> ToolResult: ...
