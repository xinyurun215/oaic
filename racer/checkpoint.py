from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal

from .errors import CheckpointCommitError, CheckpointFingerprintMismatchError, CheckpointFormatError, CheckpointNotFoundError, RevisionConflictError, validate_normalized_error_fields
from .job import InterfaceSpec, JobSpec, RequirementSpec, TestSpec, job_fingerprint, snapshot_job_spec
from .jsonio import (
    CHECKPOINT_MAX_DEPTH,
    STAGE_OUTPUT_MAX_DEPTH,
    JsonValue,
    artifact_json_bytes,
    snapshot_json_value,
    strict_json_loads,
)
from .state import RunStatus, StageState, StageStatus
from .traceability import StageTraceLink, TraceEvent, TraceEventKind, TraceabilitySnapshot, build_traceability_snapshot

_CHECKPOINT_FIELDS = {"schema_version", "revision", "generation", "run_id", "job_id", "job_fingerprint", "run_status", "stages", "traceability", "events"}
_STAGE_FIELDS = {"stage_id", "status", "attempts", "exit_code", "output"}
_SNAPSHOT_FIELDS = {"schema_version", "requirements", "interfaces", "tests", "stage_links"}
_REQUIREMENT_FIELDS = {"requirement_id", "text"}
_INTERFACE_FIELDS = {"interface_id", "name", "requirement_ids"}
_TEST_FIELDS = {"test_id", "name", "requirement_ids", "interface_ids"}
_LINK_FIELDS = {"stage_id", "requirement_ids", "interface_ids", "test_ids"}
_EVENT_FIELDS = {"event_id", "generation", "sequence", "kind", "recorded_at", "run_status", "stage_id", "stage_status", "attempt", "exit_code", "error_type", "error_message", "previous_generation", "previous_run_status"}


@dataclass(frozen=True)
class Checkpoint:
    schema_version: Literal[1]
    revision: int
    generation: int
    run_id: str
    job_id: str
    job_fingerprint: str
    run_status: RunStatus
    stages: tuple[StageState, ...]
    traceability: TraceabilitySnapshot
    events: tuple[TraceEvent, ...]


class JsonCheckpointStore:
    def __init__(self, state_dir: str | Path, job: JobSpec) -> None:
        self._state_dir = Path(state_dir).resolve()
        self._job = snapshot_job_spec(job)

    def exists(self) -> bool:
        return self._path().is_file()

    def load(self) -> Checkpoint:
        path = self._path()
        if not path.is_file():
            raise CheckpointNotFoundError("checkpoint does not exist")
        try:
            checkpoint = _checkpoint_from_value(strict_json_loads(path.read_bytes()))
            _validate_checkpoint(checkpoint, self._job)
            return checkpoint
        except (CheckpointFormatError, CheckpointFingerprintMismatchError):
            raise
        except (OSError, UnicodeError, ValueError, KeyError, TypeError, RecursionError) as exc:
            raise CheckpointFormatError(f"invalid checkpoint: {exc}") from exc

    def commit(self, checkpoint: Checkpoint, *, expected_revision: int | None) -> None:
        _validate_checkpoint(checkpoint, self._job)
        try:
            value = snapshot_json_value(
                _checkpoint_to_value(checkpoint), max_depth=CHECKPOINT_MAX_DEPTH
            )
            data = artifact_json_bytes(value)
            reconstructed = _checkpoint_from_value(strict_json_loads(data))
            _validate_checkpoint(reconstructed, self._job)
            if reconstructed != checkpoint:
                raise CheckpointFormatError("checkpoint does not round-trip exactly")
        except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
            raise CheckpointFormatError(f"invalid checkpoint: {exc}") from exc
        path = self._path()
        if expected_revision is None:
            if path.is_file():
                self.load()
                raise RevisionConflictError("initial checkpoint revision conflict")
            if path.exists() or checkpoint.revision != 0:
                raise RevisionConflictError("initial checkpoint revision conflict")
        else:
            if type(expected_revision) is not int or expected_revision < 0:
                raise RevisionConflictError("invalid expected revision")
            if not path.is_file():
                raise RevisionConflictError("checkpoint does not exist for revision update")
            current = self.load()
            if current.revision != expected_revision or checkpoint.revision != expected_revision + 1:
                raise RevisionConflictError("checkpoint revision conflict")
        temporary_path: Path | None = None
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(mode="wb", dir=self._state_dir, delete=False) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(data)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, path)
        except OSError as exc:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise CheckpointCommitError("checkpoint commit failed") from exc

    def _path(self) -> Path:
        return self._state_dir / "checkpoint.json"


