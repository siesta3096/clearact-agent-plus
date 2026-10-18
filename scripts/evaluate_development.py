"""Opt-in real-model project evaluation. Never part of the deterministic test suite.

Run from the repository: PYTHONPATH=src python scripts/evaluate_development.py --output <new-directory>
Uses a configured localhost model, isolated state, native project tools and no general commands/MCP.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

from clearact import cli
from clearact.domain.enums import RiskLevel, RunStatus
from clearact.domain.models import ChatMessage, Run, RunExecutionSettings, UserPolicy
from clearact.runtime.prompting import system_prompt
from clearact.settings import load_settings

GOAL = (
    "在这个空目录创建一个实际可玩的中世纪幽默视觉小说网页：城堡地下室里的奶酪失踪案，不要恐怖元素。"
    "要有开场、至少两个会走向不同结局的选择、重新开始和本地存档读取。"
    "拆分页面、样式、引擎和剧情数据，写好运行说明与每一部分作用的架构文档。"
    "请直接在目录写文件，离线完成。最后实际打开网页，测试两个分支以及存档读档，修复遇到的问题。"
)


class NativePreviewApproval:
    async def request(self, action, assessment):
        return action.tool_name == "preview_project" and assessment.category == "browser_interaction"


async def evaluate(output: Path, profile_name: str | None):
    settings = load_settings(cli._project_root())
    selected = profile_name or settings.models["default_profile"]
    profile = settings.models["profiles"][selected]
    if urlsplit(profile["base_url"]).hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("This evaluation only sends requests to a configured localhost model")
    if output.exists() and any(output.iterdir()):
        raise ValueError("Evaluation output directory must be empty")
    output.mkdir(parents=True, exist_ok=True)
    project = output / "project"
    project.mkdir()
    settings = settings.model_copy(update={"workspace_root": project, "data_root": output / "state", "mcp_servers": {}})
    run = Run(goal=GOAL, execution=RunExecutionSettings(workdir=str(project), profile=selected, task_mode="develop"),
              policy=UserPolicy(autonomy_threshold=RiskLevel.YELLOW, allow_web=False,
                                allowed_scopes=[str(project)], capability_rules={
                                    "local_read": "allow", "workspace_create": "allow", "workspace_modify": "allow",
                                    "outside_write": "deny", "browser_interaction": "allow",
                                }), messages=[ChatMessage(role="system", content=system_prompt(GOAL, "zh")),
                                              ChatMessage(role="user", content=GOAL)])
    started, error = time.monotonic(), None
    try:
        await asyncio.wait_for(cli._run_impl(
            GOAL, selected, None, str(project), 40, 100, run=run, render_console=False,
            approval_gate=NativePreviewApproval(), settings=settings,
        ), timeout=1200)
    except Exception as exc:
        error = type(exc).__name__
    results = [{"tool": message.name, "status": message.metadata.get("status"),
                "action_id": message.tool_call_id} for message in run.messages if message.role == "tool"]
    summary = {"run_id": run.id, "model": profile["model"], "status": run.status.value,
               "elapsed_seconds": round(time.monotonic() - started, 2), "error_type": error,
               "tools": results,
               "delivery": run.delivery_report.model_dump(mode="json") if run.delivery_report else None}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=True), flush=True)
    passed = run.status is RunStatus.COMPLETED and run.delivery_report and run.delivery_report.passed and not error
    return 0 if passed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(evaluate(args.output.resolve(), args.profile)))
