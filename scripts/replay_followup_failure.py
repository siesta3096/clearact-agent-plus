"""Replay a private failed ledger against localhost in isolated copies; never modify its workspace."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import time
from collections import Counter
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from evaluate_lightweight import MeasuredProvider, PreviewOnlyApproval, emit, save

from clearact import cli
from clearact.context.budget import ContextBudget
from clearact.domain.enums import RunStatus
from clearact.domain.models import ChatMessage, Run
from clearact.runtime.delivery import infer_contract
from clearact.settings import load_settings
from clearact.tools.base import ToolContext
from clearact.tools.project_preview import PreviewProjectTool


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def remap(value, old, new):
    if isinstance(value, str):
        return value.replace(old, new).replace(old.replace("\\", "/"), new.replace("\\", "/"))
    if isinstance(value, list):
        return [remap(item, old, new) for item in value]
    if isinstance(value, dict):
        return {key: remap(item, old, new) for key, item in value.items()}
    return value


async def replay(source: Path, output: Path, timeout: float):
    cli._load_dotenv(cli._project_root())
    settings = load_settings(cli._project_root())
    selected = settings.models["default_profile"]
    profile = settings.models["profiles"][selected]
    if urlsplit(profile["base_url"]).hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Only the configured localhost model is allowed")
    if not output.is_relative_to(settings.project_root / "data" / "evals"):
        raise ValueError("Output must be inside project data/evals")
    if output.exists():
        raise ValueError("Use a new output directory")
    original_hash = digest(source)
    original = Run.model_validate_json(source.read_text(encoding="utf-8"))
    root = Path(original.execution.workdir).resolve()
    paths = [root / name for name in ("index.html", "style.css", "game.js")]
    hashes = {path.name: digest(path) for path in paths}
    status_index = next(index for index in range(len(original.messages) - 1, -1, -1)
                        if original.messages[index].role == "user")
    question = original.messages[status_index].content
    before_mode = infer_contract(original.model_copy(update={"messages": original.messages[:status_index + 1]})).mode
    output.mkdir(parents=True)
    report = {"model": profile["model"], "original_run_sha256": original_hash,
              "source_sha256": hashes, "status_question": question, "status_mode": before_mode, "cases": []}

    # Exercise the actual generated typewriter UI without changing it.
    browser_root = output / "browser" / "project"
    browser_root.mkdir(parents=True)
    for path in paths:
        (browser_root / path.name).write_bytes(path.read_bytes())
    browser_args = {"steps": [{"selector": "#startBtn", "action": "click", "expect_text": "皮普"},
                              *[{"selector": "body", "action": "press", "value": "Space"} for _ in range(11)],
                              {"selector": "#choices button:first-child", "action": "click",
                               "expect_text": "皮普环顾四周"}], "expect_text": "皮普环顾四周"}
    result = await PreviewProjectTool().execute(browser_args, ToolContext(browser_root), "actual_typewriter_branch")
    report["browser_branch"] = {"passed": result.ok, **result.metadata}
    save(output / "summary.json", report)
    emit({"event": "browser_replay", "passed": result.ok, "steps_completed": result.metadata["steps_completed"]})

    for case in ("status_before_work", "status_after_failure", "continue_after_failure"):
        case_root = output / case
        project = case_root / "project"
        project.mkdir(parents=True)
        if case != "status_before_work":
            for path in paths:
                (project / path.name).write_bytes(path.read_bytes())
        local = settings.model_copy(update={"workspace_root": project, "data_root": case_root / "state",
                                             "mcp_servers": {}})
        run = Run.model_validate(remap(original.model_dump(mode="json"), str(root), str(project)))
        # The web follow-up handler resets a terminal run before starting the next turn.
        run.status = RunStatus.CREATED
        if case == "status_before_work":
            run.messages = run.messages[:status_index + 1]
            run.workflow_steps = [step for step in run.workflow_steps
                                  if step.start_message_index is None or step.start_message_index < status_index]
            run.delivery_report = None
        else:
            text = question if case == "status_after_failure" else (
                "继续完成游戏，先检查已有文件，避免重写。修复必要问题，用实际浏览器测试第一处分支选择。"
                "只做必要修改，不新增架构文档；检查若仍不通过，请如实说明具体未完成项。"
            )
            run.messages.append(ChatMessage(role="user", content=text))
        run.policy.allowed_scopes = [str(project)]
        run.policy.capability_rules.update({"command_execute": "deny", "web_read": "deny",
                                            "mcp_read": "deny", "mcp_write": "deny"})
        start_messages = len(run.messages)
        initial_files = {p.name: digest(p) for p in project.iterdir()}
        records = []
        measured = MeasuredProvider(cli._build_provider(profile), records,
                                    ContextBudget(profile["context_window"], settings.agent.context_budget_ratio))
        started = time.monotonic()
        error = None
        emit({"event": "case_started", "case": case})
        try:
            with patch.object(cli, "_build_provider", return_value=measured):
                await asyncio.wait_for(cli._run_impl(
                    run.goal, selected, None, str(project), 12, 40, run=run, render_console=False,
                    approval_gate=PreviewOnlyApproval(), settings=local,
                ), timeout=timeout)
        except Exception as exc:
            error = type(exc).__name__
        new_messages = run.messages[start_messages:]
        files_unchanged = {p.name: digest(p) for p in project.iterdir() if p.is_file()} == initial_files
        outcome = {"case": case, "status": run.status.value, "mode": run.task_contract.mode,
                   "elapsed_seconds": round(time.monotonic() - started, 3), "model_calls": len(records),
                   "input_tokens": sum(record.get("usage", {}).get("prompt_tokens", 0) for record in records),
                   "output_tokens": sum(record.get("usage", {}).get("completion_tokens", 0) for record in records),
                   "tools": dict(Counter(m.name for m in new_messages if m.role == "tool")),
                   "files_unchanged": files_unchanged, "error_type": error,
                   "final_answer": run.messages[-1].content, "calls": records}
        if case.startswith("status_"):
            outcome["passed"] = (error is None and files_unchanged and run.task_contract.mode == "discuss"
                                 and run.messages[-1].metadata.get("source") == "recorded_status") and not any(
                m.tool_calls and any(a.tool_name not in {"list_files", "read_file", "search_files", "check_files"}
                                     for a in m.tool_calls) for m in new_messages
            )
        else:
            outcome["passed"] = error is None and run.status == RunStatus.COMPLETED
        report["cases"].append(outcome)
        save(output / "summary.json", report)
        emit({"event": "case_completed", **{key: value for key, value in outcome.items()
                                             if key not in {"calls", "final_answer"}}})
        if error:
            break
    report["original_unchanged"] = digest(source) == original_hash and all(digest(p) == hashes[p.name] for p in paths)
    save(output / "summary.json", report)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=240)
    args = parser.parse_args()
    asyncio.run(replay(args.run.resolve(), args.output.resolve(), args.timeout))
