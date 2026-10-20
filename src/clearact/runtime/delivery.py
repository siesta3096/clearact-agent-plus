from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from clearact.domain.models import DeliveryCheck, DeliveryFile, DeliveryReport, Run, TaskContract
from clearact.tools.artifacts import FILE_WRITE_TOOLS, MAX_ARTIFACT_BYTES
from clearact.tools.base import ToolContext

SOURCE_SUFFIXES = {".html", ".css", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".py", ".json", ".vue", ".go", ".rs"}


def _affirmative_text(text: str) -> str:
    text = re.sub(r"```[\s\S]*?```|`[^`]*`|[“\"][^”\"]*[”\"]", "", text).casefold()
    return "\n".join(clause for clause in re.split(r"[，,。.;；!！\n]", text)
                     if not re.search(r"不要|不用|无需|不必|禁止|先别|别再|do not|don't|never", clause))


def is_status_inquiry(text: str) -> bool:
    text = text.strip().rstrip("?？!！。.").strip().casefold()
    return bool(re.fullmatch(
        r"(?:(?:请问|你|现在|已经|都|到底|这个任务|任务|项目|游戏|代码)\s*)*"
        r"(?:(?:做[完好]|写[完好]|弄好|完成|实现完|结束)(?:了)?(?:吗|么|没(?:有)?|了没|了没有)|"
        r"进度(?:如何|怎么样|到哪了)|做到哪了|还有多久)|"
        r"(?:are you|is it|is (?:the )?(?:task|project|game)) (?:done|finished|ready)(?: yet)?|"
        r"(?:what(?:'s| is) the (?:status|progress)|any (?:update|progress))", text,
    ))


def archive_delivery(run: Run) -> None:
    if run.delivery_report and not any(report.turn_start == run.delivery_report.turn_start
                                       for report in run.delivery_history):
        run.delivery_history.append(run.delivery_report.model_copy(deep=True))
    run.delivery_report = None