def _checkpoint_to_value(checkpoint: Checkpoint) -> dict[str, JsonValue]:
    return {"schema_version": checkpoint.schema_version, "revision": checkpoint.revision, "generation": checkpoint.generation, "run_id": checkpoint.run_id, "job_id": checkpoint.job_id, "job_fingerprint": checkpoint.job_fingerprint, "run_status": checkpoint.run_status.value, "stages": [_stage_to_value(item) for item in checkpoint.stages], "traceability": _snapshot_to_value(checkpoint.traceability), "events": [_event_to_value(item) for item in checkpoint.events]}


def _stage_to_value(state: StageState) -> dict[str, JsonValue]:
    return {"stage_id": state.stage_id, "status": state.status.value, "attempts": state.attempts, "exit_code": state.exit_code, "output": state.output}


def _snapshot_to_value(snapshot: TraceabilitySnapshot) -> dict[str, JsonValue]:
    return {"schema_version": snapshot.schema_version, "requirements": [{"requirement_id": item.requirement_id, "text": item.text} for item in snapshot.requirements], "interfaces": [{"interface_id": item.interface_id, "name": item.name, "requirement_ids": list(item.requirement_ids)} for item in snapshot.interfaces], "tests": [{"test_id": item.test_id, "name": item.name, "requirement_ids": list(item.requirement_ids), "interface_ids": list(item.interface_ids)} for item in snapshot.tests], "stage_links": [{"stage_id": item.stage_id, "requirement_ids": list(item.requirement_ids), "interface_ids": list(item.interface_ids), "test_ids": list(item.test_ids)} for item in snapshot.stage_links]}


def _event_to_value(event: TraceEvent) -> dict[str, JsonValue]:
    return {"event_id": event.event_id, "generation": event.generation, "sequence": event.sequence, "kind": event.kind.value, "recorded_at": event.recorded_at, "run_status": event.run_status.value, "stage_id": event.stage_id, "stage_status": event.stage_status.value if event.stage_status is not None else None, "attempt": event.attempt, "exit_code": event.exit_code, "error_type": event.error_type, "error_message": event.error_message, "previous_generation": event.previous_generation, "previous_run_status": event.previous_run_status.value if event.previous_run_status is not None else None}


def _checkpoint_from_value(value: JsonValue) -> Checkpoint:
    value = snapshot_json_value(value, max_depth=CHECKPOINT_MAX_DEPTH)
    root = _object(value, "checkpoint", _CHECKPOINT_FIELDS)
    return Checkpoint(_one(root["schema_version"], "schema_version"), _integer(root["revision"], "revision"), _integer(root["generation"], "generation"), _string(root["run_id"], "run_id"), _string(root["job_id"], "job_id"), _string(root["job_fingerprint"], "job_fingerprint"), _run_status(root["run_status"], "run_status"), tuple(_stage_from_value(item, index) for index, item in enumerate(_array(root["stages"], "stages"))), _snapshot_from_value(root["traceability"]), tuple(_event_from_value(item, index) for index, item in enumerate(_array(root["events"], "events"))))


def _stage_from_value(value: JsonValue, index: int) -> StageState:
    item = _object(value, f"stages[{index}]", _STAGE_FIELDS)
    output = item["output"]
    snapshot_json_value(output, max_depth=STAGE_OUTPUT_MAX_DEPTH)
    return StageState(_string(item["stage_id"], f"stages[{index}].stage_id"), _stage_status(item["status"], f"stages[{index}].status"), _integer(item["attempts"], f"stages[{index}].attempts"), _nullable_integer(item["exit_code"], f"stages[{index}].exit_code"), output)


def _snapshot_from_value(value: JsonValue) -> TraceabilitySnapshot:
    item = _object(value, "traceability", _SNAPSHOT_FIELDS)
    return TraceabilitySnapshot(_one(item["schema_version"], "traceability.schema_version"), tuple(_requirement_from_value(entry, index) for index, entry in enumerate(_array(item["requirements"], "traceability.requirements"))), tuple(_interface_from_value(entry, index) for index, entry in enumerate(_array(item["interfaces"], "traceability.interfaces"))), tuple(_test_from_value(entry, index) for index, entry in enumerate(_array(item["tests"], "traceability.tests"))), tuple(_link_from_value(entry, index) for index, entry in enumerate(_array(item["stage_links"], "traceability.stage_links"))))


