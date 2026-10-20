"""Small work should complete without planning round trips or fabricated extra deliverables."""

import asyncio

from test_development_agent import call, developer
from test_task_scenarios import ScriptedProvider, _run

from clearact.domain.enums import RunStatus
from clearact.domain.models import LLMResponse


def test_small_file_task_writes_immediately_and_finishes_in_two_requests(workspace):
    provider = ScriptedProvider([
        LLMResponse(tool_calls=[call("write_file", path="hello.txt", content="hello\n")]),
        LLMResponse(content="已保存 hello.txt。"),
    ])
    runner, _ = developer(workspace, provider)
    run = _run("创建 hello.txt，内容是 hello。")
    asyncio.run(runner.run(run))
    assert run.status is RunStatus.COMPLETED
    assert (workspace / "hello.txt").read_text() == "hello\n"
    assert len(provider.inputs) == 2
    assert run.workflow_plan == []
    assert [message.name for message in run.messages if message.role == "tool"] == ["write_file"]


def test_no_plan_does_not_allow_claiming_an_unwritten_explicit_file(workspace):
    (workspace / "report.txt").write_text("Original report", encoding="utf-8")
    provider = ScriptedProvider([
        LLMResponse(content="已写入 report.txt。"), LLMResponse(content="已写入 report.txt。"),
    ])
    runner, _ = developer(workspace, provider)
    run = _run("创建 report.txt，写入本次总结。")
    asyncio.run(runner.run(run))
    assert run.status is RunStatus.NEEDS_REVIEW
    assert "report.txt" in run.stage_notes["completion_review"]
    assert (workspace / "report.txt").read_text() == "Original report"
    assert len(provider.inputs) == 2


def test_single_file_project_needs_no_generated_architecture_or_extra_source_files(workspace):
    provider = ScriptedProvider([
        LLMResponse(tool_calls=[call("list_files", path="."),
                                call("write_file", path="app.py", content="print('hello')\n")]),
        LLMResponse(tool_calls=[call("check_files", paths=["app.py"])]),
        LLMResponse(content="已写入并检查。"),
    ])
    runner, _ = developer(workspace, provider)
    run = _run("创建一个单文件 Python 项目 app.py，打印 hello。")
    asyncio.run(runner.run(run))
    assert run.status is RunStatus.COMPLETED
    assert run.delivery_report.passed
    assert {file.path for file in run.delivery_report.files} == {"app.py"}
    assert not (workspace / "README.md").exists()
    assert not (workspace / "ARCHITECTURE.md").exists()
    assert len(provider.inputs) == 3


def test_explicit_architecture_file_is_still_required_when_user_asks_for_it(workspace):
    provider = ScriptedProvider([
        LLMResponse(tool_calls=[call("list_files", path="."),
                                call("write_file", path="app.py", content="print('hello')\n")]),
        LLMResponse(tool_calls=[call("check_files", paths=["app.py"])]),
        LLMResponse(content="完成。"), LLMResponse(content="完成。"),
    ])
    runner, _ = developer(workspace, provider)
    run = _run("创建 Python 项目 app.py，并创建 ARCHITECTURE.md。")
    asyncio.run(runner.run(run))
    assert run.status is RunStatus.NEEDS_REVIEW
    assert any(check.key == "requested_paths" and not check.passed for check in run.delivery_report.checks)


def test_literal_arithmetic_offers_no_tool_schemas(workspace):
    class ArithmeticProvider(ScriptedProvider):
        async def chat(self, messages, tools):
            assert tools == []
            return await super().chat(messages, tools)

    provider = ArithmeticProvider([LLMResponse(content="391")])
    runner, _ = developer(workspace, provider)
    run = _run("只回复整数结果：17 乘以 23 等于多少？")
    asyncio.run(runner.run(run))
    assert run.status is RunStatus.COMPLETED
    assert len(provider.inputs) == 1


def test_new_file_has_no_irrecoverable_preinspection_gate(workspace):
    provider = ScriptedProvider([
        LLMResponse(tool_calls=[call("write_file", path="app.py", content="print('hello')\n")]),
        LLMResponse(tool_calls=[call("check_files", paths=["app.py"])]),
        LLMResponse(content="完成。"),
    ])
    runner, _ = developer(workspace, provider)
    run = _run("创建一个单文件 Python 项目 app.py。")
    asyncio.run(runner.run(run))
    assert run.status is RunStatus.COMPLETED
    assert run.delivery_report.passed
    assert len(provider.inputs) == 3