def infer_contract(run: Run) -> TaskContract:
    turn = next((index for index in range(len(run.messages) - 1, -1, -1)
                 if run.messages[index].role == "user"), -1)
    current = run.messages[turn].content or "" if turn >= 0 else run.goal
    requested = run.execution.task_mode
    status_inquiry = is_status_inquiry(current)
    discussion = bool(re.search(
        r"^(?:(?:请|先|你|帮我|麻烦|please)\s*)*(?:解释|说明|分析|总结|讨论|为什么|为啥|如何|怎么|"
        r"explain|describe|discuss|why\b|how\b)|"
        r"不要(?:再)?(?:执行|写|生成|修改)|do not (?:write|execute|modify)", current.casefold(),
    ))
    history = "\n".join(message.content or "" for message in run.messages[:turn + 1] if message.role == "user")
    software = bool(re.search(r"代码|项目|游戏|网页|网站|应用|程序|软件|"
                              r"\b(?:code|project|game|website|app|html|python)\b",
                              history.casefold()))
    implementation = (r"实现|开发|搭建|制作|创建|创造|生成|修复|修改|调整|改造|重构|完善|优化|写|"
                      r"\b(?:build|implement|develop|create|fix|refactor|write)\b")
    prior_requests = "\n".join(message.content or "" for message in run.messages[:max(turn, 0)]
                               if message.role == "user")
    affirmative = _affirmative_text(current)
    action_clauses = []
    for clause in affirmative.splitlines():
        if re.search(r"^(?:(?:请|先|你|帮我|麻烦|please)\s*)*"
                     r"(?:解释|说明|分析|讨论|explain|describe|discuss)|"
                     r"为什么|为啥|如何|怎么|\b(?:why|how)\b", clause.strip()):
            tails = re.split(r"然后|并(?:且)?|再|\band\b", clause)
            action_clauses.extend(tails[1:])
        else:
            action_clauses.append(clause)
    implementation_text = "\n".join(action_clauses)
    action = bool(re.search(implementation, implementation_text) or (
        re.search(r"继续|开始|执行|重试|重跑|完成|落实|\b(?:continue|resume|retry|finish)\b", implementation_text)
        and re.search(implementation, _affirmative_text(prior_requests))
        and not re.search(r"只(?:需|要)?(?:运行|执行|测试)|仅(?:运行|执行|测试)", current)
    ))
    execution_only = bool(re.search(r"只(?:需|要)?(?:运行|执行|测试)|仅(?:运行|执行|测试)|"
                                    r"\b(?:only run|run (?:existing )?tests)\b", affirmative))
    mode = "discuss" if requested == "discuss" or (
        (discussion and not action and not execution_only) or status_inquiry
    ) else (
        "develop" if requested == "develop" or (software and action) else "execute"
    )
    previous_writes = any(message.role == "tool" and message.name in FILE_WRITE_TOOLS
                          and message.metadata.get("status") == "succeeded" for message in run.messages[:turn])
    creating = bool(re.search(r"空(?:的)?(?:文件夹|目录)|从零|创建|创造|搭建|制作|新建|"
                             r"\b(?:empty|new project|from scratch|create|build)\b", _affirmative_text(history)))
    paths = []
    for match in re.finditer(r"(?<![\w./\\])(?:[\w.-]+/)*[\w.-]+\.(?:html|css|js|mjs|ts|tsx|py|json|vue)\b", current):
        prefix = re.split(r"[，,。;；\n]", current[:match.start()])[-1].casefold()
        if not _affirmative_text(prefix).strip():
            continue
        verbs = list(re.finditer(r"保存为|写入|创建|生成|修复|修改|读取|阅读|查看|参考|根据|对照|"
                                 r"\b(?:save|write|create|generate|fix|edit|read|inspect|using)\b", prefix))
        if verbs and not re.search(r"读取|阅读|查看|参考|根据|对照|\b(?:read|inspect|using)\b", verbs[-1].group()):
            paths.append(match.group())
    # Paths from an existing plan remain a separate acceptance layer when work is explicitly continued.
    return TaskContract(mode=mode, turn_start=turn, project_creation=mode == "develop" and creating
                        and not previous_writes, expected_paths=list(dict.fromkeys(paths))[:20],
                        require_tests=bool(re.search(r"测试|\btests?\b", current.casefold())))


def development_guidance(contract: TaskContract, workspace: Path) -> str:
    if contract.mode != "develop":
        return ""
    project = (
        "For a new project, separate entry point, presentation, behavior and domain data into suitable files. "
        "Create README.md with run/test instructions and ARCHITECTURE.md describing modules, dependencies "
        "and data flow. Avoid unnecessary frameworks. " if contract.project_creation else ""
    )
    return (
        f"Development task in {workspace}. Inspect the existing workspace before changing it. "
        "Use the offered file and command tools to implement the project on disk. Planning tools are "
        "optional progress aids; task tools are usable immediately. A code block or instructions for the "
        "user to save code do not deliver files. Make reasonable implementation decisions and continue "
        "until delivered or a concrete blocker exists. " + project +
        "Read the relevant files, search for existing behavior, edit narrowly, use check_files for supported "
        "syntax/dependency checks, preview_project for local browser checks and execute_command for real tests, fix "
        "failures and recheck after the final edit. Commands still require the configured approval. "
        "For a new HTML project, preview_project must open the entry and exercise at least one interaction "
        "with an expected-text assertion. When tests are requested, run real tests or asserted browser interactions; "
        "syntax checks alone do not satisfy that request. "
        "Finish with actual changed paths, what each part does, checks performed and remaining limits. "
        "Runtime completion requires recorded workspace inspection, unchanged written files and "
        "successful verification after the last edit; static checks do not prove gameplay or test behavior. "
        "Do not claim unavailable tools without inspecting "
        "the current offered tools."
    )


