from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from clearact.domain.enums import DecisionOutcome, RiskLevel, RunStatus, Stage


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


class Action(BaseModel):
    id: str = Field(default_factory=lambda: new_id("act"))
    tool_name: str
    arguments: dict[str, Any]


class ChatMessage(BaseModel):
    role: str
    content: str | None = None
    # This stores only reasoning explicitly returned by the selected model/API.
    # It is not an attempt to recover a model's hidden chain-of-thought.
    reasoning_content: str | None = None
    tool_call_id: str | None = None
    name: str | None = None
    # Structured public tool outcome data for specialized workflow cards.
    metadata: dict[str, Any] = Field(default_factory=dict)
    tool_calls: list[Action] = Field(default_factory=list)


class ToolDefinition(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]


class WorkflowStep(BaseModel):
    """A user-visible phase explicitly declared by the model during a run."""

    id: str = Field(default_factory=lambda: new_id("step"))
    title: str
    summary: str
    action_ids: list[str] = Field(default_factory=list)


class LLMResponse(BaseModel):
    content: str | None = None
    # Provider-returned reasoning / thinking text, when the provider exposes it.
    reasoning_content: str | None = None
    tool_calls: list[Action] = Field(default_factory=list)
    usage: dict[str, int] = Field(default_factory=dict)
    finish_reason: str | None = None


class ToolResult(BaseModel):
    action_id: str
    tool_name: str
    ok: bool
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class RiskAssessment(BaseModel):
    level: RiskLevel
    hard_stop: bool = False
    reasons: list[str] = Field(default_factory=list)


class UserPolicy(BaseModel):
    autonomy_threshold: RiskLevel = RiskLevel.GREEN
    allowed_scopes: list[str] = Field(default_factory=lambda: ["workspace"])
    allow_read: bool = True
    allow_write: bool = True
    allow_web: bool = True


class PolicyDecision(BaseModel):
    outcome: DecisionOutcome
    reason: str


class RunExecutionSettings(BaseModel):
    """Persist the execution choices needed to resume a topic after a reload."""

    workdir: str | None = None
    profile: str | None = None
    max_iterations: int | None = Field(default=None, ge=1)
    max_tool_calls: int | None = Field(default=None, ge=1)
    interface_language: str | None = Field(default=None, pattern="^(zh|en)$")


class Run(BaseModel):
    id: str = Field(default_factory=lambda: new_id("run"))
    goal: str
    title: str | None = None
    status: RunStatus = RunStatus.CREATED
    policy: UserPolicy = Field(default_factory=UserPolicy)
    execution: RunExecutionSettings = Field(default_factory=RunExecutionSettings)
    messages: list[ChatMessage] = Field(default_factory=list)
    # Public, model-generated stage summaries. These are deliberately distinct
    # from private chain-of-thought / provider reasoning traces.
    stage_notes: dict[str, str] = Field(default_factory=dict)
    # The first card is always task understanding. Every later card is created
    # only when the model declares a meaningful next phase.
    workflow_steps: list[WorkflowStep] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class RunEvent(BaseModel):
    type: str
    run_id: str
    timestamp: datetime = Field(default_factory=datetime.now)
    stage: Stage | None = None
    risk: RiskLevel | None = None
    title: str
    detail: str | None = None
    action_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class Checkpoint(BaseModel):
    id: str = Field(default_factory=lambda: new_id("cp"))
    run_id: str
    action_id: str
    constraint_version: int = 1
    input_fingerprint: str
    valid: bool = True
