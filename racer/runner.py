from dataclasses import replace

from .checkpoint import Checkpoint, JsonCheckpointStore
from .clock import Clock, format_recorded_at
from .errors import (
    CheckpointFingerprintMismatchError,
    ExecutorContractError,
    InvalidStateError,
    StageFailedError,
    UnknownInterruptStageError,
    normalize_exception,
)
from .exit_codes import ExitCode
from .job import JobSpec, StageSpec, job_fingerprint, snapshot_job_spec
from .jsonio import (
    STAGE_OUTPUT_MAX_DEPTH,
    fingerprint_json_bytes,
    snapshot_json_value,
    strict_json_loads,
)
from .queue import OrderedStageQueue
from .stages import StageExecutor
from .state import (
    RunResult,
    RunStatus,
    StageContext,
    StageResult,
    StageState,
    StageStatus,
    initial_stage_state,
    transition_run_status,
    transition_stage_state,
)
from .traceability import (
    TraceDocument,
    TraceEvent,
    TraceEventKind,
    build_traceability_snapshot,
)


class Runner:
    def __init__(
        self,
        job: JobSpec,
        store: JsonCheckpointStore,
        executor: StageExecutor,
        clock: Clock,
    ) -> None:
        self._job = snapshot_job_spec(job)
        self._store = store
        self._executor = executor
        self._clock = clock
        self._queue = OrderedStageQueue(self._job.stages)

    def run(self, *, interrupt_before: str | None = None) -> RunResult:
        self._validate_interrupt_before(interrupt_before)
        if self._store.exists():
            checkpoint = self._load_checkpoint_for_job()
            if checkpoint.run_status is not RunStatus.READY:
                raise InvalidStateError(
                    f"run requires READY; found {checkpoint.run_status.value}"
                )
        else:
            checkpoint = self._create_checkpoint()
        return self._execute(
            self._start_run(checkpoint),
            False,
            interrupt_before,
        )

    def resume(self, *, interrupt_before: str | None = None) -> RunResult:
        self._validate_interrupt_before(interrupt_before)
        checkpoint = self._load_checkpoint_for_job()
        if checkpoint.run_status is RunStatus.RUNNING:
            running = [
                state
                for state in checkpoint.stages
                if state.status is StageStatus.RUNNING
            ]
            if running:
                checkpoint = self._interrupt_running_stage(checkpoint, running[0])
                checkpoint, state = self._resume_stage(
                    checkpoint,
                    checkpoint.stages[self._stage_index(running[0].stage_id)],
                )
                return self._execute(
                    checkpoint,
                    True,
                    interrupt_before,
                    started=(self._spec(state.stage_id), state),
                )
            if all(
                state.status is StageStatus.SUCCEEDED
                for state in checkpoint.stages
            ):
                return self._succeed_run(checkpoint, True)
            return self._execute(checkpoint, True, interrupt_before)
        if checkpoint.run_status is RunStatus.INTERRUPTED:
            state = next(
                state
                for state in checkpoint.stages
                if state.status is StageStatus.INTERRUPTED
            )
            checkpoint, state = self._resume_stage(checkpoint, state)
            return self._execute(
                checkpoint,
                True,
                interrupt_before,
                started=(self._spec(state.stage_id), state),
            )
        raise InvalidStateError(
            "resume requires RUNNING or INTERRUPTED; found "
            f"{checkpoint.run_status.value}"
        )

    def reset(self) -> RunResult:
        checkpoint = self._load_checkpoint_for_job()
        if checkpoint.run_status is RunStatus.READY:
            return self._result(checkpoint, ExitCode.OK, None, resumed=False)
        candidate = Checkpoint(
            schema_version=1,
            revision=checkpoint.revision + 1,
            generation=checkpoint.generation + 1,
            run_id=f"{self._job.job_id}:{checkpoint.generation + 1}",
            job_id=self._job.job_id,
            job_fingerprint=job_fingerprint(self._job),
            run_status=RunStatus.READY,
            stages=tuple(
                initial_stage_state(spec.stage_id) for spec in self._job.stages
            ),
            traceability=build_traceability_snapshot(self._job),
            events=checkpoint.events,
        )
        checkpoint = self._commit_events(
            candidate,
            checkpoint.revision,
            (
                (
                    TraceEventKind.RESET,
                    None,
                    None,
                    None,
                    checkpoint.generation,
                    checkpoint.run_status,
                ),
            ),
        )
        return self._result(checkpoint, ExitCode.OK, None, resumed=False)

    def trace(self) -> TraceDocument:
        checkpoint = self._load_checkpoint_for_job()
        return TraceDocument(
            schema_version=checkpoint.schema_version,
            job_id=checkpoint.job_id,
            job_fingerprint=checkpoint.job_fingerprint,
            current_generation=checkpoint.generation,
            current_run_id=checkpoint.run_id,
            current_revision=checkpoint.revision,
            current_run_status=checkpoint.run_status,
            stage_states=checkpoint.stages,
            traceability=checkpoint.traceability,
            events=checkpoint.events,
        )

    def _load_checkpoint_for_job(self) -> Checkpoint:
        checkpoint = self._store.load()
        if (
            checkpoint.job_id != self._job.job_id
            or checkpoint.job_fingerprint != job_fingerprint(self._job)
        ):
            raise CheckpointFingerprintMismatchError(
                "checkpoint does not match job fingerprint"
            )
        return checkpoint

    def _execute(
        self,
        checkpoint: Checkpoint,
        resumed: bool,
        interrupt_before: str | None,
        *,
        started: tuple[StageSpec, StageState] | None = None,
    ) -> RunResult:
        while True:
            if started is None:
                spec = self._queue.next_stage(
                    {state.stage_id: state for state in checkpoint.stages}
                )
                if spec is None:
                    return self._succeed_run(checkpoint, resumed)
                checkpoint, state = self._start_stage(checkpoint, spec)
            else:
                spec, state = started
                started = None
            if spec.stage_id == interrupt_before:
                return self._result(
                    checkpoint,
                    ExitCode.INTERRUPTED,
                    None,
                    resumed=resumed,
                    status=RunStatus.INTERRUPTED,
                    durable_status=RunStatus.RUNNING,
                )
            context = StageContext(
                self._job.job_id,
                checkpoint.run_id,
                checkpoint.generation,
                spec.stage_id,
                state.attempts,
            )
            try:
                result = self._executor.execute(self._detached_spec(spec), context)
                result = self._snapshot_result(spec, result)
            except ExecutorContractError as error:
                return self._fail_internal(checkpoint, state, error, resumed)
            except Exception as error:
                return self._fail_internal(checkpoint, state, error, resumed)
            if result.exit_code == 0:
                checkpoint = self._succeed_stage(checkpoint, state, result)
            else:
                return self._fail_explicit(checkpoint, state, result, resumed)

    def _create_checkpoint(self) -> Checkpoint:
        checkpoint = Checkpoint(
            schema_version=1,
            revision=0,
            generation=0,
            run_id=f"{self._job.job_id}:0",
            job_id=self._job.job_id,
            job_fingerprint=job_fingerprint(self._job),
            run_status=RunStatus.READY,
            stages=tuple(
                initial_stage_state(spec.stage_id) for spec in self._job.stages
            ),
            traceability=build_traceability_snapshot(self._job),
            events=(),
        )
        return self._commit_events(
            checkpoint,
            None,
            (
                (
                    TraceEventKind.CHECKPOINT_CREATED,
                    None,
                    None,
                    None,
                    None,
                    None,
                ),
            ),
        )

    def _start_run(self, checkpoint: Checkpoint) -> Checkpoint:
        candidate = replace(
            checkpoint,
            revision=checkpoint.revision + 1,
            run_status=transition_run_status(checkpoint.run_status, RunStatus.RUNNING),
        )
        return self._commit_events(
            candidate,
            checkpoint.revision,
            ((TraceEventKind.RUN_STARTED, None, None, None, None, None),),
        )

    def _start_stage(
        self,
        checkpoint: Checkpoint,
        spec: StageSpec,
    ) -> tuple[Checkpoint, StageState]:
        index = self._stage_index(spec.stage_id)
        state = transition_stage_state(
            checkpoint.stages[index],
            StageStatus.RUNNING,
        )
        candidate = replace(
            checkpoint,
            revision=checkpoint.revision + 1,
            stages=(
                checkpoint.stages[:index]
                + (state,)
                + checkpoint.stages[index + 1 :]
            ),
        )
        return (
            self._commit_events(
                candidate,
                checkpoint.revision,
                ((TraceEventKind.STAGE_STARTED, state, None, None, None, None),),
            ),
            state,
        )

    def _interrupt_running_stage(
        self,
        checkpoint: Checkpoint,
        state: StageState,
    ) -> Checkpoint:
        index = self._stage_index(state.stage_id)
        interrupted = transition_stage_state(state, StageStatus.INTERRUPTED)
        candidate = replace(
            checkpoint,
            revision=checkpoint.revision + 1,
            run_status=transition_run_status(
                checkpoint.run_status,
                RunStatus.INTERRUPTED,
            ),
            stages=(
                checkpoint.stages[:index]
                + (interrupted,)
                + checkpoint.stages[index + 1 :]
            ),
        )
        return self._commit_events(
            candidate,
            checkpoint.revision,
            (
                (
                    TraceEventKind.STAGE_INTERRUPTED,
                    interrupted,
                    None,
                    None,
                    None,
                    None,
                ),
                (TraceEventKind.RUN_INTERRUPTED, None, None, None, None, None),
            ),
        )

    def _resume_stage(
        self,
        checkpoint: Checkpoint,
        state: StageState,
    ) -> tuple[Checkpoint, StageState]:
        index = self._stage_index(state.stage_id)
        running = transition_stage_state(state, StageStatus.RUNNING)
        candidate = replace(
            checkpoint,
            revision=checkpoint.revision + 1,
            run_status=transition_run_status(checkpoint.run_status, RunStatus.RUNNING),
            stages=(
                checkpoint.stages[:index]
                + (running,)
                + checkpoint.stages[index + 1 :]
            ),
        )
        return (
            self._commit_events(
                candidate,
                checkpoint.revision,
                (
                    (TraceEventKind.RUN_RESUMED, None, None, None, None, None),
                    (TraceEventKind.STAGE_STARTED, running, None, None, None, None),
                ),
            ),
            running,
        )

    def _succeed_stage(
        self,
        checkpoint: Checkpoint,
        state: StageState,
        result: StageResult,
    ) -> Checkpoint:
        index = self._stage_index(state.stage_id)
        succeeded = replace(
            transition_stage_state(state, StageStatus.SUCCEEDED),
            exit_code=0,
            output=result.output,
        )
        candidate = replace(
            checkpoint,
            revision=checkpoint.revision + 1,
            stages=(
                checkpoint.stages[:index]
                + (succeeded,)
                + checkpoint.stages[index + 1 :]
            ),
        )
        return self._commit_events(
            candidate,
            checkpoint.revision,
            ((TraceEventKind.STAGE_SUCCEEDED, succeeded, 0, None, None, None),),
        )

    def _succeed_run(self, checkpoint: Checkpoint, resumed: bool) -> RunResult:
        candidate = replace(
            checkpoint,
            revision=checkpoint.revision + 1,
            run_status=transition_run_status(checkpoint.run_status, RunStatus.SUCCEEDED),
        )
        checkpoint = self._commit_events(
            candidate,
            checkpoint.revision,
            ((TraceEventKind.RUN_SUCCEEDED, None, 0, None, None, None),),
        )
        return self._result(checkpoint, ExitCode.OK, None, resumed=resumed)

    def _fail_explicit(
        self,
        checkpoint: Checkpoint,
        state: StageState,
        result: StageResult,
        resumed: bool,
    ) -> RunResult:
        error = StageFailedError(result.stage_id, result.exit_code)
        return self._commit_failure(
            checkpoint,
            state,
            result.exit_code,
            result.output,
            error,
            ExitCode.STAGE_FAILED,
            resumed,
        )

    def _fail_internal(
        self,
        checkpoint: Checkpoint,
        state: StageState,
        error: Exception,
        resumed: bool,
    ) -> RunResult:
        normalized_error = normalize_exception(error)
        return self._commit_failure(
            checkpoint,
            state,
            int(ExitCode.INTERNAL_ERROR),
            {"error_type": normalized_error[0], "message": normalized_error[1]},
            normalized_error,
            ExitCode.INTERNAL_ERROR,
            resumed,
        )

    def _commit_failure(
        self,
        checkpoint: Checkpoint,
        state: StageState,
        stage_exit_code: int,
        output: object,
        error: Exception | tuple[str, str],
        result_exit_code: ExitCode,
        resumed: bool,
    ) -> RunResult:
        normalized_error = (
            error if type(error) is tuple else normalize_exception(error)
        )
        index = self._stage_index(state.stage_id)
        failed = replace(
            transition_stage_state(state, StageStatus.FAILED),
            exit_code=stage_exit_code,
            output=output,
        )
        candidate = replace(
            checkpoint,
            revision=checkpoint.revision + 1,
            run_status=transition_run_status(checkpoint.run_status, RunStatus.FAILED),
            stages=(
                checkpoint.stages[:index]
                + (failed,)
                + checkpoint.stages[index + 1 :]
            ),
        )
        checkpoint = self._commit_events(
            candidate,
            checkpoint.revision,
            (
                (
                    TraceEventKind.STAGE_FAILED,
                    failed,
                    stage_exit_code,
                    normalized_error,
                    None,
                    None,
                ),
                (
                    TraceEventKind.RUN_FAILED,
                    None,
                    int(result_exit_code),
                    normalized_error,
                    None,
                    None,
                ),
            ),
        )
        return self._result(
            checkpoint,
            result_exit_code,
            failed.stage_id,
            resumed=resumed,
        )

    def _commit_events(
        self,
        candidate: Checkpoint,
        expected_revision: int | None,
        events: tuple,
    ) -> Checkpoint:
        recorded_at = self._recorded_at()
        for (
            kind,
            state,
            exit_code,
            error,
            previous_generation,
            previous_run_status,
        ) in events:
            event = self._event(
                candidate,
                kind,
                recorded_at,
                state,
                exit_code,
                error,
                previous_generation,
                previous_run_status,
            )
            candidate = replace(candidate, events=candidate.events + (event,))
        self._store.commit(candidate, expected_revision=expected_revision)
        return candidate

    def _event(
        self,
        checkpoint: Checkpoint,
        kind: TraceEventKind,
        recorded_at: str,
        state: StageState | None,
        exit_code: int | None,
        error: tuple[str, str] | None,
        previous_generation: int | None,
        previous_run_status: RunStatus | None,
    ) -> TraceEvent:
        sequence = sum(
            event.generation == checkpoint.generation
            for event in checkpoint.events
        )
        return TraceEvent(
            f"{checkpoint.run_id}:{sequence:06d}",
            checkpoint.generation,
            sequence,
            kind,
            recorded_at,
            checkpoint.run_status,
            None if state is None else state.stage_id,
            None if state is None else state.status,
            None if state is None else state.attempts,
            exit_code,
            None if error is None else error[0],
            None if error is None else error[1],
            previous_generation,
            previous_run_status,
        )

    def _recorded_at(self) -> str:
        return format_recorded_at(self._clock.now_utc())

    def _stage_index(self, stage_id: str) -> int:
        return self._queue.ordered_stage_ids().index(stage_id)

    def _spec(self, stage_id: str) -> StageSpec:
        return self._job.stages[self._stage_index(stage_id)]

    def _validate_interrupt_before(self, interrupt_before: str | None) -> None:
        if (
            interrupt_before is not None
            and interrupt_before not in self._queue.ordered_stage_ids()
        ):
            raise UnknownInterruptStageError(
                f"unknown interrupt stage '{interrupt_before}'"
            )

    def _snapshot_result(self, spec: StageSpec, result: object) -> StageResult:
        if type(result) is not StageResult:
            raise ExecutorContractError(
                f"executor must return StageResult for stage '{spec.stage_id}'"
            )
        if type(result.stage_id) is not str:
            raise ExecutorContractError(
                f"executor result stage_id must be an exact string for stage "
                f"'{spec.stage_id}'"
            )
        if result.stage_id != spec.stage_id:
            raise ExecutorContractError(
                f"executor result stage_id does not match stage '{spec.stage_id}'"
            )
        if type(result.exit_code) is not int:
            raise ExecutorContractError(
                f"executor result exit_code must be an integer for stage "
                f"'{spec.stage_id}'"
            )
        if not 0 <= result.exit_code <= 255:
            raise ExecutorContractError(
                f"executor result exit_code must be between 0 and 255 for stage "
                f"'{spec.stage_id}'"
            )
        try:
            snapshot = snapshot_json_value(result.output, max_depth=STAGE_OUTPUT_MAX_DEPTH)
            durable_output = strict_json_loads(fingerprint_json_bytes(snapshot))
        except (ValueError, RecursionError, RuntimeError) as exc:
            raise ExecutorContractError(
                f"executor result output is not JSON-compatible for stage "
                f"'{spec.stage_id}': {exc}"
            ) from exc
        return StageResult(result.stage_id, result.exit_code, durable_output)

    def _detached_spec(self, spec: StageSpec) -> StageSpec:
        return StageSpec(
            spec.stage_id,
            spec.action,
            snapshot_json_value(spec.payload),
            tuple(spec.requirement_ids),
            tuple(spec.interface_ids),
            tuple(spec.test_ids),
        )

    def _result(
        self,
        checkpoint: Checkpoint,
        exit_code: ExitCode,
        failed_stage_id: str | None,
        *,
        resumed: bool,
        status: RunStatus | None = None,
        durable_status: RunStatus | None = None,
    ) -> RunResult:
        return RunResult(
            checkpoint.job_id,
            checkpoint.run_id,
            checkpoint.generation,
            checkpoint.run_status if status is None else status,
            checkpoint.run_status if durable_status is None else durable_status,
            tuple(
                state.stage_id
                for state in checkpoint.stages
                if state.status is StageStatus.SUCCEEDED
            ),
            failed_stage_id,
            exit_code,
            resumed,
            checkpoint.revision,
        )