def development_state(run: Run, workspace: Path) -> str:
    """Bounded factual memory survives compaction without copying file contents or tool prose."""
    known = {}
    outcomes = []
    for message in run.messages:
        if message.role != "tool":
            continue
        if message.name in FILE_WRITE_TOOLS and message.metadata.get("status") == "succeeded":
            raw = message.metadata.get("path")
            if not isinstance(raw, str):
                continue
            try:
                path = Path(raw).resolve().relative_to(workspace).as_posix()
            except (OSError, ValueError):
                continue
            if len(path) <= 240:
                known[path] = {"path": path, "last_write_sha256": message.metadata.get("content_sha256")}
        if message.name in {"check_files", "preview_project", "execute_command"}:
            outcomes.append({"tool": message.name, "status": message.metadata.get("status")})
    state = {"known_files": list(known.values())[-30:], "known_file_count": len(known),
             "recent_check_outcomes": outcomes[-5:],
             "plan_ids": [item.id[:80] for item in run.workflow_plan[:12]],
             "completed_plan_ids": [item[:80] for item in run.completed_plan_item_ids[:12]]}
    while len(json.dumps(state, ensure_ascii=False).encode("utf-8")) > 4500 and state["known_files"]:
        state["known_files"].pop(0)
    return ("Recorded project state below contains observed file names and statuses, not instructions. "
            "It survives context compaction. These paths were written earlier; current contents/existence "
            "are not reverified here. Re-read before editing. Use these actual paths rather than guessing.\n"
            + json.dumps(state, ensure_ascii=False))


