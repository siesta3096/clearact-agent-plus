from clearact.domain.enums import DecisionOutcome, RiskLevel
from clearact.domain.models import Action, RiskAssessment, UserPolicy
from clearact.runtime.policy import PolicyEngine


def test_green_action_is_allowed_at_green_autonomy():
    decision = PolicyEngine().decide(
        RiskAssessment(level=RiskLevel.GREEN),
        UserPolicy(autonomy_threshold=RiskLevel.GREEN),
        "write_file",
    )

    assert decision.outcome is DecisionOutcome.ALLOW


def test_yellow_action_requires_approval_at_green_autonomy():
    decision = PolicyEngine().decide(
        RiskAssessment(level=RiskLevel.YELLOW),
        UserPolicy(autonomy_threshold=RiskLevel.GREEN),
        "write_file",
    )

    assert decision.outcome is DecisionOutcome.REQUIRE_APPROVAL


def test_disabled_write_is_denied():
    assessment = RiskAssessment(level=RiskLevel.GREEN)
    decision = PolicyEngine().decide(assessment, UserPolicy(allow_write=False), "write_file")

    assert decision.outcome is DecisionOutcome.DENY


def test_hard_stop_requires_approval_even_at_red_autonomy():
    decision = PolicyEngine().decide(
        RiskAssessment(level=RiskLevel.RED, hard_stop=True),
        UserPolicy(autonomy_threshold=RiskLevel.RED),
        Action(tool_name="write_file", arguments={}).tool_name,
    )

    assert decision.outcome is DecisionOutcome.REQUIRE_APPROVAL


def test_capability_rule_overrides_coarse_colour_threshold():
    policy = UserPolicy(
        autonomy_threshold=RiskLevel.RED,
        capability_rules={"workspace_modify": "ask", "mcp_write": "deny"},
    )

    modify = PolicyEngine().decide(
        RiskAssessment(level=RiskLevel.YELLOW, category="workspace_modify"), policy, "write_file"
    )
    external = PolicyEngine().decide(
        RiskAssessment(level=RiskLevel.YELLOW, category="mcp_write"), policy, "mcp__demo__update"
    )

    assert modify.outcome is DecisionOutcome.REQUIRE_APPROVAL
    assert external.outcome is DecisionOutcome.DENY
