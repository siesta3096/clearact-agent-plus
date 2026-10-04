from clearact.domain.enums import RiskLevel, Stage
from clearact.domain.models import Action, Run, RunEvent


def run_started(run: Run) -> RunEvent:
    return RunEvent(type="run.started", run_id=run.id, title="正在理解任务", stage=Stage.UNDERSTAND)


def action_started(
    run: Run, action: Action, stage: Stage, risk: RiskLevel, title: str, detail: str,
    *, policy_outcome: str | None = None, policy_reason: str | None = None,
) -> RunEvent:
    return RunEvent(
        type="action.started",
        run_id=run.id,
        action_id=action.id,
        stage=stage,
        risk=risk,
        title=title,
        detail=detail,
        data={"policy_outcome": policy_outcome, "policy_reason": policy_reason},
    )


def action_completed(run: Run, action: Action, stage: Stage, risk: RiskLevel, detail: str) -> RunEvent:
    return RunEvent(
        type="action.completed",
        run_id=run.id,
        action_id=action.id,
        stage=stage,
        risk=risk,
        title="已完成",
        detail=detail,
    )


def action_failed(run: Run, action: Action, stage: Stage, risk: RiskLevel, detail: str) -> RunEvent:
    return RunEvent(
        type="action.failed",
        run_id=run.id,
        action_id=action.id,
        stage=stage,
        risk=risk,
        title="操作失败",
        detail=detail,
    )


def action_not_run(
    run: Run, action: Action, stage: Stage, risk: RiskLevel, *, outcome: str, reason: str
) -> RunEvent:
    if outcome not in {"skipped", "denied"}:
        raise ValueError("Unsupported non-execution outcome.")
    return RunEvent(
        type=f"action.{outcome}",
        run_id=run.id,
        action_id=action.id,
        stage=stage,
        risk=risk,
        title="已跳过" if outcome == "skipped" else "未执行",
        detail=reason,
        data={"outcome": outcome},
    )


def approval_required(run: Run, action: Action, risk: RiskLevel, detail: str) -> RunEvent:
    return RunEvent(
        type="approval.required",
        run_id=run.id,
        action_id=action.id,
        stage=Stage.ACT,
        risk=risk,
        title="等待你的确认",
        detail=detail,
    )


def model_reasoning(run: Run, detail: str, stage: Stage, action_id: str | None = None) -> RunEvent:
    """Record reasoning text the provider explicitly returned for this turn."""
    return RunEvent(
        type="model.reasoning",
        run_id=run.id,
        stage=stage,
        action_id=action_id,
        title="模型返回的推理内容",
        detail=detail,
    )


def completion_review_requested(
    run: Run, pending_titles: list[str], missing_paths: list[str], changed_paths: list[str] | None = None,
    unworked_titles: list[str] | None = None,
) -> RunEvent:
    language = run.execution.interface_language or "zh"
    changed_paths = changed_paths or []
    unworked_titles = unworked_titles or []
    return RunEvent(
        type="completion.review_requested",
        run_id=run.id,
        stage=Stage.DELIVER,
        title="正在核对完成条件" if language == "zh" else "Checking completion conditions",
        detail=(
            f"未进入的计划阶段：{', '.join(pending_titles) or '无'}；待核对文件：{', '.join(missing_paths) or '无'}；"
            f"内容已变化：{', '.join(changed_paths) or '无'}；"
            f"尚无实际操作：{', '.join(unworked_titles) or '无'}。"
            if language == "zh" else
            f"Unreached phases: {', '.join(pending_titles) or 'none'}; "
            f"files to verify: {', '.join(missing_paths) or 'none'}; "
            f"changed files: {', '.join(changed_paths) or 'none'}; "
            f"phases without actions: {', '.join(unworked_titles) or 'none'}."
        ),
        data={"pending_titles": pending_titles, "missing_paths": missing_paths,
              "changed_paths": changed_paths, "unworked_titles": unworked_titles},
    )


def model_retrying(
    run: Run,
    *,
    attempt: int,
    max_attempts: int,
    delay_seconds: float,
    detail: str,
    error_type: str,
) -> RunEvent:
    language = run.execution.interface_language or "zh"
    title = "模型连接中断，正在自动重连" if language == "zh" else "Model connection interrupted; reconnecting"
    return RunEvent(
        type="model.retrying",
        run_id=run.id,
        stage=Stage.UNDERSTAND,
        title=title,
        detail=detail,
        data={
            "attempt": attempt,
            "max_attempts": max_attempts,
            "delay_seconds": delay_seconds,
            "error_type": error_type,
        },
    )


def model_recovered(run: Run, attempts: int) -> RunEvent:
    language = run.execution.interface_language or "zh"
    return RunEvent(
        type="model.recovered",
        run_id=run.id,
        stage=Stage.UNDERSTAND,
        title="模型连接已恢复" if language == "zh" else "Model connection restored",
        data={"attempts": attempts},
    )


def run_completed(run: Run) -> RunEvent:
    return RunEvent(type="run.completed", run_id=run.id, title="任务已完成", stage=Stage.DELIVER)
