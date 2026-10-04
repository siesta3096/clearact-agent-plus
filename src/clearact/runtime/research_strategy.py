from __future__ import annotations

from clearact.domain.models import ResearchStrategy, Run

_NO_WEB = (
    "不要联网", "无需联网", "不需要联网", "不要搜索", "无需搜索", "不需要搜索", "不要上网",
    "只用本地", "仅用本地", "只用附件", "仅用附件", "offline", "without web", "no web",
    "do not browse the web", "don't browse the web", "do not search the web",
)
_EXPLICIT_WEB = (
    "联网", "网络搜索", "网页搜索", "搜索网页", "搜索资料", "查找资料", "网站", "网页", "互联网",
    "最新", "实时", "当前信息", "当前政策", "查证", "外部资料", "在线资料", "web search", "search online",
    "browse the web", "online sources", "latest", "current news", "up-to-date",
)
_LOCAL = (
    "附件", "上传", "文件", "工作区", "本地", "pdf", "文档", "文件夹", "目录",
    "attached", "attachment", "workspace", "local file", "folder", "document",
)
_LOCAL_SOURCE = (
    "根据文件", "读取文件", "这些文件", "文件夹里", "文件夹中的", "文件夹内", "目录中的",
    "目录里", "文件中的", "已有文件", "现有文件",
    "工作区里的", "工作区中的", "工作区文件", "本地资料", "本地文件", "read file", "using file",
    "先看文件", "先读文件", "先查看文件", "先读取文件", "先看工作区", "先查看工作区",
    "read the file first", "inspect the workspace first", "files in this folder",
)
_RESEARCH = ("调查", "调研", "研究", "资料来源", "research", "investigate")
_PREFER_LOCAL = (
    "优先使用已上传", "优先使用本地", "优先本地资料", "避免不必要的网页搜索",
    "prioritize the uploaded", "prioritize local", "avoid unnecessary web searches",
)
_FINISH_FROM_EVIDENCE = (
    "基于已经获取的资料尽快", "基于现有资料完成", "基于已有资料完成",
    "finish a useful result from the evidence already collected", "finish with current evidence",
)


def local_source_requested(text: str) -> bool:
    return any(term in text.casefold() for term in _LOCAL_SOURCE)


def classify_research_strategy(run: Run) -> ResearchStrategy:
    """Choose a conservative evidence strategy from the user's latest intent.

    The latest message can override an older topic goal. Attachments are still
    considered because a rewind may reuse them without uploading them again.
    """
    user_messages = [message for message in run.messages if message.role == "user"]
    latest = (user_messages[-1].content or "").casefold() if user_messages else ""
    original = (run.goal or "").casefold()
    combined = f"{original}\n{latest}"
    attachments = [
        item
        for message in user_messages
        for item in (message.metadata.get("attachments") or [])
        if isinstance(item, dict)
    ]
    latest_file_attachments = any(
        isinstance(item, dict) and item.get("kind") != "image"
        for item in ((user_messages[-1].metadata.get("attachments") or []) if user_messages else [])
    )
    has_local = bool(attachments) or any(term in combined for term in _LOCAL)
    latest_no_web = any(term in latest for term in _NO_WEB)
    latest_web = any(term in latest for term in _EXPLICIT_WEB)
    original_no_web = any(term in original for term in _NO_WEB)
    original_web = any(term in original for term in _EXPLICIT_WEB)
    latest_local = any(term in latest for term in _LOCAL)

    if latest_no_web or (original_no_web and not latest_web):
        return ResearchStrategy(mode="local_only", search_streak_limit=0, total_search_limit=0)
    if any(term in latest for term in _FINISH_FROM_EVIDENCE):
        return ResearchStrategy(mode="local_only", search_streak_limit=0, total_search_limit=0)
    if has_local and any(term in latest for term in _PREFER_LOCAL):
        needs_read = any(item.get("kind") != "image" for item in attachments) or local_source_requested(combined)
        return ResearchStrategy(
            mode="local_first", search_streak_limit=2, total_search_limit=2,
            requires_local_evidence=needs_read,
        )
    if latest_web or (original_web and not (latest_local and latest != original)):
        if has_local:
            return ResearchStrategy(
                mode="mixed", search_streak_limit=4, total_search_limit=8,
                requires_local_evidence=latest_file_attachments or local_source_requested(latest or original),
            )
        return ResearchStrategy(mode="research", search_streak_limit=5)
    if has_local:
        needs_read = any(item.get("kind") != "image" for item in attachments) or local_source_requested(combined)
        return ResearchStrategy(
            mode="local_first",
            search_streak_limit=2,
            total_search_limit=2,
            requires_local_evidence=needs_read,
        )
    if any(term in combined for term in _RESEARCH):
        return ResearchStrategy(mode="research", search_streak_limit=5)
    return ResearchStrategy(mode="balanced", search_streak_limit=3, total_search_limit=6)
