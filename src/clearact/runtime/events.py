from clearact.domain.enums import RiskLevel, Stage
from clearact.domain.models import Action, Run, RunEvent


def run_started(run: Run) -> RunEvent:
    return RunEvent(type="run.started", run_id=run.id, title="正在理解任务", stage=Stage.UNDERSTAND)


def action_started(run: Run, action: Action, stage: Stage, risk: RiskLevel, title: str, detail: str) -> RunEvent:
    return RunEvent(
        type="action.started",
        run_id=run.id,
        action_id=action.id,
        stage=stage,
        risk=risk,
        title=title,
        detail=detail,
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


def model_reasoning(run: Run, detail: str, stage: Stage) -> RunEvent:
    """Record reasoning text the provider explicitly returned for this turn."""
    return RunEvent(
        type="model.reasoning",
        run_id=run.id,
        stage=stage,
        title="模型返回的推理内容",
        detail=detail,
    )


def run_completed(run: Run) -> RunEvent:
    return RunEvent(type="run.completed", run_id=run.id, title="任务已完成", stage=Stage.DELIVER)
