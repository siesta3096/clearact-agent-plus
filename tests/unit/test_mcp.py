import asyncio

from clearact.domain.enums import RiskLevel
from clearact.domain.models import Action
from clearact.runtime.policy import PolicyEngine
from clearact.runtime.risk import RiskEvaluator
from clearact.tools.mcp import MCPManager, MCPTool, mcp_tool_name


def test_mcp_name_is_namespaced_and_risk_is_controlled(workspace):
    name = mcp_tool_name("my server", "write/file")
    assert name == "mcp__my_server__write_file"
    assessment = RiskEvaluator(workspace, {}).assess(Action(tool_name=name, arguments={}))
    assert assessment.level is RiskLevel.YELLOW


def test_mcp_is_denied_when_external_tools_are_disabled(workspace):
    action = Action(tool_name=mcp_tool_name("demo", "tool"), arguments={})
    decision = PolicyEngine().decide(
        RiskEvaluator(workspace, {}).assess(action),
        type(
            "Policy",
            (),
            {"allow_read": True, "allow_write": True, "allow_web": False, "autonomy_threshold": RiskLevel.RED},
        )(),
        action.tool_name,
    )
    assert decision.outcome.value == "deny"


def test_mcp_tool_adapts_discovered_definition_and_result():
    class Remote:
        name = "echo"
        description = "Echo input"
        inputSchema = {"type": "object", "properties": {"value": {"type": "string"}}}

    class Manager:
        def definitions(self):
            from clearact.domain.models import ToolDefinition

            return [ToolDefinition(name="mcp__demo__echo", description="MCP", parameters=Remote.inputSchema)]

        async def call(self, name, arguments):
            from clearact.domain.models import ToolResult

            return ToolResult(
                action_id="", tool_name=name, ok=True, content=arguments["value"], metadata={"kind": "mcp"}
            )

    tool = MCPTool("mcp__demo__echo", Manager())
    assert tool.definition().parameters["properties"]["value"]["type"] == "string"
    result = asyncio.run(tool.execute({"value": "hello"}, None, "act_1"))
    assert result.action_id == "act_1"
    assert result.content == "hello"


def test_mcp_connection_timeout_is_isolated(monkeypatch):
    async def stalled_connect(_self, _config, _stack):
        await asyncio.Event().wait()

    monkeypatch.setattr(MCPManager, "_connect_one", stalled_connect)
    manager = MCPManager({"slow": {"connectTimeoutSeconds": 0.01}})

    asyncio.run(manager.connect())

    assert manager.definitions() == []
    assert manager.errors["slow"].startswith("TimeoutError:")
    asyncio.run(manager.close())


def test_mcp_connection_timeout_rejects_invalid_values():
    manager = MCPManager({"invalid": {"connectTimeoutSeconds": 0}})

    asyncio.run(manager.connect())

    assert manager.errors["invalid"].startswith("ValueError:")
    asyncio.run(manager.close())