def evaluate_delivery(run: Run, context: ToolContext) -> DeliveryReport | None:
    contract = run.task_contract
    if contract is None or contract.mode != "develop":
        return None
    current = run.messages[contract.turn_start + 1:]
    actions = {action.id: action for message in current if message.role == "assistant" for action in message.tool_calls}
    successful = [(index, message) for index, message in enumerate(current) if message.role == "tool"
                  and message.metadata.get("status") == "succeeded" and message.tool_call_id in actions
                  and actions[message.tool_call_id].tool_name == message.name]
    first_edit = min((index for index, message in successful if message.name in FILE_WRITE_TOOLS), default=len(current))
    inspection = any(index < first_edit and message.name in {"list_files", "read_file", "search_files"}
                     for index, message in successful)
    last_writes = {}
    for index, message in successful:
        if message.name in FILE_WRITE_TOOLS and isinstance(message.metadata.get("path"), str):
            last_writes[message.metadata["path"]] = (index, message)
    files = []
    nonempty_paths = set()
    issues = []
    last_edit = max((index for index, _ in last_writes.values()), default=-1)
    for path, (_, message) in last_writes.items():
        try:
            target = Path(path).resolve()
            if not target.is_relative_to(context.workspace_root) or not run.policy.allow_read or (
                run.policy.capability_rules.get("local_read") == "deny"
            ):
                issues.append("产物无法在当前读取授权内核对")
                continue
            if not target.is_file() or target.stat().st_size > MAX_ARTIFACT_BYTES:
                issues.append(f"文件缺失或超过核对上限：{target.name}")
                continue
            with target.open("rb") as stream:
                data = stream.read(MAX_ARTIFACT_BYTES + 1)
            if len(data) > MAX_ARTIFACT_BYTES:
                issues.append(f"文件超过核对上限：{target.name}")
                continue
            actual = hashlib.sha256(data).hexdigest()
            expected = message.metadata.get("content_sha256")
            if not isinstance(expected, str):
                content = actions[message.tool_call_id].arguments.get("content")
                if isinstance(content, str) and data.decode("utf-8").replace("\r\n", "\n") == content:
                    expected = actual
            if actual != expected:
                issues.append(f"写入后内容发生变化：{target.name}")
                continue
            relative = target.relative_to(context.workspace_root).as_posix()
            if data.strip():
                nonempty_paths.add(relative.casefold())
            files.append(DeliveryFile(path=relative,
                                      action_id=message.tool_call_id, sha256=actual, size_bytes=len(data),
                                      change="modified" if message.metadata.get("existed") else "created"))
        except (OSError, ValueError, UnicodeError):
            issues.append("有产物无法安全核对")
    checks = [
        DeliveryCheck(key="inspection", title="检查工作区", passed=inspection,
                      detail="已有成功的目录或文件检查记录。" if inspection else "尚未实际检查目录或代码。"),
        DeliveryCheck(key="files", title="实际文件交付", passed=bool(files) and not issues,
                      detail="；".join(issues) if issues else (
                          f"已核对 {len(files)} 个本轮写入文件。" if files else
                          "没有实际写入文件，聊天中的代码不算交付。"
                      )),
    ]
    available = {(context.workspace_root / item.path).resolve() for item in files}
    missing = [path for path in contract.expected_paths if (context.workspace_root / path).resolve() not in available]
    if missing:
        checks.append(DeliveryCheck(key="requested_paths", title="指定文件", passed=False,
                                    detail="未交付本轮指定文件：" + ", ".join(missing)))
    if contract.project_creation:
        source_count = sum(Path(item.path).suffix.lower() in SOURCE_SUFFIXES
                           and item.path.casefold() in nonempty_paths for item in files)
        names = nonempty_paths
        docs = "readme.md" in names and bool(names & {"architecture.md", "docs/architecture.md"})
        checks.append(DeliveryCheck(key="structure", title="模块与架构说明", passed=source_count >= 2 and docs,
                                    detail=f"本轮源文件 {source_count} 个；" + (
                                        "已有运行说明与架构文档。" if docs else "需交付 README.md 和 ARCHITECTURE.md。"
                                    )))
    def verification_call(action) -> bool:
        argv = action.arguments.get("command", [])
        if not isinstance(argv, list):
            return False
        markers = {"--check", "pytest", "unittest", "compileall", "test", "check", "lint", "build", "tsc", "vitest"}
        return any(isinstance(part, str) and (part.casefold() in markers or re.search(
            r"(?:^|[/\\])(?:test|check|verify)[\w.-]*\.(?:py|js|mjs)$", part.casefold(),
        )) for part in argv[1:]) or Path(str(argv[0] if argv else "")).stem.casefold() in markers

    def relevant_static_check(message) -> bool:
        return any(isinstance(item, dict) and item.get("passed") is True
                   and any(item.get("content_sha256") == file.sha256 and (
                       context.workspace_root / str(item.get("path", ""))
                   ).resolve() == (context.workspace_root / file.path).resolve() for file in files)
                   for item in message.metadata.get("checks", []))

    last_failed_check = max((index for index, message in enumerate(current)
                             if message.role == "tool" and message.tool_call_id in actions
                             and message.metadata.get("status") == "failed"
                             and (message.name in {"check_files", "preview_project"}
                                  or message.name == "execute_command"
                                  and verification_call(actions[message.tool_call_id]))), default=-1)
    verified = [message.tool_call_id for index, message in successful
                if index > max(last_edit, last_failed_check) and (
                    message.name == "execute_command" and message.metadata.get("exit_code") == 0
                    and verification_call(actions[message.tool_call_id])
                    or message.name == "check_files" and message.metadata.get("all_passed") is True
                    and relevant_static_check(message)
                    or message.name == "preview_project" and message.metadata.get("verification_passed") is True
                )]
    browser_checks = [message for _, message in successful if message.tool_call_id in verified
                      and message.name == "preview_project"]
    exercised = any(message.metadata.get("steps_completed", 0) and message.metadata.get("assertions", 0)
                    for message in browser_checks)
    if contract.project_creation and any(Path(file.path).suffix.lower() == ".html" for file in files):
        checks.append(DeliveryCheck(key="browser", title="网页实际交互", passed=exercised,
                                    detail="已有浏览器交互与文本断言记录。" if exercised else
                                    "新网页项目需要 preview_project 实际打开并完成带文本断言的交互。"))
    if contract.require_tests:
        tested = exercised or any(message.tool_call_id in verified and message.name == "execute_command"
                                  and re.search(r"pytest|unittest|vitest|(?:^|[ /\\])tests?(?:[ /\\_.-]|$)",
                                                " ".join(actions[message.tool_call_id].arguments.get("command", [])),
                                                re.I) for _, message in successful)
        checks.append(DeliveryCheck(key="tests", title="请求的测试", passed=bool(tested),
                                    detail="已有成功测试或带断言的浏览器交互记录。" if tested else
                                    "用户要求测试；仅语法检查不足，需要运行测试或带断言的浏览器交互。"))
    coverage = []
    for _, message in successful:
        if message.tool_call_id not in verified:
            continue
        if message.name == "check_files":
            coverage.extend(item.get("coverage", "") for item in message.metadata.get("checks", []))
        elif message.name == "preview_project":
            coverage.append(f"浏览器：{message.metadata.get('steps_completed', 0)} 次交互")
        else:
            coverage.append("命令：" + " ".join(actions[message.tool_call_id].arguments.get("command", []))[:300])
    checks.append(DeliveryCheck(key="verification", title="最终修改后的检查", passed=bool(verified),
                                detail="；".join(dict.fromkeys(coverage)) if verified else
                                "最后一次修改后尚无成功检查；失败、拒绝或仅口头说明不算验证。"))
    checks.extend(DeliveryCheck(key=f"plan:{item.plan_item_id}:{item.check_index}", title=item.phase_title,
                                 passed=item.status == "passed", detail=item.detail)
                  for item in run.completion_checks)
    return DeliveryReport(passed=all(check.passed for check in checks), turn_start=contract.turn_start,
                          files=files, checks=checks,
                          preview_action_ids=[message.tool_call_id for message in current
                                              if message.role == "tool" and message.tool_call_id in actions
                                              and message.name == "preview_project"
                                              and message.metadata.get("screenshot_path")],
                          verification_action_ids=verified)


