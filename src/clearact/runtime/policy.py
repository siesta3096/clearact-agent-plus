from clearact.domain.enums import DecisionOutcome
from clearact.domain.models import PolicyDecision, RiskAssessment, UserPolicy
from clearact.tools.artifacts import FILE_WRITE_TOOLS


class PolicyEngine:
    def decide(self, assessment: RiskAssessment, policy: UserPolicy, tool_name: str | None = None) -> PolicyDecision:
        if tool_name == "preview_project" and (not policy.allow_read or not policy.allow_write
                                               or policy.capability_rules.get("local_read") == "deny"):
            return PolicyDecision(outcome=DecisionOutcome.DENY, reason="本地预览需要已启用的工作区读取与写入权限。")
        if (tool_name in {"list_files", "read_file", "read_pdf", "search_files", "check_files"}
                and not policy.allow_read):
            return PolicyDecision(outcome=DecisionOutcome.DENY, reason="用户策略已禁用本地读取操作。")
        if tool_name in FILE_WRITE_TOOLS and not policy.allow_write:
            return PolicyDecision(outcome=DecisionOutcome.DENY, reason="用户策略已禁用本地写入操作。")
        if tool_name == "execute_command" and (
            not all((policy.allow_read, policy.allow_write, policy.allow_web))
            or any(policy.capability_rules.get(category) == "deny" for category in (
                "local_read", "web_read", "workspace_create", "workspace_modify", "outside_write", "destructive",
                "mcp_read", "mcp_write", "browser_interaction",
            ))
        ):
            return PolicyDecision(
                outcome=DecisionOutcome.DENY,
                reason="通用命令无法保证只读、离线或路径隔离，不能绕过已关闭的能力。",
            )
        if tool_name in {"web_search", "fetch_url"} and not policy.allow_web:
            return PolicyDecision(outcome=DecisionOutcome.DENY, reason="用户策略已禁用联网操作。")
        if tool_name and tool_name.startswith("mcp__") and not policy.allow_web:
            return PolicyDecision(outcome=DecisionOutcome.DENY, reason="用户策略已禁用外部 MCP 服务调用。")
        if tool_name == "computer_use" and not policy.allow_web:
            return PolicyDecision(outcome=DecisionOutcome.DENY, reason="用户策略已禁用联网操作。")
        rule = policy.capability_rules.get(assessment.category)
        if rule == "deny":
            return PolicyDecision(outcome=DecisionOutcome.DENY, reason="该能力已被用户关闭。")
        if assessment.hard_stop:
            return PolicyDecision(
                outcome=DecisionOutcome.REQUIRE_APPROVAL,
                reason="该操作触发了不可绕过的强制确认规则。",
            )
        if rule == "allow":
            return PolicyDecision(outcome=DecisionOutcome.ALLOW, reason="该能力已由用户设为自动允许。")
        if rule == "ask":
            return PolicyDecision(outcome=DecisionOutcome.REQUIRE_APPROVAL, reason="该能力设置为每次先询问。")
        if assessment.level.rank <= policy.autonomy_threshold.rank:
            return PolicyDecision(outcome=DecisionOutcome.ALLOW, reason="操作处于用户允许的自动执行等级内。")
        return PolicyDecision(
            outcome=DecisionOutcome.REQUIRE_APPROVAL,
            reason="操作风险高于当前自动执行等级。",
        )
