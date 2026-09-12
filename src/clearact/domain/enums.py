from enum import StrEnum


class RiskLevel(StrEnum):
    WHITE = "white"
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"

    @property
    def rank(self) -> int:
        return list(type(self)).index(self)


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Stage(StrEnum):
    UNDERSTAND = "understand"
    COLLECT = "collect"
    ANALYZE = "analyze"
    PREPARE = "prepare"
    ACT = "act"
    DELIVER = "deliver"


class DecisionOutcome(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class ViewMode(StrEnum):
    SIMPLE = "simple"
    EXPERT = "expert"