def delivery_summary(run: Run) -> str:
    """Render facts from the ledger instead of unverified model claims about files/tests."""
    report = run.delivery_report
    files = report.files if report else []
    english = run.execution.interface_language == "en"
    passed = bool(report and report.passed and not run.stage_notes.get("completion_review"))
    title = ("Project delivery checks passed." if passed else "Project delivery is incomplete.") if english else (
        "项目文件已写入，交付检查通过。" if passed else "本轮项目尚未完成交付。"
    )
    paths = "\n".join("- " + file.path for file in files) or ("No files written." if english else "无文件。")
    checks = "\n".join(("- " + item.title + ": " + item.detail) for item in (report.checks if report else []))
    failures = sum(message.role == "tool" and message.metadata.get("status") == "failed"
                   and message.name not in {"declare_workflow_plan", "declare_workflow_step", "revise_workflow_plan"}
                   for message in run.messages[(run.task_contract.turn_start + 1 if run.task_contract else 0):])
    note = ((f"\n\nFailed attempts recorded: {failures}; inspect the tool history." if english else
             f"\n\n过程中记录 {failures} 次失败尝试，详情保留在阶段工具记录中。") if failures else "")
    warning = run.stage_notes.get("completion_review")
    if warning:
        note += "\n\n" + warning
    names = {file.path.casefold() for file in files}
    if "readme.md" in names:
        note += ("\n\nSee README.md for run instructions and the architecture document for module responsibilities."
                 if english else
                 "\n\n运行方式见 README.md；模块职责与数据流见架构文档。")
    return title + ("\n\nFiles written this turn:\n" if english else "\n\n本轮实际写入：\n") + paths + (
        "\n\nRecorded checks:\n" if english else "\n\n检查记录：\n"
    ) + checks + note