def _requirement_from_value(value: JsonValue, index: int) -> RequirementSpec:
    item = _object(value, f"traceability.requirements[{index}]", _REQUIREMENT_FIELDS)
    return RequirementSpec(_string(item["requirement_id"], "requirement_id"), _string(item["text"], "text"))


def _interface_from_value(value: JsonValue, index: int) -> InterfaceSpec:
    item = _object(value, f"traceability.interfaces[{index}]", _INTERFACE_FIELDS)
    return InterfaceSpec(_string(item["interface_id"], "interface_id"), _string(item["name"], "name"), _strings(item["requirement_ids"], "requirement_ids"))


def _test_from_value(value: JsonValue, index: int) -> TestSpec:
    item = _object(value, f"traceability.tests[{index}]", _TEST_FIELDS)
    return TestSpec(_string(item["test_id"], "test_id"), _string(item["name"], "name"), _strings(item["requirement_ids"], "requirement_ids"), _strings(item["interface_ids"], "interface_ids"))


def _link_from_value(value: JsonValue, index: int) -> StageTraceLink:
    item = _object(value, f"traceability.stage_links[{index}]", _LINK_FIELDS)
    return StageTraceLink(_string(item["stage_id"], "stage_id"), _strings(item["requirement_ids"], "requirement_ids"), _strings(item["interface_ids"], "interface_ids"), _strings(item["test_ids"], "test_ids"))


def _event_from_value(value: JsonValue, index: int) -> TraceEvent:
    item = _object(value, f"events[{index}]", _EVENT_FIELDS)
    return TraceEvent(_string(item["event_id"], "event_id"), _integer(item["generation"], "event.generation"), _integer(item["sequence"], "event.sequence"), _event_kind(item["kind"], "event.kind"), _recorded_at(item["recorded_at"]), _run_status(item["run_status"], "event.run_status"), _nullable_string(item["stage_id"], "event.stage_id"), _nullable_stage_status(item["stage_status"], "event.stage_status"), _nullable_integer(item["attempt"], "event.attempt"), _nullable_integer(item["exit_code"], "event.exit_code"), _nullable_string(item["error_type"], "event.error_type"), _nullable_string(item["error_message"], "event.error_message"), _nullable_integer(item["previous_generation"], "event.previous_generation"), _nullable_run_status(item["previous_run_status"], "event.previous_run_status"))


def _validate_checkpoint(checkpoint: Checkpoint, job: JobSpec) -> None:
    if not isinstance(checkpoint, Checkpoint):
        raise CheckpointFormatError("checkpoint must be a Checkpoint")
    if checkpoint.schema_version != 1 or type(checkpoint.schema_version) is not int:
        raise CheckpointFormatError("schema_version must be the integer 1")
    _integer(checkpoint.revision, "revision")
    _integer(checkpoint.generation, "generation")
    if type(checkpoint.run_id) is not str or type(checkpoint.job_id) is not str or type(checkpoint.job_fingerprint) is not str:
        raise CheckpointFormatError("checkpoint identifiers must be strings")
    if not isinstance(checkpoint.run_status, RunStatus):
        raise CheckpointFormatError("run_status is invalid")
    if type(checkpoint.stages) is not tuple or type(checkpoint.events) is not tuple:
        raise CheckpointFormatError("stages and events must be tuples")
    if checkpoint.job_id != job.job_id or checkpoint.job_fingerprint != job_fingerprint(job):
        raise CheckpointFingerprintMismatchError("checkpoint does not match job fingerprint")
    if checkpoint.run_id != f"{job.job_id}:{checkpoint.generation}":
        raise CheckpointFormatError("checkpoint run_id does not match generation")
    _validate_traceability_snapshot(checkpoint.traceability, job)
    for state in checkpoint.stages:
        if not isinstance(state, StageState) or not isinstance(state.status, StageStatus):
            raise CheckpointFormatError("checkpoint stage state is invalid")
        _integer(state.attempts, f"stage '{state.stage_id}' attempts")
        if state.exit_code is not None:
            _integer(state.exit_code, f"stage '{state.stage_id}' exit_code")
        try:
            snapshot_json_value(state.output, max_depth=STAGE_OUTPUT_MAX_DEPTH)
        except (ValueError, RecursionError) as exc:
            raise CheckpointFormatError(f"invalid stage output: {exc}") from exc
    expected_ids = tuple(stage.stage_id for stage in job.stages)
    if tuple(stage.stage_id for stage in checkpoint.stages) != expected_ids:
        raise CheckpointFormatError("checkpoint stages do not match job order")
    _validate_state_shape(checkpoint.run_status, checkpoint.stages)
    _validate_events(checkpoint, job)