def test_existing_file_overwrite_still_requires_inspection_and_only_one_repair(workspace):
    (workspace / "app.py").write_text("print('original')\n", encoding="utf-8")
    provider = ScriptedProvider([
        LLMResponse(tool_calls=[call("write_file", path="app.py", content="print('changed')\n")]),
        LLMResponse(tool_calls=[call("check_files", paths=["app.py"])]),
        LLMResponse(content="完成。"),
        LLMResponse(tool_calls=[call("list_files", path=".")]),
        LLMResponse(content="完成。"),
    ])
    runner, _ = developer(workspace, provider)
    run = _run("修复 app.py。")
    asyncio.run(runner.run(run))
    assert run.status is RunStatus.NEEDS_REVIEW
    assert not next(check for check in run.delivery_report.checks if check.key == "inspection").passed
    assert len(provider.inputs) == 5  # No second/third final-answer repair loop after later tool work.


def test_plain_text_task_omits_office_schemas_but_explicit_office_work_keeps_them(workspace):
    class ToolProvider(ScriptedProvider):
        def __init__(self, expected, responses):
            super().__init__(responses)
            self.expected = expected

        async def chat(self, messages, tools):
            names = {tool.name for tool in tools}
            assert ("create_document" in names) is self.expected
            assert ("create_spreadsheet" in names) is self.expected
            return await super().chat(messages, tools)

    plain = ToolProvider(False, [
        LLMResponse(tool_calls=[call("write_file", path="hello.txt", content="hello")]),
        LLMResponse(content="已保存。"),
    ])
    runner, _ = developer(workspace, plain)
    asyncio.run(runner.run(_run("创建 hello.txt，不要 Word 文档或 Excel 表格。")))
    office = ToolProvider(True, [LLMResponse(content="请提供需要写入的内容。")])
    runner, _ = developer(workspace, office)
    asyncio.run(runner.run(_run("根据 hello.txt 生成 Word 文档和 Excel 表格。")))


def test_rereading_a_missing_excerpt_middle_returns_the_original_cached_text(workspace):
    from clearact.context.budget import ContextBudget
    from clearact.context.builder import ContextBuilder

    (workspace / "a.txt").write_text("HEADER\n" + "x" * 4000 + "MIDDLE_MARKER" + "y" * 4000 + "\nTAIL",
                                     encoding="utf-8", newline="\n")
    (workspace / "b.txt").write_text("other source", encoding="utf-8")
    provider = ScriptedProvider([
        LLMResponse(tool_calls=[call("read_file", path="a.txt")]),
        LLMResponse(tool_calls=[call("read_file", path="b.txt")]),
        LLMResponse(tool_calls=[call("read_file", path="a.txt", offset=4007, max_chars=13)]),
        LLMResponse(content="MIDDLE_MARKER"),
    ])
    runner, _ = developer(workspace, provider)
    runner._context_builder = ContextBuilder(ContextBudget(128000, .8), history_target_units=12000)
    run = _run("读取 a.txt 和 b.txt，再检查 a.txt 的中间标记。")
    asyncio.run(runner.run(run))
    assert any(message.metadata.get("context_excerpted") for message in provider.inputs[2])
    reused = [message for message in provider.inputs[3] if message.name == "read_file"
              and message.metadata.get("reason") == "covered_read"]
    assert reused and "MIDDLE_MARKER" in reused[-1].content
    original = next(message for message in run.messages if message.name == "read_file"
                    and message.metadata.get("status") == "succeeded")
    assert "MIDDLE_MARKER" in original.content and len(original.content) > 8000


def test_split_multifile_reads_keep_all_observed_values_across_compaction(workspace):
    import json

    from clearact.context.budget import ContextBudget
    from clearact.context.builder import ContextBuilder

    expected = {f"entry_{index:02}": 1007 + index * 37 for index in range(12)}
    for index, (key, value) in enumerate(expected.items()):
        (workspace / f"input_{index:02}.json").write_text(json.dumps({
            "id": key, "value": value, "padding": "background " * 280,
        }, indent=2), encoding="utf-8")

    class ValueProvider(ScriptedProvider):
        async def chat(self, messages, tools):
            if len(self.inputs) == 4:
                observed = "\n".join(message.content or "" for message in messages if message.role == "tool")
                for key, value in expected.items():
                    assert f'"id": "{key}"' in observed
                    assert f'"value": {value}' in observed
                assert any(message.metadata.get("context_excerpted") for message in messages)
            return await super().chat(messages, tools)

    reads = [call("read_file", path=f"input_{index:02}.json") for index in range(12)]
    provider = ValueProvider([
        *[LLMResponse(tool_calls=reads[start:end]) for start, end in [(0, 3), (3, 7), (7, 11), (11, 12)]],
        LLMResponse(tool_calls=[call("write_file", path="summary.json", content=json.dumps(expected))]),
        LLMResponse(content="已汇总。"),
    ])
    runner, _ = developer(workspace, provider)
    runner._context_builder = ContextBuilder(ContextBudget(100000, .8))
    run = _run("读取全部 input 文件，将所有 id 和 value 写入 summary.json。")
    asyncio.run(runner.run(run))
    assert run.status is RunStatus.COMPLETED
    assert json.loads((workspace / "summary.json").read_text()) == expected
    assert sum(message.name == "read_file" for message in run.messages) == 12
