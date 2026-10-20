"""Replay a saved context failure without a model call or changes to the original run/workspace."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from clearact import cli
from clearact.context.budget import ContextBudget, ContextBudgetError
from clearact.context.builder import ContextBuilder
from clearact.context.compact import message_groups
from clearact.runtime.delivery import infer_contract
from clearact.settings import load_settings
from clearact.storage.run_store import RunStore


def replay(run_id: str) -> dict:
    settings = load_settings(cli._project_root())
    store = RunStore(settings.data_root)
    run = store.load_run(run_id)
    original = hashlib.sha256(run.model_dump_json().encode()).hexdigest()
    messages = list(run.messages)
    if messages and messages[-1].role == "assistant" and not messages[-1].tool_calls and (
        "ContextBudgetError" in (messages[-1].content or "")
    ):
        messages.pop()
    groups = message_groups(messages)
    latest_transaction = next(group for group in reversed(groups) if group[0].tool_calls)
    events = store.load_events(run_id)
    names = next(event.data["tool_names"] for event in reversed(events)
                 if event.type == "model.waiting" and event.data.get("tool_names"))
    definitions = {tool.name: tool.definition() for tool in cli._builtin_tools()}
    if any(name not in definitions for name in names):
        raise ValueError("Replay needs unavailable non-native tool definitions; no request was sent")
    tools = [definitions[name] for name in names]
    profile = settings.models["profiles"][run.execution.profile or settings.models["default_profile"]]
    budget = ContextBudget(profile["context_window"], settings.agent.context_budget_ratio)
    full_error = None
    try:
        ContextBuilder(budget, reasoning_history="full").build(messages, tools, set())
    except ContextBudgetError as exc:
        full_error = {"estimated": exc.estimated, "limit": exc.limit}
    automatic = ContextBuilder(budget)
    selected, _ = automatic.build(messages, tools, set())
    action_ids = {action.id for message in latest_transaction for action in message.tool_calls}
    assert action_ids <= {action.id for message in selected for action in message.tool_calls}
    assert action_ids <= {message.tool_call_id for message in selected if message.role == "tool"}
    assert [message for message in selected if message.role in {"system", "user"}] == [
        message for message in messages if message.role in {"system", "user"}
    ]
    assert not budget.exceeds(selected, tools)
    assert hashlib.sha256(run.model_dump_json().encode()).hexdigest() == original
    assert hashlib.sha256(store.load_run(run_id).model_dump_json().encode()).hexdigest() == original
    return {"run_id": run_id, "recorded_status": run.status.value, "status_question_mode": infer_contract(run).mode,
            "full_replay_failure": full_error, "automatic": asdict(automatic.last_build_report),
            "selected_estimated_units": budget.estimate(selected, tools), "limit": budget.limit,
            "latest_action_ids_preserved": True, "user_and_system_messages_preserved": True,
            "original_record_unchanged": True, "model_requests_sent": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = replay(args.run_id)
    data = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(data, encoding="utf-8", newline="\n")
    print(data)