def _validate_traceability_snapshot(
    snapshot: object,
    job: JobSpec,
) -> None:
    if type(snapshot) is not TraceabilitySnapshot:
        raise CheckpointFormatError("checkpoint traceability is invalid")
    if type(snapshot.schema_version) is not int or snapshot.schema_version != 1:
        raise CheckpointFormatError("traceability schema_version must be the integer 1")
    if (
        type(snapshot.requirements) is not tuple
        or type(snapshot.interfaces) is not tuple
        or type(snapshot.tests) is not tuple
        or type(snapshot.stage_links) is not tuple
    ):
        raise CheckpointFormatError("checkpoint traceability collections must be tuples")
    for item in snapshot.requirements:
        if (
            type(item) is not RequirementSpec
            or type(item.requirement_id) is not str
            or type(item.text) is not str
        ):
            raise CheckpointFormatError("checkpoint traceability requirement is invalid")
    for item in snapshot.interfaces:
        if (
            type(item) is not InterfaceSpec
            or type(item.interface_id) is not str
            or type(item.name) is not str
            or type(item.requirement_ids) is not tuple
            or any(type(value) is not str for value in item.requirement_ids)
        ):
            raise CheckpointFormatError("checkpoint traceability interface is invalid")
    for item in snapshot.tests:
        if (
            type(item) is not TestSpec
            or type(item.test_id) is not str
            or type(item.name) is not str
            or type(item.requirement_ids) is not tuple
            or type(item.interface_ids) is not tuple
            or any(type(value) is not str for value in item.requirement_ids)
            or any(type(value) is not str for value in item.interface_ids)
        ):
            raise CheckpointFormatError("checkpoint traceability test is invalid")
    for item in snapshot.stage_links:
        if (
            type(item) is not StageTraceLink
            or type(item.stage_id) is not str
            or type(item.requirement_ids) is not tuple
            or type(item.interface_ids) is not tuple
            or type(item.test_ids) is not tuple
            or any(type(value) is not str for value in item.requirement_ids)
            or any(type(value) is not str for value in item.interface_ids)
            or any(type(value) is not str for value in item.test_ids)
        ):
            raise CheckpointFormatError("checkpoint traceability stage link is invalid")
    if snapshot != build_traceability_snapshot(job):
        raise CheckpointFormatError("checkpoint traceability does not match job")


def _validate_state_shape(status: RunStatus, stages: tuple[StageState, ...]) -> None:
    statuses = tuple(stage.status for stage in stages)
    if status is RunStatus.READY and statuses == (StageStatus.PENDING,) * len(stages): return
    if status is RunStatus.SUCCEEDED and statuses == (StageStatus.SUCCEEDED,) * len(stages): return
    if status is RunStatus.RUNNING and (statuses == (StageStatus.SUCCEEDED,) * len(stages) or _prefix_suffix(statuses, StageStatus.SUCCEEDED, StageStatus.PENDING) or _single_active(statuses, StageStatus.RUNNING)): return
    if status is RunStatus.INTERRUPTED and _single_active(statuses, StageStatus.INTERRUPTED): return
    if status is RunStatus.FAILED and _single_active(statuses, StageStatus.FAILED): return
    raise CheckpointFormatError("checkpoint has an illegal run and stage state combination")


def _prefix_suffix(statuses: tuple[StageStatus, ...], prefix: StageStatus, suffix: StageStatus) -> bool:
    split = 0
    while split < len(statuses) and statuses[split] is prefix: split += 1
    return all(value is suffix for value in statuses[split:])


def _single_active(statuses: tuple[StageStatus, ...], active: StageStatus) -> bool:
    try: index = statuses.index(active)
    except ValueError: return False
    return statuses.count(active) == 1 and all(value is StageStatus.SUCCEEDED for value in statuses[:index]) and all(value is StageStatus.PENDING for value in statuses[index + 1:])


