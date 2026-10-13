import asyncio

from clearact.domain.models import Action, ToolResult
from clearact.tools.base import ToolContext
from clearact.tools.registry import ToolRegistry


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, timeout_seconds: float) -> None:
        self._registry = registry
        self._timeout_seconds = timeout_seconds

    async def execute(self, action: Action, context: ToolContext) -> ToolResult:
        tool = self._registry.get(action.tool_name)
        if action.tool_name == "execute_command":
            arguments = dict(action.arguments)
            arguments.setdefault("timeout_seconds", min(60, self._timeout_seconds))
            requested = arguments["timeout_seconds"]
            if type(requested) in {int, float} and 0.1 <= requested <= 300:
                arguments["timeout_seconds"] = min(requested, self._timeout_seconds)
            # Let the tool report timeouts and reap its process tree before the outer cancellation.
            return await asyncio.wait_for(
                tool.execute(arguments, context, action.id), timeout=self._timeout_seconds + 5,
            )
        return await asyncio.wait_for(tool.execute(action.arguments, context, action.id), timeout=self._timeout_seconds)
