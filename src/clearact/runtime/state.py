from clearact.domain.enums import RunStatus
from clearact.domain.models import Run

_ALLOWED_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    RunStatus.CREATED: {RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED},
    RunStatus.RUNNING: {
        RunStatus.WAITING_APPROVAL,
        RunStatus.PAUSED,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    RunStatus.WAITING_APPROVAL: {RunStatus.RUNNING, RunStatus.PAUSED, RunStatus.FAILED, RunStatus.CANCELLED},
    RunStatus.PAUSED: {RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED},
    RunStatus.COMPLETED: set(),
    RunStatus.FAILED: set(),
    RunStatus.CANCELLED: set(),
}


def transition(run: Run, target: RunStatus) -> None:
    if target not in _ALLOWED_TRANSITIONS[run.status]:
        message = f"Cannot transition run from {run.status.value} to {target.value}."
        raise ValueError(message)
    run.status = target