def _validate_events(checkpoint: Checkpoint, job: JobSpec) -> None:
    if not checkpoint.events:
        raise CheckpointFormatError("checkpoint events must not be empty")
    stage_order = tuple(stage.stage_id for stage in job.stages)
    stage_ids = set(stage_order)
    current_generation = -1
    expected_sequence = 0
    replay_status: RunStatus | None = None
    replay_states: dict[str, tuple[StageStatus, int, int | None]] = {}
    for event in checkpoint.events:
        if not isinstance(event, TraceEvent):
            raise CheckpointFormatError("checkpoint event is invalid")
        _integer(event.generation, "event generation")
        _integer(event.sequence, "event sequence")
        if event.generation == current_generation:
            if event.sequence != expected_sequence:
                raise CheckpointFormatError("event sequences must be continuous")
        elif event.generation == current_generation + 1:
            if event.sequence != 0:
                raise CheckpointFormatError("generation must start at sequence zero")
            current_generation = event.generation
        else:
            raise CheckpointFormatError("event generations must be continuous from zero")
        expected_sequence = event.sequence + 1
        if event.event_id != f"{job.job_id}:{event.generation}:{event.sequence:06d}":
            raise CheckpointFormatError("event_id does not match job and sequence")
        _validate_event_fields(event, stage_ids)
        if event.generation == 0 and event.sequence == 0:
            if event.kind is not TraceEventKind.CHECKPOINT_CREATED:
                raise CheckpointFormatError("generation zero must start with checkpoint_created")
            replay_status = _replay_checkpoint_created(event, stage_order)
            replay_states = _pending_replay_states(stage_order)
            continue
        if event.sequence == 0:
            if event.kind is not TraceEventKind.RESET:
                raise CheckpointFormatError("new generations must start with reset")
            if replay_status is RunStatus.READY:
                raise CheckpointFormatError("READY generation cannot have a reset event")
            if event.previous_generation != event.generation - 1 or event.previous_run_status is not replay_status:
                raise CheckpointFormatError("reset previous state does not match history")
            _require_no_exit_or_error(event)
            replay_status = RunStatus.READY
            replay_states = _pending_replay_states(stage_order)
            continue
        if event.kind is TraceEventKind.RESET:
            raise CheckpointFormatError("reset must start a new generation")
        if replay_status is None:
            raise CheckpointFormatError("event history has no initial checkpoint")
        replay_status = _replay_event(
            event,
            replay_status,
            replay_states,
            stage_order,
        )
    _validate_atomic_event_pairs(checkpoint.events)
    if current_generation != checkpoint.generation:
        raise CheckpointFormatError("checkpoint generation does not match event history")
    if replay_status is not checkpoint.run_status:
        raise CheckpointFormatError("checkpoint run status does not match event history")
    for state in checkpoint.stages:
        replayed = replay_states[state.stage_id]
        if (state.status, state.attempts, state.exit_code) != replayed:
            raise CheckpointFormatError("checkpoint stages do not match event history")
    if checkpoint.revision != _expected_checkpoint_revision(checkpoint.events):
        raise CheckpointFormatError("checkpoint revision does not match event history")


def _expected_checkpoint_revision(events: tuple[TraceEvent, ...]) -> int:
    revision = 0
    for index, event in enumerate(events[1:], start=1):
        previous = events[index - 1]
        if event.kind in (TraceEventKind.RUN_FAILED, TraceEventKind.RUN_INTERRUPTED):
            continue
        if (
            event.kind is TraceEventKind.STAGE_STARTED
            and previous.kind is TraceEventKind.RUN_RESUMED
        ):
            continue
        revision += 1
    return revision


def _pending_replay_states(
    stage_order: tuple[str, ...],
) -> dict[str, tuple[StageStatus, int, int | None]]:
    return {
        stage_id: (StageStatus.PENDING, 0, None)
        for stage_id in stage_order
    }


def _replay_checkpoint_created(
    event: TraceEvent,
    stage_order: tuple[str, ...],
) -> RunStatus:
    if (
        event.run_status is not RunStatus.READY
        or event.exit_code is not None
        or event.error_type is not None
        or event.error_message is not None
    ):
        raise CheckpointFormatError("checkpoint_created fields are invalid")
    return RunStatus.READY


