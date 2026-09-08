"""MCP client bridge: dynamic tools still execute through ClearAct's normal executor."""

from __future__ import annotations

import re
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from clearact.domain.errors import ToolValidationError
from clearact.domain.models import ToolDefinition, ToolResult
from clearact.tools.base import ToolContext
from clearact.tools.network import resolve_url_target

_NAME = re.compile(r"[^a-zA-Z0-9_-]+")


def _safe_name(value: str) -> str:
    return _NAME.sub("_", value).strip("_") or "tool"


def mcp_tool_name(server_name: str, tool_name: str) -> str:
    return f"mcp__{_safe_name(server_name)}__{_safe_name(tool_name)}"


class MCPManager:
    """Owns MCP transport sessions for one ClearAct run and discovers their tools."""

    def __init__(self, servers: dict[str, dict[str, Any]], *, allow_localhost: bool = False) -> None:
        self._servers = {name: dict(config) for name, config in servers.items() if config.get("enabled", True)}
        self._allow_localhost = allow_localhost
        self._sessions: dict[str, ClientSession] = {}
        self._tools: dict[str, tuple[str, str, Any]] = {}
        self._stack = AsyncExitStack()
        self.errors: dict[str, str] = {}

    async def connect(self) -> None:
        for name, config in self._servers.items():
            try:
                session = await self._connect_one(config)
                listed = await session.list_tools()
                self._sessions[name] = session
                for remote in listed.tools:
                    local = mcp_tool_name(name, remote.name)
                    if local in self._tools:
                        raise ValueError(f"Duplicate MCP tool name: {local}")
                    self._tools[local] = (name, remote.name, remote)
            except Exception as exc:
                self.errors[name] = f"{type(exc).__name__}: {exc}"

    async def _connect_one(self, config: dict[str, Any]) -> ClientSession:
        transport = config.get("transport", "stdio")
        if transport == "stdio":
            command = config.get("command")
            if not isinstance(command, str) or not command.strip():
                raise ValueError("stdio server requires command")
            args = config.get("args", [])
            if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
                raise ValueError("stdio args must be a string list")
            env = config.get("env")
            if env is not None and (
                not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())
            ):
                raise ValueError("stdio env must be a string map")
            streams = await self._stack.enter_async_context(
                stdio_client(StdioServerParameters(command=command, args=args, env=env))
            )
        elif transport == "streamable_http":
            url = config.get("url")
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                raise ValueError("streamable_http server requires an http(s) url")
            safe, error, _ = resolve_url_target(url, allow_loopback=self._allow_localhost)
            if not safe:
                raise ValueError(f"Unsafe MCP URL: {error}")
            headers = config.get("headers")
            if headers is not None and (
                not isinstance(headers, dict)
                or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items())
            ):
                raise ValueError("headers must be a string map")
            read_stream, write_stream, _ = await self._stack.enter_async_context(
                streamablehttp_client(url, headers=headers)
            )
            streams = (read_stream, write_stream)
        else:
            raise ValueError("transport must be stdio or streamable_http")
        session = await self._stack.enter_async_context(ClientSession(*streams))
        await session.initialize()
        return session

    def definitions(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name=local_name,
                description=(
                    f"MCP ({server}/{remote_name}): {getattr(remote, 'description', '') or 'External MCP tool'}"
                ),
                parameters=getattr(remote, "inputSchema", None) or {"type": "object", "properties": {}},
            )
            for local_name, (server, remote_name, remote) in self._tools.items()
        ]

    async def call(self, local_name: str, arguments: dict[str, Any]) -> ToolResult:
        try:
            server, remote_name, _ = self._tools[local_name]
        except KeyError as exc:
            raise ToolValidationError(f"Unknown MCP tool: {local_name}") from exc
        result = await self._sessions[server].call_tool(remote_name, arguments=arguments)
        blocks = []
        for block in result.content:
            dumped = block.model_dump(mode="json") if hasattr(block, "model_dump") else str(block)
            blocks.append(dumped)
        content = "\n".join(item.get("text", str(item)) if isinstance(item, dict) else str(item) for item in blocks)
        if not content:
            content = "MCP tool returned no text content."
        return ToolResult(
            action_id="",
            tool_name=local_name,
            ok=not bool(getattr(result, "isError", False)),
            content=content,
            metadata={
                "kind": "mcp",
                "server": server,
                "remote_tool": remote_name,
                "content": blocks,
                "is_error": bool(getattr(result, "isError", False)),
            },
        )

    async def close(self) -> None:
        await self._stack.aclose()


class MCPTool:
    def __init__(self, name: str, manager: MCPManager) -> None:
        self.name = name
        self._manager = manager
        self._definition = next(definition for definition in manager.definitions() if definition.name == name)

    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(self, arguments: dict, context: ToolContext, action_id: str) -> ToolResult:
        result = await self._manager.call(self.name, arguments)
        result.action_id = action_id
        return result
