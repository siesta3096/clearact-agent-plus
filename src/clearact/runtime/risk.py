from __future__ import annotations

from pathlib import Path

from clearact.domain.enums import RiskLevel
from clearact.domain.models import Action, RiskAssessment


class RiskEvaluator:
    def __init__(self, workspace_root: Path, rules: dict, tool_risk_hints: dict[str, dict] | None = None) -> None:
        self._workspace_root = workspace_root.resolve()
        self._rules = rules
        self._tool_risk_hints = tool_risk_hints or {}

    def assess(self, action: Action) -> RiskAssessment:
        if action.tool_name in self._rules.get("hard_stop_operations", []):
            return RiskAssessment(
                level=RiskLevel.RED,
                hard_stop=True,
                reasons=["该操作属于必须单独确认的重大外部或系统行为。"],
                category="destructive",
            )
        if action.tool_name in {"web_search", "fetch_url", "list_files", "read_file"}:
            category = "web_read" if action.tool_name in {"web_search", "fetch_url"} else "local_read"
            return RiskAssessment(level=RiskLevel.WHITE, reasons=["只读操作，不会改变状态。"], category=category)
        if action.tool_name.startswith("mcp__"):
            hint = self._tool_risk_hints.get(action.tool_name, {})
            if hint.get("destructive"):
                return RiskAssessment(
                    level=RiskLevel.RED,
                    reasons=["MCP 服务将此工具声明为可能产生破坏性副作用。"],
                    category="destructive",
                )
            if hint.get("read_only"):
                return RiskAssessment(
                    level=RiskLevel.WHITE,
                    reasons=["MCP 服务将此工具声明为只读。"],
                    category="mcp_read",
                )
            return RiskAssessment(
                level=RiskLevel.YELLOW,
                reasons=["第三方 MCP 工具未声明为只读；执行前按外部修改处理。"],
                category="mcp_write",
            )
        if action.tool_name == "write_file":
            return self._assess_write(action)
        if action.tool_name in {"delete_file", "execute_command"}:
            return RiskAssessment(
                level=RiskLevel.RED, reasons=["删除或命令执行属于高影响操作。"], category="destructive"
            )
        return RiskAssessment(level=RiskLevel.YELLOW, reasons=["未知工具默认按受控修改处理。"], category="other")

    def _assess_write(self, action: Action) -> RiskAssessment:
        raw_path = action.arguments.get("path")
        if not isinstance(raw_path, str):
            return RiskAssessment(
                level=RiskLevel.RED,
                hard_stop=True,
                reasons=["写入路径缺失或格式无效。"],
                category="destructive",
            )
        candidate = Path(raw_path).expanduser()
        target = candidate.resolve() if candidate.is_absolute() else (self._workspace_root / candidate).resolve()
        if not self._is_within_workspace(target):
            # Yellow can inspect outside paths, but may only write there after approval.
            # Red permits this ordinary user-file write automatically; system paths remain
            # blocked by the filesystem tool itself.
            return RiskAssessment(
                level=RiskLevel.RED, reasons=["将写入工作区外的文件。"], category="outside_write"
            )
        if target.exists():
            return RiskAssessment(
                level=RiskLevel.YELLOW, reasons=["将修改已有文件。"], category="workspace_modify"
            )
        return RiskAssessment(
            level=RiskLevel.GREEN, reasons=["将在授权工作区创建新文件，可通过快照撤销。"], category="workspace_create"
        )

    def _is_within_workspace(self, target: Path) -> bool:
        try:
            target.relative_to(self._workspace_root)
        except ValueError:
            return False
        return True