def _replay_event(
    event: TraceEvent,
    run_status: RunStatus,
    states: dict[str, tuple[StageStatus, int, int | None]],
    stage_order: tuple[str, ...],
) -> RunStatus:
    if event.kind is TraceEventKind.RUN_STARTED:
        _require_event(event, run_status is RunStatus.READY and event.run_status is RunStatus.RUNNING)
        _require_no_exit_or_error(event)
        return RunStatus.RUNNING
    if event.kind is TraceEventKind.RUN_RESUMED:
        _require_event(event, run_status is RunStatus.INTERRUPTED and event.run_status is RunStatus.RUNNING)
        _require_no_exit_or_error(event)
        return RunStatus.RUNNING
    if event.kind is TraceEventKind.STAGE_STARTED:
        _require_event(event, run_status is RunStatus.RUNNING and event.run_status is RunStatus.RUNNING)
        stage_id = _event_stage_id(event)
        status, attempts, _ = states[stage_id]
        _require_event(event, status in (StageStatus.PENDING, StageStatus.INTERRUPTED))
        _require_event(event, _stage_position_is_active(states, stage_order, stage_id))
        _require_event(event, event.stage_status is StageStatus.RUNNING and event.attempt == attempts + 1)
        _require_no_exit_or_error(event)
        states[stage_id] = (StageStatus.RUNNING, event.attempt, None)
        return RunStatus.RUNNING
    if event.kind is TraceEventKind.STAGE_SUCCEEDED:
        _require_event(event, run_status is RunStatus.RUNNING and event.run_status is RunStatus.RUNNING)
        stage_id = _event_stage_id(event)
        status, attempts, _ = states[stage_id]
        _require_event(event, status is StageStatus.RUNNING and event.stage_status is StageStatus.SUCCEEDED and event.attempt == attempts)
        _require_event(event, event.exit_code == 0)
        _require_no_error(event)
        states[stage_id] = (StageStatus.SUCCEEDED, attempts, 0)
        return RunStatus.RUNNING
    if event.kind is TraceEventKind.STAGE_FAILED:
        _require_event(event, run_status is RunStatus.RUNNING and event.run_status is RunStatus.FAILED)
        stage_id = _event_stage_id(event)
        status, attempts, _ = states[stage_id]
        _require_event(event, status is StageStatus.RUNNING and event.stage_status is StageStatus.FAILED and event.attempt == attempts)
        _require_event(event, event.exit_code is not None and event.exit_code > 0)
        _require_failure_error(event)
        states[stage_id] = (StageStatus.FAILED, attempts, event.exit_code)
        return RunStatus.FAILED
    if event.kind is TraceEventKind.STAGE_INTERRUPTED:
        _require_event(event, run_status is RunStatus.RUNNING and event.run_status is RunStatus.INTERRUPTED)
        stage_id = _event_stage_id(event)
        status, attempts, _ = states[stage_id]
        _require_event(event, status is StageStatus.RUNNING and event.stage_status is StageStatus.INTERRUPTED and event.attempt == attempts)
        _require_no_exit_or_error(event)
        states[stage_id] = (StageStatus.INTERRUPTED, attempts, None)
        return RunStatus.INTERRUPTED
    if event.kind is TraceEventKind.RUN_INTERRUPTED:
        _require_event(event, run_status is RunStatus.INTERRUPTED and event.run_status is RunStatus.INTERRUPTED)
        _require_no_exit_or_error(event)
        return RunStatus.INTERRUPTED
    if event.kind is TraceEventKind.RUN_SUCCEEDED:
        _require_event(event, run_status is RunStatus.RUNNING and event.run_status is RunStatus.SUCCEEDED)
        _require_event(event, all(state[0] is StageStatus.SUCCEEDED for state in states.values()))
        _require_event(event, event.exit_code == 0)
        _require_no_error(event)
        return RunStatus.SUCCEEDED
    if event.kind is TraceEventKind.RUN_FAILED:
        _require_event(event, run_status is RunStatus.FAILED and event.run_status is RunStatus.FAILED)
        _require_event(event, event.exit_code in (4, 70))
        _require_failure_error(event)
        return RunStatus.FAILED
    raise CheckpointFormatError("event kind is not valid at this sequence")


def _event_stage_id(event: TraceEvent) -> str:
    if type(event.stage_id) is not str:
        raise CheckpointFormatError("stage event identifier is invalid")
    return event.stage_id


