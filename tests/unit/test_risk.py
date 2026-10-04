from clearact.domain.enums import DecisionOutcome, RiskLevel
from clearact.domain.models import Action, UserPolicy
from clearact.runtime.policy import PolicyEngine
from clearact.runtime.risk import RiskEvaluator


def test_new_workspace_write_is_green(workspace):
    assessment = RiskEvaluator(workspace, {}).assess(Action(tool_name="write_file", arguments={"path": "new.txt"}))

    assert assessment.level is RiskLevel.GREEN


def test_existing_write_is_yellow(workspace):
    (workspace / "existing.txt").write_text("old", encoding="utf-8")

    assessment = RiskEvaluator(workspace, {}).assess(Action(tool_name="write_file", arguments={"path": "existing.txt"}))

    assert assessment.level is RiskLevel.YELLOW


def test_path_escape_is_red_but_not_a_hard_stop(workspace):
    action = Action(tool_name="write_file", arguments={"path": "../outside.txt"})
    assessment = RiskEvaluator(workspace, {}).assess(action)

    assert assessment.level is RiskLevel.RED
    assert assessment.hard_stop is False


def test_high_impact_tools_always_require_confirmation_at_red(workspace):
    evaluator = RiskEvaluator(workspace, {}, {
        "mcp__demo__delete": {"destructive": True},
    })
    policy = UserPolicy(
        autonomy_threshold=RiskLevel.RED,
        capability_rules={"destructive": "allow"},
    )

    for tool_name in ("mcp__demo__delete", "delete_file", "execute_command"):
        assessment = evaluator.assess(Action(tool_name=tool_name, arguments={}))
        decision = PolicyEngine().decide(assessment, policy, tool_name)
        assert assessment.level is RiskLevel.RED
        assert assessment.hard_stop
        assert decision.outcome is DecisionOutcome.REQUIRE_APPROVAL
