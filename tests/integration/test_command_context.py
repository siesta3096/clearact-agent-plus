import asyncio
import sys

from clearact.context.budget import ContextBudget
from clearact.context.builder import ContextBuilder
from clearact.domain.models import Action, ChatMessage
from clearact.runtime.prompting import system_prompt
from clearact.tools.base import ToolContext
from clearact.tools.command import ExecuteCommandTool
from clearact.tools.computer_use import ComputerUseTool
from clearact.tools.filesystem import ListFilesTool, ReadFileTool, ReadPdfTool, WriteFileTool
from clearact.tools.office import CreateDocumentTool, CreateSpreadsheetTool
from clearact.tools.web import FetchUrlTool, WebSearchTool
from clearact.tools.workflow import DeclareWorkflowPlanTool, DeclareWorkflowStepTool, ReviseWorkflowPlanTool


def test_default_unicode_command_logs_fit_default_context_with_all_builtin_tools(workspace):
    command = ExecuteCommandTool()
    action = Action(tool_name=command.name, arguments={
        "command": [sys.executable, "-c", "import sys; print('中' * 40000); print('错' * 40000, file=sys.stderr)"],
    })
    result = asyncio.run(command.execute(action.arguments, ToolContext(workspace), action.id))
    assert result.ok and result.metadata["output_truncated"]
    definitions = [tool.definition() for tool in (
        DeclareWorkflowPlanTool(), DeclareWorkflowStepTool(), ReviseWorkflowPlanTool(),
        ListFilesTool(), ReadFileTool(), ReadPdfTool(), WriteFileTool(), command,
        CreateDocumentTool(), CreateSpreadsheetTool(), WebSearchTool(), FetchUrlTool(), ComputerUseTool(),
    )]
    goal = "Run the requested command and summarize its output."
    messages = [ChatMessage(role="system", content=system_prompt(goal, "en")),
                ChatMessage(role="user", content=goal), ChatMessage(role="assistant", tool_calls=[action]),
                ChatMessage(role="tool", name=command.name, tool_call_id=action.id, content=result.content)]
    budget = ContextBudget(32768, 0.8)
    retained, selected_tools = ContextBuilder(budget).build(messages, definitions, {command.name})
    assert retained == messages
    assert not budget.exceeds(retained, selected_tools)
