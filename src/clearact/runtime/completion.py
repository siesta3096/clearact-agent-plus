from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree

from clearact.domain.models import CompletionCheck, CompletionCheckResult, Run
from clearact.tools.artifacts import FILE_WRITE_TOOLS
from clearact.tools.base import ToolContext


def _office_structure(path: Path, kind: str) -> bool:
    """Check bounded core XML structure, not document layout or formula results."""
    core, expected_tag = (
        ("word/document.xml", "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}document")
        if kind == "docx" else
        ("xl/workbook.xml", "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}workbook")
    )
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) > 10000 or len(names) != len(set(names)):
                return False
            for name in ("[Content_Types].xml", core):
                entry = archive.getinfo(name)
                if entry.file_size > 16 * 1024 * 1024:
                    return False
                data = archive.read(name)
                if b"\x00" in data or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
                    return False
                root = ElementTree.fromstring(data)
                if name == core and root.tag != expected_tag:
                    return False
                if name == "[Content_Types].xml" and root.tag != (
                    "{http://schemas.openxmlformats.org/package/2006/content-types}Types"
                ):
                    return False
        return True
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, RuntimeError, ElementTree.ParseError):
        return False


def _file_check(check: CompletionCheck, run: Run, context: ToolContext, current_messages) -> tuple[str, str]:
    raw = Path(check.path).expanduser()
    target = (raw if raw.is_absolute() else context.workspace_root / raw).resolve()
    if not target.is_relative_to(context.workspace_root):
        return "unverified", f"文件超出本次工作区，未读取：{check.path}"
    if not run.policy.allow_read or run.policy.capability_rules.get("local_read") == "deny":
        return "unverified", f"读取权限已关闭，无法核对文件：{check.path}"
    actions = {action.id: action for message in current_messages if message.role == "assistant"
               for action in message.tool_calls}
    writes = []
    for message in current_messages:
        action = actions.get(message.tool_call_id)
        if message.role != "tool" or message.name not in FILE_WRITE_TOOLS or not action:
            continue
        if message.metadata.get("status") != "succeeded" or message.metadata.get("path") != str(target):
            continue
        raw_action_path = action.arguments.get("path")
        if not isinstance(raw_action_path, str):
            continue
        candidate = Path(raw_action_path).expanduser()
        recorded = (candidate if candidate.is_absolute() else context.workspace_root / candidate).resolve()
        if recorded == target and action.tool_name == message.name:
            writes.append((message, action))
    if not writes:
        return "failed", f"本轮没有该文件的成功生成或写入记录：{check.path}"
    if not target.is_file():
        return "failed", f"未找到本轮产物：{check.path}"
    before = target.stat()
    if before.st_size > 32 * 1024 * 1024:
        return "unverified", f"产物超出 32 MB 自动核对限额：{check.path}"
    message, action = writes[-1]
    with target.open("rb") as stream:
        data = stream.read(32 * 1024 * 1024 + 1)
    if len(data) > 32 * 1024 * 1024:
        return "unverified", f"产物超出自动核对限额：{check.path}"
    expected_hash = message.metadata.get("content_sha256")
    if isinstance(expected_hash, str):
        unchanged = hashlib.sha256(data).hexdigest() == expected_hash
    else:
        expected_text = action.arguments.get("content")
        unchanged = isinstance(expected_text, str) and (
            data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n") == expected_text
        )
    if not unchanged:
        return "failed", f"产物在写入后发生变化，未覆盖用户修改：{check.path}"
    kind = check.format if check.format != "auto" else target.suffix.lstrip(".").lower()
    if kind in {"docx", "xlsx"} and not _office_structure(target, kind):
        return "failed", f"产物不是可识别的 {kind.upper()} 核心结构：{check.path}"
    after = target.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
    ):
        return "unverified", f"核对期间文件发生变化，请重新检查：{check.path}"
    return "passed", f"已核对本轮产物及写入内容：{check.path}"


def _source_key(url: object) -> str | None:
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url.strip())
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            return None
        scheme, host, port = parts.scheme.lower(), parts.hostname.lower().rstrip("."), parts.port
        host = f"[{host}]" if ":" in host else host
        authority = f"{host}:{port}" if port and port != {"http": 80, "https": 443}[scheme] else host
        return urlunsplit((scheme, authority, parts.path or "/", parts.query, ""))
    except ValueError:
        return None


def evaluate_completion_checks(
    run: Run, context: ToolContext, *, include_unreached: bool,
) -> list[CompletionCheckResult]:
    """Use current-turn recorded evidence only; never run a tool or contact a model."""
    turn = next((index for index in range(len(run.messages) - 1, -1, -1)
                 if run.messages[index].role == "user"), -1)
    current = run.messages[turn + 1:]
    actions = {action.id: action for message in current if message.role == "assistant" for action in message.tool_calls}
    successful = {message.tool_call_id: message for message in current
                  if message.role == "tool" and message.tool_call_id in actions
                  and actions[message.tool_call_id].tool_name == message.name
                  and message.metadata.get("status") == "succeeded"}
    current_phases = {step.plan_item_id for step in run.workflow_steps
                      if step.start_message_index is not None and step.start_message_index >= turn}
    old_phases = {step.plan_item_id for step in run.workflow_steps} - current_phases
    sources = set()
    for action_id, message in successful.items():
        action = actions[action_id]
        if action.tool_name == "fetch_url":
            source = _source_key(message.metadata.get("url") or action.arguments.get("url"))
        elif action.tool_name == "computer_use" and action.arguments.get("action") == "inspect":
            source = _source_key(message.metadata.get("url"))
        else:
            source = None
        if source:
            sources.add(source)
    results = []
    for item in run.workflow_plan:
        if item.id in old_phases or (item.id not in current_phases and not include_unreached):
            continue
        for index, check in enumerate(item.checks):
            try:
                if check.type == "file":
                    status, detail = _file_check(check, run, context, current)
                elif check.type == "tool_success":
                    count = sum(message.name == check.tool_name and (
                        check.tool_name != "execute_command" or message.metadata.get("exit_code") == 0
                    ) for message in successful.values())
                    status = "passed" if count >= check.min_count else "failed"
                    detail = f"本轮 {check.tool_name} 成功 {count} 次，要求至少 {check.min_count} 次。"
                else:
                    status = "passed" if len(sources) >= check.min_count else "failed"
                    detail = f"本轮实际打开 {len(sources)} 个不同网页来源，要求至少 {check.min_count} 个。"
            except (OSError, UnicodeError, ValueError, RuntimeError):
                status, detail = "unverified", "无法安全核对该条件，请检查文件状态和访问权限。"
            results.append(CompletionCheckResult(
                plan_item_id=item.id, phase_title=item.title, check_index=index,
                type=check.type, status=status, detail=detail,
            ))
    return results
