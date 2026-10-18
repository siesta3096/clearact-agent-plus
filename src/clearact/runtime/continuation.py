import re

from clearact.domain.models import Run


def continues_unfinished_plan(run: Run) -> bool:
    """Retain pending checks for explicit execution follow-ups, not discussion.

    This only selects verification scope. It never grants tool permissions.
    Ambiguous questions remain discussion rather than triggering extra work.
    """
    reached = {step.plan_item_id for step in run.workflow_steps if step.status in {"completed", "recovered"}}
    reached.update(run.completed_plan_item_ids)
    if not any(item.id not in reached for item in run.workflow_plan):
        return False
    latest = next((message.content or "" for message in reversed(run.messages) if message.role == "user"), "")
    text = re.sub(r"```[\s\S]*?```|`[^`]*`|[“\"][^”\"]*[”\"]", "", latest).casefold()
    for clause in re.split(r"[，,。.;；!！\n]", text):
        clause = clause.strip()
        if not clause or re.search(r"不要|不用|无需|不必|禁止|先别|别再|do not|don't|never", clause):
            continue
        if re.search(r"^(?:(?:请|先|帮我|你|麻烦|please)\s*)*"
                     r"(?:解释|说明|分析|总结|讨论|告诉我|explain|describe|discuss)",
                     clause) or re.search(r"为什么|如何|怎么|什么|是否|\b(?:why|how|whether)\b", clause):
            continue
        if re.search(r"实现|执行|继续|开始|重做|重试|重跑|补做|创建|写入|保存|制作|生成|做出来|"
                     r"\b(?:implement|execute|continue|resume|retry|build|create|write|save|generate|finish)\b",
                     clause):
            return True
    return False
