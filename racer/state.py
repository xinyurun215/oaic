from dataclasses import dataclass, replace
from enum import StrEnum, unique

from .errors import StateTransitionError
from .exit_codes import ExitCode
from .jsonio import JsonValue


@unique
class RunStatus(StrEnum):
    READY = "READY"
    RUNNING = "RUNNING"
    INTERRUPTED = "INTERRUPTED"
    FAILED = "FAILED"
    SUCCEEDED = "SUCCEEDED"


@unique
class StageStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    INTERRUPTED = "INTERRUPTED"
    FAILED = "FAILED"
    SUCCEEDED = "SUCCEEDED"


@dataclass(frozen=True)
class StageState:
    stage_id: str
    status: StageStatus
    attempts: int
    exit_code: int | None
    output: JsonValue


@dataclass(frozen=True)
class StageContext:
    job_id: str
    run_id: str
    generation: int
    stage_id: str
    attempt: int


@dataclass(frozen=True)
class StageResult:
    stage_id: str
    exit_code: int
    output: JsonValue


@dataclass(frozen=True)
class RunResult:
    job_id: str
    run_id: str
    generation: int
    status: RunStatus
    durable_status: RunStatus
    completed_stage_ids: tuple[str, ...]
    failed_stage_id: str | None
    exit_code: ExitCode
    resumed: bool
    checkpoint_revision: int


_RUN_TRANSITIONS = frozenset(
    {
        (RunStatus.READY, RunStatus.RUNNING),
        (RunStatus.RUNNING, RunStatus.INTERRUPTED),
        (RunStatus.RUNNING, RunStatus.FAILED),
        (RunStatus.RUNNING, RunStatus.SUCCEEDED),
        (RunStatus.INTERRUPTED, RunStatus.RUNNING),
    }
)

_STAGE_TRANSITIONS = frozenset(
    {
        (StageStatus.PENDING, StageStatus.RUNNING),
        (StageStatus.RUNNING, StageStatus.SUCCEEDED),
        (StageStatus.RUNNING, StageStatus.FAILED),
        (StageStatus.RUNNING, StageStatus.INTERRUPTED),
        (StageStatus.INTERRUPTED, StageStatus.RUNNING),
    }
)


def initial_stage_state(stage_id: str) -> StageState:
    return StageState(
        stage_id=stage_id,
        status=StageStatus.PENDING,
        attempts=0,
        exit_code=None,
        output=None,
    )


def transition_run_status(
    current: RunStatus,
    target: RunStatus,
) -> RunStatus:
    if (current, target) not in _RUN_TRANSITIONS:
        raise StateTransitionError(
            f"invalid run transition: {current.value} -> {target.value}"
        )
    return target


def transition_stage_state(
    state: StageState,
    target: StageStatus,
) -> StageState:
    if (state.status, target) not in _STAGE_TRANSITIONS:
        raise StateTransitionError(
            f"invalid stage transition for '{state.stage_id}': "
            f"{state.status.value} -> {target.value}"
        )
    attempts = state.attempts + int(target is StageStatus.RUNNING)
    return replace(state, status=target, attempts=attempts)
