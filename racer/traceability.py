from dataclasses import dataclass
from enum import StrEnum, unique
from typing import Literal

from .job import (
    InterfaceSpec,
    JobSpec,
    RequirementSpec,
    TestSpec,
)
from .state import RunStatus, StageState, StageStatus


@unique
class TraceEventKind(StrEnum):
    CHECKPOINT_CREATED = "checkpoint_created"
    RUN_STARTED = "run_started"
    RUN_RESUMED = "run_resumed"
    STAGE_STARTED = "stage_started"
    STAGE_SUCCEEDED = "stage_succeeded"
    STAGE_FAILED = "stage_failed"
    STAGE_INTERRUPTED = "stage_interrupted"
    RUN_INTERRUPTED = "run_interrupted"
    RUN_SUCCEEDED = "run_succeeded"
    RUN_FAILED = "run_failed"
    RESET = "reset"
@dataclass(frozen=True)
class StageTraceLink:
    stage_id: str
    requirement_ids: tuple[str, ...]
    interface_ids: tuple[str, ...]
    test_ids: tuple[str, ...]


@dataclass(frozen=True)
class TraceEvent:
    event_id: str
    generation: int
    sequence: int
    kind: TraceEventKind
    recorded_at: str
    run_status: RunStatus
    stage_id: str | None
    stage_status: StageStatus | None
    attempt: int | None
    exit_code: int | None
    error_type: str | None
    error_message: str | None
    previous_generation: int | None
    previous_run_status: RunStatus | None


@dataclass(frozen=True)
class TraceabilitySnapshot:
    schema_version: Literal[1]
    requirements: tuple[RequirementSpec, ...]
    interfaces: tuple[InterfaceSpec, ...]
    tests: tuple[TestSpec, ...]
    stage_links: tuple[StageTraceLink, ...]


@dataclass(frozen=True)
class TraceDocument:
    schema_version: Literal[1]
    job_id: str
    job_fingerprint: str
    current_generation: int
    current_run_id: str
    current_revision: int
    current_run_status: RunStatus
    stage_states: tuple[StageState, ...]
    traceability: TraceabilitySnapshot
    events: tuple[TraceEvent, ...]


def build_traceability_snapshot(job: JobSpec) -> TraceabilitySnapshot:
    return TraceabilitySnapshot(
        schema_version=1,
        requirements=job.requirements,
        interfaces=job.interfaces,
        tests=job.tests,
        stage_links=tuple(
            StageTraceLink(
                stage_id=stage.stage_id,
                requirement_ids=stage.requirement_ids,
                interface_ids=stage.interface_ids,
                test_ids=stage.test_ids,
            )
            for stage in job.stages
        ),
    )
