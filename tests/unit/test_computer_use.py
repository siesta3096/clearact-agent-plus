import asyncio

from clearact.domain.enums import DecisionOutcome, RiskLevel
from clearact.domain.errors import ToolValidationError
from clearact.domain.models import Action, UserPolicy
from clearact.runtime.policy import PolicyEngine
from clearact.runtime.risk import RiskEvaluator
from clearact.tools.base import ToolContext
from clearact.tools.computer_use import ComputerUseTool, _normalise_url, _redact_sensitive_url


def test_browser_read_is_read_only_but_page_interaction_requires_approval(tmp_path):
    evaluator = RiskEvaluator(tmp_path, {})
    policy = PolicyEngine()
    defaults = UserPolicy(
        autonomy_threshold=RiskLevel.RED,
        capability_rules={"browser_interaction": "ask"},
    )

    read = evaluator.assess(Action(tool_name="computer_use", arguments={"action": "inspect"}))
    click = evaluator.assess(Action(tool_name="computer_use", arguments={"action": "click", "target": "Continue"}))

    assert policy.decide(read, defaults, "computer_use").outcome == DecisionOutcome.ALLOW
    assert click.category == "browser_interaction"
    assert policy.decide(click, defaults, "computer_use").outcome == DecisionOutcome.REQUIRE_APPROVAL


def test_irreversible_page_action_always_requires_confirmation(tmp_path):
    assessment = RiskEvaluator(tmp_path, {}).assess(
        Action(tool_name="computer_use", arguments={"action": "click", "target": "Delete account"})
    )

    decision = PolicyEngine().decide(assessment, UserPolicy(autonomy_threshold=RiskLevel.RED), "computer_use")

    assert assessment.hard_stop
    assert decision.outcome == DecisionOutcome.REQUIRE_APPROVAL


def test_agent_browser_tool_is_unavailable_until_user_opens_a_session(tmp_path):
    result = asyncio.run(
        ComputerUseTool().execute({"action": "inspect"}, ToolContext(tmp_path), "act_browser")
    )

    assert not result.ok
    assert "open the browser panel" in result.content


def test_browser_navigation_only_accepts_http_urls():
    assert _normalise_url("example.com") == "https://example.com"
    assert _normalise_url("localhost:8787") == "http://localhost:8787"
    assert _normalise_url("http://localhost:8787") == "http://localhost:8787"

    for unsafe_url in ("file:///C:/secret.txt", "javascript:alert(1)", "https://user:pass@example.com"):
        try:
            _normalise_url(unsafe_url)
        except ToolValidationError:
            continue
        raise AssertionError(f"Unsafe URL was accepted: {unsafe_url}")


def test_browser_inspection_redacts_url_tokens():
    assert _redact_sensitive_url("https://example.com/page?token=secret&q=public") == (
        "https://example.com/page?token=%5Bredacted%5D&q=public"
    )