def _stage_position_is_active(
    states: dict[str, tuple[StageStatus, int, int | None]],
    stage_order: tuple[str, ...],
    stage_id: str,
) -> bool:
    index = stage_order.index(stage_id)
    return (
        all(states[item][0] is StageStatus.SUCCEEDED for item in stage_order[:index])
        and all(states[item][0] is StageStatus.PENDING for item in stage_order[index + 1:])
    )


def _require_event(event: TraceEvent, condition: bool) -> None:
    if not condition:
        raise CheckpointFormatError(f"{event.kind.value} fields are invalid")


def _require_no_error(event: TraceEvent) -> None:
    _require_event(event, event.error_type is None and event.error_message is None)


def _require_no_exit_or_error(event: TraceEvent) -> None:
    _require_event(event, event.exit_code is None)
    _require_no_error(event)


def _require_failure_error(event: TraceEvent) -> None:
    _require_event(
        event,
        type(event.error_type) is str and type(event.error_message) is str,
    )


def _validate_atomic_event_pairs(events: tuple[TraceEvent, ...]) -> None:
    for index, event in enumerate(events):
        previous = events[index - 1] if index else None
        following = events[index + 1] if index + 1 < len(events) else None
        if event.kind is TraceEventKind.STAGE_FAILED:
            _validate_failure_pair(event, following)
        elif event.kind is TraceEventKind.RUN_FAILED:
            if previous is None or previous.kind is not TraceEventKind.STAGE_FAILED:
                raise CheckpointFormatError("run_failed must follow stage_failed")
        elif event.kind is TraceEventKind.STAGE_INTERRUPTED:
            _validate_adjacent_pair(event, following, TraceEventKind.RUN_INTERRUPTED)
        elif event.kind is TraceEventKind.RUN_INTERRUPTED:
            if previous is None or previous.kind is not TraceEventKind.STAGE_INTERRUPTED:
                raise CheckpointFormatError("run_interrupted must follow stage_interrupted")
        elif event.kind is TraceEventKind.RUN_RESUMED:
            _validate_adjacent_pair(event, following, TraceEventKind.STAGE_STARTED)


def _validate_adjacent_pair(
    first: TraceEvent,
    second: TraceEvent | None,
    expected_kind: TraceEventKind,
) -> None:
    if (
        second is None
        or second.generation != first.generation
        or second.sequence != first.sequence + 1
        or second.kind is not expected_kind
        or second.recorded_at != first.recorded_at
    ):
        raise CheckpointFormatError(
            f"{first.kind.value} requires adjacent {expected_kind.value}"
        )


def _validate_failure_pair(
    stage_event: TraceEvent,
    run_event: TraceEvent | None,
) -> None:
    _validate_adjacent_pair(stage_event, run_event, TraceEventKind.RUN_FAILED)
    assert run_event is not None
    if (
        run_event.error_type != stage_event.error_type
        or run_event.error_message != stage_event.error_message
    ):
        raise CheckpointFormatError("failure event errors must match")
    stage_id = _event_stage_id(stage_event)
    if run_event.exit_code == 4:
        expected_message = (
            f"stage '{stage_id}' failed with executor exit code "
            f"{stage_event.exit_code}"
        )
        if (
            stage_event.error_type != "StageFailedError"
            or stage_event.exit_code is None
            or not 1 <= stage_event.exit_code <= 255
            or stage_event.error_message != expected_message
        ):
            raise CheckpointFormatError("explicit stage failure mapping is invalid")
    elif (
        run_event.exit_code != 70
        or stage_event.exit_code != 70
        or type(stage_event.error_type) is not str
        or type(stage_event.error_message) is not str
    ):
        raise CheckpointFormatError("internal stage failure mapping is invalid")


