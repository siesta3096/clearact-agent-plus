from __future__ import annotations

from clearact.domain.models import ToolDefinition, ToolResult
from clearact.tools.base import ToolContext


class DeclareWorkflowPlanTool:
    """Let the model publish a task-specific plan before external work starts."""

    name = "declare_workflow_plan"

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Publish the concise, user-visible plan for this task before using any external tool. "
                "Choose phases that fit the actual task; do not use a generic fixed template."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "Short explanation of the goal, constraints, and approach.",
                    },
                    "steps": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 12,
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "description": "Stable short identifier."},
                                "title": {"type": "string", "description": "Task-specific phase title."},
                                "summary": {"type": "string", "description": "What this phase will produce."},
                                "done_when": {
                                    "type": "string",
                                    "description": (
                                        "One concrete, user-visible condition for judging this phase complete."
                                    ),
                                },
                                "kind": {
                                    "type": "string",
                                    "enum": ["research", "analysis", "files", "external", "general"],
                                },
                            },
                            "required": ["id", "title", "summary", "kind"],
                        },
                    },
                },
                "required": ["summary", "steps"],
            },
        )

    async def execute(self, arguments: dict, _context: ToolContext, action_id: str) -> ToolResult:
        return ToolResult(
            action_id=action_id,
            tool_name=self.name,
            ok=True,
            content="Workflow plan recorded. Begin its first phase.",
        )


class DeclareWorkflowStepTool:
    """A zero-risk marker the model uses to create an honest UI workflow phase."""

    name = "declare_workflow_step"

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Create a visible workflow step before beginning a meaningful new phase. "
                "Use a task-specific title and a concise description of what this phase will do. "
                "This does not perform external work."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "plan_item_id": {
                        "type": "string",
                        "description": "The id of the matching item from declare_workflow_plan.",
                    },
                    "title": {"type": "string", "description": "Specific short phase name, 2-12 words."},
                    "summary": {"type": "string", "description": "Concise user-visible plan for this phase."},
                    "kind": {
                        "type": "string",
                        "enum": ["research", "analysis", "files", "external", "general"],
                    },
                },
                "required": ["title", "summary"],
            },
        )

    async def execute(self, arguments: dict, _context: ToolContext, action_id: str) -> ToolResult:
        return ToolResult(
            action_id=action_id,
            tool_name=self.name,
            ok=True,
            content="Workflow step recorded. Continue with the work described in this phase.",
        )


class ReviseWorkflowPlanTool:
    """Replace only the not-yet-started phases when the task changes shape."""

    name = "revise_workflow_plan"

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Update or extend the remaining user-visible phases when new evidence changes the approach. "
                "Completed and active phases are preserved automatically. Supply only future phases "
                "and a concise public reason; do not call this for minor wording changes."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                    "steps": {
                        "type": "array", "minItems": 1, "maxItems": 12,
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "title": {"type": "string"},
                                "summary": {"type": "string"},
                                "done_when": {"type": "string"},
                                "kind": {"type": "string"},
                            },
                            "required": ["id", "title", "summary"],
                        },
                    },
                },
                "required": ["reason", "steps"],
            },
        )

    async def execute(self, arguments: dict, _context: ToolContext, action_id: str) -> ToolResult:
        return ToolResult(
            action_id=action_id,
            tool_name=self.name,
            ok=True,
            content="Remaining workflow phases updated.",
        )
