from clearact.domain.models import ToolDefinition


class ToolFilter:
    def select(self, tools: list[ToolDefinition], recent_tool_names: set[str]) -> list[ToolDefinition]:
        # A previous action does not determine the next phase's capabilities.
        # Keep workflow declarations and MCP tools reachable across phases;
        # runtime policy remains responsible for permission checks.
        return list(tools)