def _validate_event_fields(event: TraceEvent, stage_ids: set[str]) -> None:
    if not isinstance(event.kind, TraceEventKind) or not isinstance(event.run_status, RunStatus):
        raise CheckpointFormatError("event kind or run status is invalid")
    _recorded_at(event.recorded_at)
    if event.exit_code is not None:
        _integer(event.exit_code, "event exit_code")
    if event.error_type is not None or event.error_message is not None:
        if type(event.error_type) is not str or type(event.error_message) is not str:
            raise CheckpointFormatError("event error fields are invalid")
        try:
            validate_normalized_error_fields(event.error_type, event.error_message)
        except ValueError as exc:
            raise CheckpointFormatError(f"event error fields are invalid: {exc}") from exc
    if event.previous_generation is not None:
        _integer(event.previous_generation, "event previous_generation")
    if event.previous_run_status is not None and not isinstance(event.previous_run_status, RunStatus):
        raise CheckpointFormatError("event previous_run_status is invalid")
    stage_event = event.kind in {TraceEventKind.STAGE_STARTED, TraceEventKind.STAGE_SUCCEEDED, TraceEventKind.STAGE_FAILED, TraceEventKind.STAGE_INTERRUPTED}
    if stage_event:
        if type(event.stage_id) is not str or event.stage_id not in stage_ids or not isinstance(event.stage_status, StageStatus) or event.attempt is None: raise CheckpointFormatError("stage event fields are incomplete")
        _integer(event.attempt, "event attempt")
    elif event.stage_id is not None or event.stage_status is not None or event.attempt is not None: raise CheckpointFormatError("non-stage event fields must be null")
    if event.kind is TraceEventKind.STAGE_SUCCEEDED and event.exit_code != 0: raise CheckpointFormatError("stage_succeeded exit_code must be zero")
    if event.kind is TraceEventKind.RUN_SUCCEEDED and event.exit_code != 0: raise CheckpointFormatError("run_succeeded exit_code must be zero")
    if event.kind not in {TraceEventKind.STAGE_FAILED, TraceEventKind.RUN_FAILED} and (event.error_type is not None or event.error_message is not None): raise CheckpointFormatError("non-failure event errors must be null")
    if event.kind is TraceEventKind.RESET:
        if event.previous_generation != event.generation - 1 or event.previous_run_status is None or event.run_status is not RunStatus.READY: raise CheckpointFormatError("reset previous fields are invalid")
    elif event.previous_generation is not None or event.previous_run_status is not None: raise CheckpointFormatError("previous fields are only valid for reset")


def _object(value: JsonValue, context: str, required: set[str]) -> dict[str, JsonValue]:
    if type(value) is not dict: raise CheckpointFormatError(f"{context} must be an object")
    missing, unknown = sorted(required - set(value)), sorted(set(value) - required)
    if missing or unknown: raise CheckpointFormatError(f"{context} has {'missing ' + ', '.join(missing) if missing else 'unknown ' + ', '.join(unknown)} fields")
    return value


def _array(value: JsonValue, context: str) -> list[JsonValue]:
    if type(value) is not list: raise CheckpointFormatError(f"{context} must be an array")
    return value


def _string(value: JsonValue, context: str) -> str:
    if type(value) is not str: raise CheckpointFormatError(f"{context} must be a string")
    return value


def _nullable_string(value: JsonValue, context: str) -> str | None: return None if value is None else _string(value, context)
def _integer(value: object, context: str) -> int:
    if type(value) is not int or value < 0: raise CheckpointFormatError(f"{context} must be a non-negative integer")
    return value
def _nullable_integer(value: JsonValue, context: str) -> int | None: return None if value is None else _integer(value, context)
def _one(value: JsonValue, context: str) -> Literal[1]:
    if type(value) is not int or value != 1: raise CheckpointFormatError(f"{context} must be the integer 1")
    return 1
def _strings(value: JsonValue, context: str) -> tuple[str, ...]: return tuple(_string(item, context) for item in _array(value, context))
def _run_status(value: JsonValue, context: str) -> RunStatus:
    try: return RunStatus(_string(value, context))
    except ValueError as exc: raise CheckpointFormatError(f"{context} is invalid") from exc
def _nullable_run_status(value: JsonValue, context: str) -> RunStatus | None: return None if value is None else _run_status(value, context)
def _stage_status(value: JsonValue, context: str) -> StageStatus:
    try: return StageStatus(_string(value, context))
    except ValueError as exc: raise CheckpointFormatError(f"{context} is invalid") from exc
def _nullable_stage_status(value: JsonValue, context: str) -> StageStatus | None: return None if value is None else _stage_status(value, context)
def _event_kind(value: JsonValue, context: str) -> TraceEventKind:
    try: return TraceEventKind(_string(value, context))
    except ValueError as exc: raise CheckpointFormatError(f"{context} is invalid") from exc
def _recorded_at(value: JsonValue) -> str:
    text = _string(value, "event.recorded_at")
    try:
        if datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").strftime("%Y-%m-%dT%H:%M:%S.%fZ") != text: raise ValueError
    except ValueError as exc: raise CheckpointFormatError("event.recorded_at is invalid") from exc
    return text
