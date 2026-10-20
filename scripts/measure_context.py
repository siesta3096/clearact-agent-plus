"""Measure deterministic request sizes without a model, user files or private configuration."""

import argparse
import json
from pathlib import Path

from clearact import __version__, cli
from clearact.context.budget import ContextBudget
from clearact.context.builder import ContextBuilder
from clearact.domain.models import Action, ChatMessage
from clearact.runtime.prompting import system_prompt


def measure() -> dict:
    tools = [tool.definition() for tool in cli._builtin_tools()]
    budget = ContextBudget(128_000, .8)
    scenarios = {}
    for name, rounds, thinking in [("returned_thinking", 2, 8000), ("long_tool_history", 20, 0)]:
        messages = [ChatMessage(role="system", content=system_prompt("整理工作区文件")),
                    ChatMessage(role="user", content="整理工作区文件，不要联网，不要覆盖原始文件。")]
        for index in range(rounds):
            action = Action(id=f"read_{index}", tool_name="read_file", arguments={"path": f"note_{index}.txt"})
            messages.extend([
                ChatMessage(role="assistant", reasoning_content="r" * thinking or None, tool_calls=[action]),
                ChatMessage(role="tool", name="read_file", tool_call_id=action.id, content="sample " * 280),
            ])
        messages.append(ChatMessage(role="user", content="继续整理，保留此前限制。"))
        builder = ContextBuilder(budget)
        selected, offered = builder.build(messages, tools, set())
        scenarios[name] = {"input_estimated_units": budget.estimate(selected, offered),
                           "messages_sent": len(selected), "messages_recorded": len(messages),
                           "reasoning_characters_sent": sum(len(message.reasoning_content or "")
                                                            for message in selected)}
    return {"version": __version__, "metric": "conservative UTF-8 request units, not tokenizer tokens",
            "system_prompt_characters": len(system_prompt("整理工作区文件")), "scenarios": scenarios,
            "model_requests_sent": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    data = json.dumps(measure(), ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(data, encoding="utf-8", newline="\n")
    print(data)
