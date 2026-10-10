import inspect
import json
import math
import os
import unittest
from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from racer.checkpoint import Checkpoint, JsonCheckpointStore
from racer.clock import FixedClock
from racer.errors import (
    CheckpointCommitError,
    CheckpointFingerprintMismatchError,
    CheckpointFormatError,
    InvalidStateError,
    JobValidationError,
    StageFailedError,
    UnknownInterruptStageError,
)
from racer.exit_codes import ExitCode
from racer.job import (
    InterfaceSpec,
    JobSpec,
    RequirementSpec,
    StageSpec,
    TestSpec,
    job_fingerprint,
    load_job,
)
from racer.runner import RunResult, Runner
from racer.stages import FixtureStageExecutor
from racer.state import RunStatus, StageContext, StageResult, StageState, StageStatus
from racer.traceability import TraceEvent, TraceEventKind, build_traceability_snapshot


FIXTURES = Path(__file__).parent / "fixtures"
INSTANT = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


def ordered_job() -> JobSpec:
    return JobSpec(
        schema_version=1,
        job_id="ordered_job",
        requirements=(RequirementSpec("req", "Requirement"),),
        interfaces=(InterfaceSpec("api", "API", ("req",)),),
        tests=(TestSpec("test", "Test", ("req",), ("api",)),),
        stages=(
            StageSpec("first", "emit", "one", ("req",), ("api",), ("test",)),
            StageSpec("second", "emit", "two", ("req",), ("api",), ("test",)),
        ),
    )


class RecordingStore:
    def __init__(self, store, fail_commit=None, checkpoint_path: Path | None = None) -> None:
        self._store = store
        self._fail_commit = fail_commit
        self._checkpoint_path = checkpoint_path
        self.commits: list[tuple[object, int | None]] = []
        self.failure_bytes: bytes | None = None
        self.failure_snapshot = None

    def exists(self) -> bool:
        return self._store.exists()

    def load(self):
        return self._store.load()

    def commit(self, checkpoint, *, expected_revision) -> None:
        self.commits.append((checkpoint, expected_revision))
        if self._fail_commit is not None and self._fail_commit(checkpoint):
            if self._checkpoint_path is not None:
                self.failure_bytes = self._checkpoint_path.read_bytes()
                self.failure_snapshot = self._store.load()
            raise CheckpointCommitError("injected commit failure")
        self._store.commit(checkpoint, expected_revision=expected_revision)


class SequenceClock:
    def __init__(self) -> None:
        self.calls = 0

    def now_utc(self) -> datetime:
        self.calls += 1
        return INSTANT.replace(microsecond=self.calls)


class ProbeStore:
    def __init__(self, store) -> None:
        self._store = store
        self.exists_calls = 0
        self.load_calls = 0
        self.commit_calls = 0

    def exists(self) -> bool:
        self.exists_calls += 1
        return self._store.exists()

    def load(self):
        self.load_calls += 1
        return self._store.load()

    def commit(self, checkpoint, *, expected_revision) -> None:
        self.commit_calls += 1
        self._store.commit(checkpoint, expected_revision=expected_revision)


def commit_history(store: JsonCheckpointStore, *checkpoints: Checkpoint) -> None:
    expected_revision: int | None = None
    for checkpoint in checkpoints:
        store.commit(checkpoint, expected_revision=expected_revision)
        expected_revision = checkpoint.revision


class RunnerTests(unittest.TestCase):
    def test_runner_identity_mismatch_is_zero_side_effects_at_every_entry(self) -> None:
        def directory_snapshot(root):
            return {
                str(path.relative_to(root)): (
                    path.is_dir(),
                    path.stat().st_mtime_ns,
                    None if path.is_dir() else path.read_bytes(),
                )
                for path in (root, *root.rglob("*"))
            }

        class ObservedExecutor(FixtureStageExecutor):
            def __init__(self):
                self.calls = []

            def execute(self, spec, context):
                self.calls.append(spec.stage_id)
                return super().execute(spec, context)

        job_a = ordered_job()
        job_b = replace(
            job_a,
            stages=(replace(job_a.stages[0], payload="different"), job_a.stages[1]),
        )
        for other_job in (job_b, replace(job_b, job_id="other_job")):
            for status in (RunStatus.READY, RunStatus.RUNNING, RunStatus.SUCCEEDED):
                for operation in ("run", "resume", "reset", "trace"):
                    with self.subTest(job_id=other_job.job_id, status=status, operation=operation):
                        with TemporaryDirectory() as directory:
                            root = Path(directory)
                            (root / "unrelated").mkdir()
                            (root / "unrelated" / "keep.txt").write_bytes(b"keep")
                            store = JsonCheckpointStore(root, job_a)
                            setup = Runner(job_a, store, FixtureStageExecutor(), FixedClock(INSTANT))
                            if status is RunStatus.RUNNING:
                                setup.run(interrupt_before="second")
                            else:
                                setup.run()
                                if status is RunStatus.READY:
                                    setup.reset()
                            durable_before = store.load()
                            files_before = directory_snapshot(root)
                            executor = ObservedExecutor()
                            clock = SequenceClock()
                            probe = ProbeStore(store)
                            runner = Runner(other_job, probe, executor, clock)

                            with self.assertRaises(CheckpointFingerprintMismatchError) as caught:
                                getattr(runner, operation)()

                            self.assertEqual(caught.exception.exit_code, ExitCode.INVALID_STATE)
                            self.assertEqual(executor.calls, [])
                            self.assertEqual(clock.calls, 0)
                            self.assertEqual(probe.commit_calls, 0)
                            self.assertEqual(store.load(), durable_before)
                            self.assertEqual(directory_snapshot(root), files_before)

    def test_runner_identity_uses_content_not_python_object_identity(self) -> None:
        job = ordered_job()
        equivalent_job = replace(job, stages=tuple(replace(stage) for stage in job.stages))
        self.assertIsNot(job, equivalent_job)
        self.assertEqual(job_fingerprint(job), job_fingerprint(equivalent_job))
        with TemporaryDirectory() as directory:
            store = JsonCheckpointStore(directory, job)
            Runner(job, store, FixtureStageExecutor(), FixedClock(INSTANT)).run(
                interrupt_before="second"
            )
            runner = Runner(equivalent_job, store, FixtureStageExecutor(), FixedClock(INSTANT))
            self.assertEqual(runner.resume().status, RunStatus.SUCCEEDED)
            self.assertEqual(runner.trace().job_fingerprint, job_fingerprint(job))
            self.assertEqual(runner.reset().status, RunStatus.READY)
            self.assertEqual(runner.run().status, RunStatus.SUCCEEDED)

    def test_depth_64_payload_has_exact_65_layer_fixture_output(self) -> None:
        def depth(value):
            if type(value) is list:
                return 1 + max((depth(item) for item in value), default=0)
            if type(value) is dict:
                return 1 + max((depth(item) for item in value.values()), default=0)
            return 0

        payload = "leaf"
        for _ in range(64):
            payload = [payload]
        self.assertEqual(depth(payload), 64)
        for action, wrapper, expected_code in (("emit", "emitted", 0), ("fail", "error", 4)):
            with self.subTest(action=action):
                job = ordered_job()
                job = replace(job, stages=(replace(job.stages[0], action=action, payload=payload),))
                executor = FixtureStageExecutor()
                output = executor.execute(
                    job.stages[0], StageContext(job.job_id, job.job_id + ":0", 0, "first", 1)
                ).output
                self.assertEqual(output, {wrapper: payload})
                self.assertEqual(depth(output), 65)
                with TemporaryDirectory() as directory:
                    result = self.make_runner(job, executor, directory).run()
                    self.assertEqual(result.exit_code, expected_code)
                    saved = JsonCheckpointStore(directory, job).load()
                    self.assertEqual(saved.stages[0].output, output)
                    self.assertEqual(depth(saved.stages[0].output), 65)

    def test_executor_output_depth_65_accepts_and_66_rejects(self) -> None:
        for layers, expected_code in ((65, ExitCode.OK), (66, ExitCode.INTERNAL_ERROR)):
            with self.subTest(layers=layers):
                output = "leaf"
                for _ in range(layers):
                    output = [output]

                class Executor:
                    def execute(self, spec, context):
                        return StageResult(spec.stage_id, 0, output)

                with TemporaryDirectory() as directory:
                    job = ordered_job()
                    result = self.make_runner(job, Executor(), directory).run()
                    self.assertEqual(result.exit_code, expected_code)
                    saved = JsonCheckpointStore(directory, job).load()
                    if layers == 65:
                        self.assertEqual(saved.stages[0].output, output)
                    else:
                        self.assertEqual(saved.events[-1].error_type, "ExecutorContractError")
                        self.assertIn("maximum depth 65", saved.events[-1].error_message)

    def make_runner(self, job, executor, directory):
        from racer.runner import Runner

        return Runner(job, JsonCheckpointStore(directory, job), executor, FixedClock(INSTANT))

    def assert_exact_failure(
        self,
        checkpoint,
        result,
        store: RecordingStore,
        *,
        expected_result: RunResult,
        expected_run_id: str,
        expected_revision: int,
        failed_stage_id: str,
        stage_exit_code: int,
        result_exit_code: ExitCode,
        error_type: str,
        error_message: str,
    ) -> None:
        self.assertEqual(
            result,
            expected_result,
        )
        self.assertEqual(checkpoint.run_id, expected_run_id)
        self.assertEqual(checkpoint.revision, expected_revision)
        self.assertEqual(checkpoint.run_status, RunStatus.FAILED)
        stage_event, run_event = checkpoint.events[-2:]
        self.assertEqual(stage_event.kind, TraceEventKind.STAGE_FAILED)
        self.assertEqual(run_event.kind, TraceEventKind.RUN_FAILED)
        self.assertEqual(stage_event.run_status, RunStatus.FAILED)
        self.assertEqual(run_event.run_status, RunStatus.FAILED)
        self.assertEqual(
            (stage_event.stage_id, stage_event.stage_status, stage_event.attempt),
            (failed_stage_id, StageStatus.FAILED, 1),
        )
        self.assertEqual(run_event.stage_id, None)
        self.assertEqual(run_event.stage_status, None)
        self.assertEqual(run_event.attempt, None)
        self.assertEqual(stage_event.exit_code, stage_exit_code)
        self.assertEqual(run_event.exit_code, int(result_exit_code))
        self.assertEqual(stage_event.error_type, error_type)
        self.assertEqual(run_event.error_type, error_type)
        self.assertEqual(stage_event.error_message, error_message)
        self.assertEqual(run_event.error_message, error_message)
        self.assertEqual(stage_event.recorded_at, run_event.recorded_at)
        self.assertEqual(run_event.sequence, stage_event.sequence + 1)
        self.assertEqual(
            (stage_event.event_id, run_event.event_id),
            (
                f"{expected_run_id}:{stage_event.sequence:06d}",
                f"{expected_run_id}:{run_event.sequence:06d}",
            ),
        )
        self.assertEqual(
            [event.sequence for event in checkpoint.events],
            list(range(len(checkpoint.events))),
        )
        failed_commits = [
            (candidate, expected_revision)
            for candidate, expected_revision in store.commits
            if candidate.run_status is RunStatus.FAILED
        ]
        self.assertEqual(len(failed_commits), 1)
        candidate, committed_from_revision = failed_commits[0]
        self.assertEqual(candidate, checkpoint)
        self.assertEqual(committed_from_revision, expected_revision - 1)
        self.assertEqual(candidate.revision, committed_from_revision + 1)

    def test_fresh_run_executes_in_order(self) -> None:
        job = ordered_job()
        calls: list[str] = []

        class RecordingExecutor:
            def execute(self, spec, context):
                calls.append(spec.stage_id)
                return StageResult(spec.stage_id, 0, {"stage": spec.stage_id})

        with TemporaryDirectory() as directory:
            result = self.make_runner(job, RecordingExecutor(), Path(directory)).run()

        self.assertEqual(calls, ["first", "second"])
        self.assertEqual(result.completed_stage_ids, ("first", "second"))

    def test_run_continues_existing_ready_checkpoint(self) -> None:
        job = ordered_job()
        calls: list[str] = []

        class RecordingExecutor:
            def execute(self, spec, context):
                calls.append(spec.stage_id)
                return StageResult(spec.stage_id, 0, {"stage": spec.stage_id})

        with TemporaryDirectory() as directory:
            path = Path(directory)
            runner = self.make_runner(job, RecordingExecutor(), path)
            runner._create_checkpoint()
            result = runner.run()
            checkpoint = JsonCheckpointStore(path, job).load()

        self.assertEqual(calls, ["first", "second"])
        self.assertEqual(result.resumed, False)
        self.assertEqual(result.generation, 0)
        self.assertEqual(result.checkpoint_revision, 6)
        self.assertEqual(
            [event.kind for event in checkpoint.events],
            [
                TraceEventKind.CHECKPOINT_CREATED,
                TraceEventKind.RUN_STARTED,
                TraceEventKind.STAGE_STARTED,
                TraceEventKind.STAGE_SUCCEEDED,
                TraceEventKind.STAGE_STARTED,
                TraceEventKind.STAGE_SUCCEEDED,
                TraceEventKind.RUN_SUCCEEDED,
            ],
        )

    def test_reset_missing_checkpoint_without_write(self) -> None:
        job = ordered_job()

        class Executor:
            def execute(self, spec, context):
                self.fail("executor must not run")

        with TemporaryDirectory() as directory:
            path = Path(directory) / "missing"
            runner = self.make_runner(job, Executor(), path)
            with self.assertRaises(InvalidStateError):
                runner.reset()
            self.assertFalse(path.exists())

    def test_reset_ready_checkpoint_is_no_op(self) -> None:
        job = ordered_job()

        class Executor:
            def execute(self, spec, context):
                self.fail("executor must not run")

        with TemporaryDirectory() as directory:
            path = Path(directory)
            runner = self.make_runner(job, Executor(), path)
            runner._create_checkpoint()
            before = (path / "checkpoint.json").read_bytes()
            result = runner.reset()
            after = (path / "checkpoint.json").read_bytes()

        self.assertEqual(before, after)
        self.assertEqual(
            result,
            RunResult(
                "ordered_job",
                "ordered_job:0",
                0,
                RunStatus.READY,
                RunStatus.READY,
                (),
                None,
                ExitCode.OK,
                False,
                0,
            ),
        )

    def test_rejected_and_no_op_lifecycle_operations_are_zero_side_effects(self) -> None:
        job = ordered_job()
        calls: list[str] = []

        class Executor:
            def execute(self, spec, context):
                calls.append(spec.stage_id)
                return StageResult(spec.stage_id, 0, None)

        from racer.runner import Runner
        with TemporaryDirectory() as directory:
            root = Path(directory)

            missing_store = RecordingStore(JsonCheckpointStore(root / "missing", job))
            missing_clock = SequenceClock()
            with self.assertRaises(InvalidStateError):
                Runner(job, missing_store, Executor(), missing_clock).reset()
            self.assertEqual((missing_clock.calls, calls, missing_store.commits), (0, [], []))

            ready_checkpoint = Checkpoint(
                1, 0, 0, "ordered_job:0", "ordered_job", job_fingerprint(job),
                RunStatus.READY,
                (
                    StageState("first", StageStatus.PENDING, 0, None, None),
                    StageState("second", StageStatus.PENDING, 0, None, None),
                ),
                build_traceability_snapshot(job),
                (
                    TraceEvent("ordered_job:0:000000", 0, 0, TraceEventKind.CHECKPOINT_CREATED, "2026-09-02T12:00:00.000000Z", RunStatus.READY, None, None, None, None, None, None, None, None),
                ),
            )
            ready_base = JsonCheckpointStore(root / "ready", job)
            ready_base.commit(ready_checkpoint, expected_revision=None)
            ready_store = RecordingStore(ready_base)
            ready_clock = SequenceClock()
            before = (root / "ready" / "checkpoint.json").read_bytes()
            Runner(job, ready_store, Executor(), ready_clock).reset()
            self.assertEqual((root / "ready" / "checkpoint.json").read_bytes(), before)
            self.assertEqual((ready_clock.calls, calls, ready_store.commits), (0, [], []))

            resume_store = RecordingStore(ready_base)
            resume_clock = SequenceClock()
            with self.assertRaises(InvalidStateError):
                Runner(job, resume_store, Executor(), resume_clock).resume()
            self.assertEqual((resume_clock.calls, calls, resume_store.commits), (0, [], []))

            running_base = JsonCheckpointStore(root / "running", job)
            Runner(job, running_base, Executor(), FixedClock(INSTANT)).run(
                interrupt_before="first"
            )
            calls.clear()
            run_store = RecordingStore(running_base)
            run_clock = SequenceClock()
            before = (root / "running" / "checkpoint.json").read_bytes()
            with self.assertRaises(InvalidStateError):
                Runner(job, run_store, Executor(), run_clock).run()
            self.assertEqual((root / "running" / "checkpoint.json").read_bytes(), before)
            self.assertEqual((run_clock.calls, calls, run_store.commits), (0, [], []))

    def test_task7_lifecycle_commit_failures_preserve_durable_truth(self) -> None:
        job = ordered_job()
        calls: list[str] = []

        class Executor:
            def execute(self, spec, context):
                calls.append(spec.stage_id)
                return StageResult(spec.stage_id, 0, None)

        ready = Checkpoint(
            1, 0, 0, "ordered_job:0", "ordered_job", job_fingerprint(job),
            RunStatus.READY,
            (StageState("first", StageStatus.PENDING, 0, None, None), StageState("second", StageStatus.PENDING, 0, None, None)),
            build_traceability_snapshot(job),
            (TraceEvent("ordered_job:0:000000", 0, 0, TraceEventKind.CHECKPOINT_CREATED, "2026-09-02T12:00:00.000000Z", RunStatus.READY, None, None, None, None, None, None, None, None),),
        )
        from racer.runner import Runner

        def snapshot(checkpoint):
            return (
                checkpoint.generation,
                checkpoint.revision,
                checkpoint.run_status,
                checkpoint.stages,
                checkpoint.events,
            )

        def assert_preserved(path, store, before_bytes, before_snapshot) -> None:
            self.assertEqual(path.read_bytes(), before_bytes)
            self.assertEqual(snapshot(store.load()), before_snapshot)

        with TemporaryDirectory() as directory:
            root = Path(directory)

            ready_base = JsonCheckpointStore(root / "ready", job)
            ready_base.commit(ready, expected_revision=None)
            ready_before = (root / "ready" / "checkpoint.json").read_bytes()
            ready_snapshot = snapshot(ready_base.load())
            ready_store = RecordingStore(
                ready_base,
                lambda checkpoint: checkpoint.events[-1].kind is TraceEventKind.RUN_STARTED,
                root / "ready" / "checkpoint.json",
            )
            result = None
            with self.assertRaises(CheckpointCommitError):
                result = Runner(job, ready_store, Executor(), FixedClock(INSTANT)).run()
            self.assertIsNone(result)
            self.assertEqual(ready_store.failure_bytes, ready_before)
            self.assertEqual(snapshot(ready_store.failure_snapshot), ready_snapshot)
            assert_preserved(
                root / "ready" / "checkpoint.json",
                ready_base,
                ready_before,
                ready_snapshot,
            )
            self.assertEqual(calls, [])

            active_base = JsonCheckpointStore(root / "active", job)
            Runner(job, active_base, Executor(), FixedClock(INSTANT)).run(interrupt_before="first")
            calls.clear()
            active_before = (root / "active" / "checkpoint.json").read_bytes()
            active_snapshot = snapshot(active_base.load())
            first_store = RecordingStore(
                active_base,
                lambda checkpoint: checkpoint.run_status is RunStatus.INTERRUPTED,
                root / "active" / "checkpoint.json",
            )
            result = None
            with self.assertRaises(CheckpointCommitError):
                result = Runner(job, first_store, Executor(), FixedClock(INSTANT)).resume()
            self.assertIsNone(result)
            self.assertEqual(first_store.failure_bytes, active_before)
            self.assertEqual(snapshot(first_store.failure_snapshot), active_snapshot)
            assert_preserved(
                root / "active" / "checkpoint.json",
                active_base,
                active_before,
                active_snapshot,
            )
            self.assertEqual(calls, [])

            second_base = JsonCheckpointStore(root / "second", job)
            Runner(job, second_base, Executor(), FixedClock(INSTANT)).run(interrupt_before="first")
            second_store = RecordingStore(
                second_base,
                lambda checkpoint: len(checkpoint.events) >= 2 and checkpoint.events[-2].kind is TraceEventKind.RUN_RESUMED,
                root / "second" / "checkpoint.json",
            )
            result = None
            with self.assertRaises(CheckpointCommitError):
                result = Runner(job, second_store, Executor(), FixedClock(INSTANT)).resume()
            self.assertIsNone(result)
            second_snapshot = snapshot(second_base.load())
            self.assertEqual((root / "second" / "checkpoint.json").read_bytes(), second_store.failure_bytes)
            self.assertEqual(second_snapshot, snapshot(second_store.failure_snapshot))
            durable = second_base.load()
            self.assertEqual((durable.run_status, durable.revision), (RunStatus.INTERRUPTED, 3))
            self.assertEqual([event.kind for event in durable.events[-2:]], [TraceEventKind.STAGE_INTERRUPTED, TraceEventKind.RUN_INTERRUPTED])
            self.assertEqual([(state.status, state.attempts) for state in durable.stages], [(StageStatus.INTERRUPTED, 1), (StageStatus.PENDING, 0)])
            self.assertEqual(calls, [])

            no_active_events = (
                ready.events[0],
                TraceEvent("ordered_job:0:000001", 0, 1, TraceEventKind.RUN_STARTED, "2026-09-02T12:00:00.000000Z", RunStatus.RUNNING, None, None, None, None, None, None, None, None),
            )
            no_active = Checkpoint(
                1, 1, 0, "ordered_job:0", "ordered_job", job_fingerprint(job), RunStatus.RUNNING,
                (StageState("first", StageStatus.PENDING, 0, None, None), StageState("second", StageStatus.PENDING, 0, None, None)),
                build_traceability_snapshot(job), no_active_events,
            )
            no_active_base = JsonCheckpointStore(root / "no-active", job)
            commit_history(no_active_base, ready, no_active)
            no_active_before = (root / "no-active" / "checkpoint.json").read_bytes()
            no_active_snapshot = snapshot(no_active_base.load())
            no_active_store = RecordingStore(
                no_active_base,
                lambda checkpoint: checkpoint.events[-1].kind is TraceEventKind.STAGE_STARTED,
                root / "no-active" / "checkpoint.json",
            )
            result = None
            with self.assertRaises(CheckpointCommitError):
                result = Runner(job, no_active_store, Executor(), FixedClock(INSTANT)).resume()
            self.assertIsNone(result)
            self.assertEqual(no_active_store.failure_bytes, no_active_before)
            self.assertEqual(snapshot(no_active_store.failure_snapshot), no_active_snapshot)
            assert_preserved(
                root / "no-active" / "checkpoint.json",
                no_active_base,
                no_active_before,
                no_active_snapshot,
            )
            self.assertEqual(calls, [])

            all_events = (
                ready.events[0],
                TraceEvent("ordered_job:0:000001", 0, 1, TraceEventKind.RUN_STARTED, "2026-09-02T12:00:00.000000Z", RunStatus.RUNNING, None, None, None, None, None, None, None, None),
                TraceEvent("ordered_job:0:000002", 0, 2, TraceEventKind.STAGE_STARTED, "2026-09-02T12:00:00.000000Z", RunStatus.RUNNING, "first", StageStatus.RUNNING, 1, None, None, None, None, None),
                TraceEvent("ordered_job:0:000003", 0, 3, TraceEventKind.STAGE_SUCCEEDED, "2026-09-02T12:00:00.000000Z", RunStatus.RUNNING, "first", StageStatus.SUCCEEDED, 1, 0, None, None, None, None),
                TraceEvent("ordered_job:0:000004", 0, 4, TraceEventKind.STAGE_STARTED, "2026-09-02T12:00:00.000000Z", RunStatus.RUNNING, "second", StageStatus.RUNNING, 1, None, None, None, None, None),
                TraceEvent("ordered_job:0:000005", 0, 5, TraceEventKind.STAGE_SUCCEEDED, "2026-09-02T12:00:00.000000Z", RunStatus.RUNNING, "second", StageStatus.SUCCEEDED, 1, 0, None, None, None, None),
            )
            all_succeeded = Checkpoint(
                1, 5, 0, "ordered_job:0", "ordered_job", job_fingerprint(job), RunStatus.RUNNING,
                (StageState("first", StageStatus.SUCCEEDED, 1, 0, None), StageState("second", StageStatus.SUCCEEDED, 1, 0, None)),
                build_traceability_snapshot(job), all_events,
            )
            all_base = JsonCheckpointStore(root / "all-succeeded", job)
            commit_history(
                all_base,
                ready,
                no_active,
                Checkpoint(1, 2, 0, "ordered_job:0", "ordered_job", job_fingerprint(job), RunStatus.RUNNING, (StageState("first", StageStatus.RUNNING, 1, None, None), StageState("second", StageStatus.PENDING, 0, None, None)), build_traceability_snapshot(job), all_events[:3]),
                Checkpoint(1, 3, 0, "ordered_job:0", "ordered_job", job_fingerprint(job), RunStatus.RUNNING, (StageState("first", StageStatus.SUCCEEDED, 1, 0, None), StageState("second", StageStatus.PENDING, 0, None, None)), build_traceability_snapshot(job), all_events[:4]),
                Checkpoint(1, 4, 0, "ordered_job:0", "ordered_job", job_fingerprint(job), RunStatus.RUNNING, (StageState("first", StageStatus.SUCCEEDED, 1, 0, None), StageState("second", StageStatus.RUNNING, 1, None, None)), build_traceability_snapshot(job), all_events[:5]),
                all_succeeded,
            )
            all_store = RecordingStore(
                all_base,
                lambda checkpoint: checkpoint.events[-1].kind is TraceEventKind.RUN_SUCCEEDED,
                root / "all-succeeded" / "checkpoint.json",
            )
            all_before = (root / "all-succeeded" / "checkpoint.json").read_bytes()
            all_snapshot = snapshot(all_base.load())
            result = None
            with self.assertRaises(CheckpointCommitError):
                result = Runner(job, all_store, Executor(), FixedClock(INSTANT)).resume()
            self.assertIsNone(result)
            self.assertEqual(all_store.failure_bytes, all_before)
            self.assertEqual(snapshot(all_store.failure_snapshot), all_snapshot)
            assert_preserved(
                root / "all-succeeded" / "checkpoint.json",
                all_base,
                all_before,
                all_snapshot,
            )
            self.assertEqual(calls, [])

            reset_base = JsonCheckpointStore(root / "reset", job)
            Runner(job, reset_base, Executor(), FixedClock(INSTANT)).run(interrupt_before="first")
            reset_before = (root / "reset" / "checkpoint.json").read_bytes()
            reset_snapshot = snapshot(reset_base.load())
            reset_store = RecordingStore(
                reset_base,
                lambda checkpoint: checkpoint.events[-1].kind is TraceEventKind.RESET,
                root / "reset" / "checkpoint.json",
            )
            result = None
            with self.assertRaises(CheckpointCommitError):
                result = Runner(job, reset_store, Executor(), FixedClock(INSTANT)).reset()
            self.assertIsNone(result)
            self.assertEqual(reset_store.failure_bytes, reset_before)
            self.assertEqual(snapshot(reset_store.failure_snapshot), reset_snapshot)
            assert_preserved(
                root / "reset" / "checkpoint.json",
                reset_base,
                reset_before,
                reset_snapshot,
            )
            self.assertEqual(calls, [])

    def test_run_rejects_every_non_ready_status_without_side_effects(self) -> None:
        job = ordered_job()
        calls: list[str] = []

        class Executor:
            def execute(self, spec, context):
                calls.append(spec.stage_id)
                return StageResult(spec.stage_id, 0, None)

        from racer.runner import Runner
        with TemporaryDirectory() as directory:
            root = Path(directory)
            running = JsonCheckpointStore(root / "running", job)
            Runner(job, running, Executor(), FixedClock(INSTANT)).run(
                interrupt_before="first"
            )

            events = (
                TraceEvent("ordered_job:0:000000", 0, 0, TraceEventKind.CHECKPOINT_CREATED, "2026-09-02T12:00:00.000000Z", RunStatus.READY, None, None, None, None, None, None, None, None),
                TraceEvent("ordered_job:0:000001", 0, 1, TraceEventKind.RUN_STARTED, "2026-09-02T12:00:00.000000Z", RunStatus.RUNNING, None, None, None, None, None, None, None, None),
                TraceEvent("ordered_job:0:000002", 0, 2, TraceEventKind.STAGE_STARTED, "2026-09-02T12:00:00.000000Z", RunStatus.RUNNING, "first", StageStatus.RUNNING, 1, None, None, None, None, None),
                TraceEvent("ordered_job:0:000003", 0, 3, TraceEventKind.STAGE_INTERRUPTED, "2026-09-02T12:00:00.000000Z", RunStatus.INTERRUPTED, "first", StageStatus.INTERRUPTED, 1, None, None, None, None, None),
                TraceEvent("ordered_job:0:000004", 0, 4, TraceEventKind.RUN_INTERRUPTED, "2026-09-02T12:00:00.000000Z", RunStatus.INTERRUPTED, None, None, None, None, None, None, None, None),
            )
            interrupted = JsonCheckpointStore(root / "interrupted", job)
            commit_history(
                interrupted,
                Checkpoint(1, 0, 0, "ordered_job:0", "ordered_job", job_fingerprint(job), RunStatus.READY, (StageState("first", StageStatus.PENDING, 0, None, None), StageState("second", StageStatus.PENDING, 0, None, None)), build_traceability_snapshot(job), events[:1]),
                Checkpoint(1, 1, 0, "ordered_job:0", "ordered_job", job_fingerprint(job), RunStatus.RUNNING, (StageState("first", StageStatus.PENDING, 0, None, None), StageState("second", StageStatus.PENDING, 0, None, None)), build_traceability_snapshot(job), events[:2]),
                Checkpoint(1, 2, 0, "ordered_job:0", "ordered_job", job_fingerprint(job), RunStatus.RUNNING, (StageState("first", StageStatus.RUNNING, 1, None, None), StageState("second", StageStatus.PENDING, 0, None, None)), build_traceability_snapshot(job), events[:3]),
                Checkpoint(1, 3, 0, "ordered_job:0", "ordered_job", job_fingerprint(job), RunStatus.INTERRUPTED, (StageState("first", StageStatus.INTERRUPTED, 1, None, None), StageState("second", StageStatus.PENDING, 0, None, None)), build_traceability_snapshot(job), events),
            )

            failed_job = load_job(FIXTURES / "failing_job.json")
            from racer.stages import FixtureStageExecutor

            failed = JsonCheckpointStore(root / "failed", failed_job)
            Runner(failed_job, failed, FixtureStageExecutor(), FixedClock(INSTANT)).run()
            succeeded = JsonCheckpointStore(root / "succeeded", job)
            Runner(job, succeeded, Executor(), FixedClock(INSTANT)).run()

            for name, case_job, base, checkpoint_path in (
                ("RUNNING", job, running, root / "running" / "checkpoint.json"),
                ("INTERRUPTED", job, interrupted, root / "interrupted" / "checkpoint.json"),
                ("FAILED", failed_job, failed, root / "failed" / "checkpoint.json"),
                ("SUCCEEDED", job, succeeded, root / "succeeded" / "checkpoint.json"),
            ):
                with self.subTest(status=name):
                    calls.clear()
                    before = checkpoint_path.read_bytes()
                    probe = ProbeStore(base)
                    clock = SequenceClock()
                    with self.assertRaises(InvalidStateError):
                        Runner(case_job, probe, Executor(), clock).run()
                    self.assertEqual(checkpoint_path.read_bytes(), before)
                    self.assertEqual(calls, [])
                    self.assertEqual(clock.calls, 0)
                    self.assertEqual(
                        (probe.exists_calls, probe.load_calls, probe.commit_calls),
                        (1, 1, 0),
                    )

    def test_resume_rejects_terminal_and_ready_statuses_without_side_effects(self) -> None:
        job = ordered_job()
        calls: list[str] = []

        class Executor:
            def execute(self, spec, context):
                calls.append(spec.stage_id)
                return StageResult(spec.stage_id, 0, None)

        from racer.runner import Runner
        with TemporaryDirectory() as directory:
            root = Path(directory)
            ready = JsonCheckpointStore(root / "ready", job)
            ready_checkpoint = Checkpoint(
                1, 0, 0, "ordered_job:0", "ordered_job", job_fingerprint(job),
                RunStatus.READY,
                (StageState("first", StageStatus.PENDING, 0, None, None), StageState("second", StageStatus.PENDING, 0, None, None)),
                build_traceability_snapshot(job),
                (TraceEvent("ordered_job:0:000000", 0, 0, TraceEventKind.CHECKPOINT_CREATED, "2026-09-02T12:00:00.000000Z", RunStatus.READY, None, None, None, None, None, None, None, None),),
            )
            ready.commit(ready_checkpoint, expected_revision=None)
            failed_job = load_job(FIXTURES / "failing_job.json")
            from racer.stages import FixtureStageExecutor

            failed = JsonCheckpointStore(root / "failed", failed_job)
            Runner(failed_job, failed, FixtureStageExecutor(), FixedClock(INSTANT)).run()
            succeeded = JsonCheckpointStore(root / "succeeded", job)
            Runner(job, succeeded, Executor(), FixedClock(INSTANT)).run()

            for name, case_job, base, checkpoint_path, message in (
                ("READY", job, ready, root / "ready" / "checkpoint.json", "resume requires RUNNING or INTERRUPTED; found READY"),
                ("FAILED", failed_job, failed, root / "failed" / "checkpoint.json", None),
                ("SUCCEEDED", job, succeeded, root / "succeeded" / "checkpoint.json", None),
            ):
                with self.subTest(status=name):
                    calls.clear()
                    before = checkpoint_path.read_bytes()
                    probe = ProbeStore(base)
                    clock = SequenceClock()
                    if message is None:
                        context = self.assertRaises(InvalidStateError)
                    else:
                        context = self.assertRaisesRegex(InvalidStateError, message)
                    with context:
                        Runner(case_job, probe, Executor(), clock).resume()
                    self.assertEqual(checkpoint_path.read_bytes(), before)
                    self.assertEqual(calls, [])
                    self.assertEqual(clock.calls, 0)
                    self.assertEqual(
                        (probe.exists_calls, probe.load_calls, probe.commit_calls),
                        (0, 1, 0),
                    )

    def test_resume_rejects_illegal_checkpoint_shapes_without_side_effects(self) -> None:
        job = ordered_job()
        ready = Checkpoint(
            1, 0, 0, "ordered_job:0", "ordered_job", job_fingerprint(job),
            RunStatus.READY,
            (StageState("first", StageStatus.PENDING, 0, None, None), StageState("second", StageStatus.PENDING, 0, None, None)),
            build_traceability_snapshot(job),
            (TraceEvent("ordered_job:0:000000", 0, 0, TraceEventKind.CHECKPOINT_CREATED, "2026-09-02T12:00:00.000000Z", RunStatus.READY, None, None, None, None, None, None, None, None),),
        )
        calls: list[str] = []

        class Executor:
            def execute(self, spec, context):
                calls.append(spec.stage_id)
                return StageResult(spec.stage_id, 0, None)

        from racer.runner import Runner
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for name, status, stages in (
                ("multiple_running", "RUNNING", (("RUNNING", 1), ("RUNNING", 1))),
                ("interrupted_without_stage", "INTERRUPTED", (("PENDING", 0), ("PENDING", 0))),
            ):
                with self.subTest(shape=name):
                    path = root / name
                    base = JsonCheckpointStore(path, job)
                    base.commit(ready, expected_revision=None)
                    content = json.loads((path / "checkpoint.json").read_bytes())
                    content["run_status"] = status
                    for stage, (stage_status, attempts) in zip(content["stages"], stages):
                        stage["status"] = stage_status
                        stage["attempts"] = attempts
                    checkpoint_path = path / "checkpoint.json"
                    checkpoint_path.write_text(json.dumps(content), encoding="utf-8")
                    before = checkpoint_path.read_bytes()
                    calls.clear()
                    probe = ProbeStore(base)
                    clock = SequenceClock()
                    with self.assertRaises(CheckpointFormatError):
                        Runner(job, probe, Executor(), clock).resume()
                    self.assertEqual(checkpoint_path.read_bytes(), before)
                    self.assertEqual(calls, [])
                    self.assertEqual(clock.calls, 0)
                    self.assertEqual(
                        (probe.exists_calls, probe.load_calls, probe.commit_calls),
                        (0, 1, 0),
                    )

    def test_unknown_interrupt_targets_reject_before_store_access(self) -> None:
        job = ordered_job()
        calls: list[str] = []

        class Executor:
            def execute(self, spec, context):
                calls.append(spec.stage_id)
                return StageResult(spec.stage_id, 0, None)

        from racer.runner import Runner
        with TemporaryDirectory() as directory:
            root = Path(directory)
            absent_path = root / "absent"
            absent_probe = ProbeStore(JsonCheckpointStore(absent_path, job))
            absent_clock = SequenceClock()
            with self.assertRaises(UnknownInterruptStageError):
                Runner(job, absent_probe, Executor(), absent_clock).run(
                    interrupt_before="unknown"
                )
            self.assertFalse(absent_path.exists())
            self.assertEqual(calls, [])
            self.assertEqual(absent_clock.calls, 0)
            self.assertEqual(
                (absent_probe.exists_calls, absent_probe.load_calls, absent_probe.commit_calls),
                (0, 0, 0),
            )

            ready = Checkpoint(
                1, 0, 0, "ordered_job:0", "ordered_job", job_fingerprint(job),
                RunStatus.READY,
                (StageState("first", StageStatus.PENDING, 0, None, None), StageState("second", StageStatus.PENDING, 0, None, None)),
                build_traceability_snapshot(job),
                (TraceEvent("ordered_job:0:000000", 0, 0, TraceEventKind.CHECKPOINT_CREATED, "2026-09-02T12:00:00.000000Z", RunStatus.READY, None, None, None, None, None, None, None, None),),
            )
            existing = JsonCheckpointStore(root / "existing", job)
            existing.commit(ready, expected_revision=None)
            before = (root / "existing" / "checkpoint.json").read_bytes()
            existing_probe = ProbeStore(existing)
            existing_clock = SequenceClock()
            with self.assertRaises(UnknownInterruptStageError):
                Runner(job, existing_probe, Executor(), existing_clock).resume(
                    interrupt_before="unknown"
                )
            self.assertEqual((root / "existing" / "checkpoint.json").read_bytes(), before)
            self.assertEqual(calls, [])
            self.assertEqual(existing_clock.calls, 0)
            self.assertEqual(
                (existing_probe.exists_calls, existing_probe.load_calls, existing_probe.commit_calls),
                (0, 0, 0),
            )

    def test_resume_load_rejections_are_zero_side_effects(self) -> None:
        job = ordered_job()
        ready = Checkpoint(
            1, 0, 0, "ordered_job:0", "ordered_job", job_fingerprint(job),
            RunStatus.READY,
            (StageState("first", StageStatus.PENDING, 0, None, None), StageState("second", StageStatus.PENDING, 0, None, None)),
            build_traceability_snapshot(job),
            (TraceEvent("ordered_job:0:000000", 0, 0, TraceEventKind.CHECKPOINT_CREATED, "2026-09-02T12:00:00.000000Z", RunStatus.READY, None, None, None, None, None, None, None, None),),
        )
        calls: list[str] = []

        class Executor:
            def execute(self, spec, context):
                calls.append(spec.stage_id)
                return StageResult(spec.stage_id, 0, None)

        changed_job = JobSpec(
            schema_version=1,
            job_id="ordered_job",
            requirements=(RequirementSpec("req", "Requirement"),),
            interfaces=(InterfaceSpec("api", "API", ("req",)),),
            tests=(TestSpec("test", "Test", ("req",), ("api",)),),
            stages=(
                StageSpec("first", "emit", "changed", ("req",), ("api",), ("test",)),
                StageSpec("second", "emit", "two", ("req",), ("api",), ("test",)),
            ),
        )
        from racer.runner import Runner
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fingerprint_base = JsonCheckpointStore(root / "fingerprint", job)
            fingerprint_base.commit(ready, expected_revision=None)
            mismatch_path = root / "fingerprint" / "checkpoint.json"
            mismatch_before = mismatch_path.read_bytes()
            mismatch_probe = ProbeStore(JsonCheckpointStore(root / "fingerprint", changed_job))
            mismatch_clock = SequenceClock()
            with self.assertRaises(CheckpointFingerprintMismatchError):
                Runner(changed_job, mismatch_probe, Executor(), mismatch_clock).resume()
            self.assertEqual(mismatch_path.read_bytes(), mismatch_before)
            self.assertEqual((mismatch_clock.calls, calls), (0, []))
            self.assertEqual(
                (mismatch_probe.exists_calls, mismatch_probe.load_calls, mismatch_probe.commit_calls),
                (0, 1, 0),
            )

            corrupt_base = JsonCheckpointStore(root / "corrupt", job)
            corrupt_base.commit(ready, expected_revision=None)
            corrupt_path = root / "corrupt" / "checkpoint.json"
            corrupt_path.write_bytes(b"{")
            corrupt_before = corrupt_path.read_bytes()
            corrupt_probe = ProbeStore(corrupt_base)
            corrupt_clock = SequenceClock()
            with self.assertRaises(CheckpointFormatError):
                Runner(job, corrupt_probe, Executor(), corrupt_clock).resume()
            self.assertEqual(corrupt_path.read_bytes(), corrupt_before)
            self.assertEqual((corrupt_clock.calls, calls), (0, []))
            self.assertEqual(
                (corrupt_probe.exists_calls, corrupt_probe.load_calls, corrupt_probe.commit_calls),
                (0, 1, 0),
            )

    def test_interrupt_result_and_durable_checkpoint_status_are_explicit(self) -> None:
        calls: list[str] = []

        class Executor:
            def execute(self, spec, context):
                calls.append(spec.stage_id)
                return StageResult(spec.stage_id, 0, None)

        with TemporaryDirectory() as directory:
            path = Path(directory)
            job = ordered_job()
            result = self.make_runner(job, Executor(), path).run(interrupt_before="first")
            checkpoint = JsonCheckpointStore(path, job).load()

        self.assertEqual(calls, [])
        self.assertEqual(
            result,
            RunResult(
                "ordered_job", "ordered_job:0", 0,
                RunStatus.INTERRUPTED, RunStatus.RUNNING, (), None,
                ExitCode.INTERRUPTED, False, 2,
            ),
        )
        self.assertEqual(checkpoint.run_status, RunStatus.RUNNING)
        self.assertEqual(
            [(state.status, state.attempts) for state in checkpoint.stages],
            [(StageStatus.RUNNING, 1), (StageStatus.PENDING, 0)],
        )
        self.assertEqual(
            [event.kind for event in checkpoint.events],
            [
                TraceEventKind.CHECKPOINT_CREATED,
                TraceEventKind.RUN_STARTED,
                TraceEventKind.STAGE_STARTED,
            ],
        )

    def test_resume_interrupt_result_is_explicit_and_uses_two_atomic_commits(self) -> None:
        job = ordered_job()
        events = (
            TraceEvent("ordered_job:0:000000", 0, 0, TraceEventKind.CHECKPOINT_CREATED, "2026-09-02T12:00:00.000000Z", RunStatus.READY, None, None, None, None, None, None, None, None),
            TraceEvent("ordered_job:0:000001", 0, 1, TraceEventKind.RUN_STARTED, "2026-09-02T12:00:00.000000Z", RunStatus.RUNNING, None, None, None, None, None, None, None, None),
            TraceEvent("ordered_job:0:000002", 0, 2, TraceEventKind.STAGE_STARTED, "2026-09-02T12:00:00.000000Z", RunStatus.RUNNING, "first", StageStatus.RUNNING, 1, None, None, None, None, None),
        )
        checkpoint = Checkpoint(
            1, 2, 0, "ordered_job:0", "ordered_job", job_fingerprint(job),
            RunStatus.RUNNING,
            (
                StageState("first", StageStatus.RUNNING, 1, None, None),
                StageState("second", StageStatus.PENDING, 0, None, None),
            ),
            build_traceability_snapshot(job), events,
        )
        calls: list[str] = []

        class Executor:
            def execute(self, spec, context):
                calls.append(spec.stage_id)
                return StageResult(spec.stage_id, 0, None)

        from racer.runner import Runner
        with TemporaryDirectory() as directory:
            path = Path(directory)
            base = JsonCheckpointStore(path, job)
            commit_history(
                base,
                Checkpoint(
                    1, 0, 0, "ordered_job:0", "ordered_job", job_fingerprint(job),
                    RunStatus.READY,
                    (
                        StageState("first", StageStatus.PENDING, 0, None, None),
                        StageState("second", StageStatus.PENDING, 0, None, None),
                    ),
                    build_traceability_snapshot(job), events[:1],
                ),
                Checkpoint(
                    1, 1, 0, "ordered_job:0", "ordered_job", job_fingerprint(job),
                    RunStatus.RUNNING,
                    (
                        StageState("first", StageStatus.PENDING, 0, None, None),
                        StageState("second", StageStatus.PENDING, 0, None, None),
                    ),
                    build_traceability_snapshot(job), events[:2],
                ),
                checkpoint,
            )
            store = RecordingStore(base)
            clock = SequenceClock()
            result = Runner(job, store, Executor(), clock).resume(
                interrupt_before="first"
            )
            durable = base.load()

        self.assertEqual(calls, [])
        self.assertEqual(clock.calls, 2)
        self.assertEqual(
            result,
            RunResult(
                "ordered_job", "ordered_job:0", 0,
                RunStatus.INTERRUPTED, RunStatus.RUNNING, (), None,
                ExitCode.INTERRUPTED, True, 4,
            ),
        )
        self.assertEqual(
            [(candidate.revision, expected_revision) for candidate, expected_revision in store.commits],
            [(3, 2), (4, 3)],
        )
        self.assertEqual(
            [event.kind for event in store.commits[0][0].events[-2:]],
            [TraceEventKind.STAGE_INTERRUPTED, TraceEventKind.RUN_INTERRUPTED],
        )
        self.assertEqual(
            [event.kind for event in store.commits[1][0].events[-2:]],
            [TraceEventKind.RUN_RESUMED, TraceEventKind.STAGE_STARTED],
        )
        self.assertEqual(
            [event.recorded_at for event in store.commits[0][0].events[-2:]],
            ["2026-09-02T12:00:00.000001Z"] * 2,
        )
        self.assertEqual(
            [event.recorded_at for event in store.commits[1][0].events[-2:]],
            ["2026-09-02T12:00:00.000002Z"] * 2,
        )
        self.assertEqual(
            [(state.status, state.attempts) for state in durable.stages],
            [(StageStatus.RUNNING, 2), (StageStatus.PENDING, 0)],
        )

    def test_resume_stage_failure_result_is_marked_resumed(self) -> None:
        job = ordered_job()
        events = (
            TraceEvent("ordered_job:0:000000", 0, 0, TraceEventKind.CHECKPOINT_CREATED, "2026-09-02T12:00:00.000000Z", RunStatus.READY, None, None, None, None, None, None, None, None),
            TraceEvent("ordered_job:0:000001", 0, 1, TraceEventKind.RUN_STARTED, "2026-09-02T12:00:00.000000Z", RunStatus.RUNNING, None, None, None, None, None, None, None, None),
            TraceEvent("ordered_job:0:000002", 0, 2, TraceEventKind.STAGE_STARTED, "2026-09-02T12:00:00.000000Z", RunStatus.RUNNING, "first", StageStatus.RUNNING, 1, None, None, None, None, None),
            TraceEvent("ordered_job:0:000003", 0, 3, TraceEventKind.STAGE_INTERRUPTED, "2026-09-02T12:00:00.000000Z", RunStatus.INTERRUPTED, "first", StageStatus.INTERRUPTED, 1, None, None, None, None, None),
            TraceEvent("ordered_job:0:000004", 0, 4, TraceEventKind.RUN_INTERRUPTED, "2026-09-02T12:00:00.000000Z", RunStatus.INTERRUPTED, None, None, None, None, None, None, None, None),
        )
        checkpoint = Checkpoint(
            1, 3, 0, "ordered_job:0", "ordered_job", job_fingerprint(job),
            RunStatus.INTERRUPTED,
            (
                StageState("first", StageStatus.INTERRUPTED, 1, None, None),
                StageState("second", StageStatus.PENDING, 0, None, None),
            ),
            build_traceability_snapshot(job), events,
        )

        class Executor:
            def execute(self, spec, context):
                return StageResult("first", 9, {"reason": "resume"})

        from racer.runner import Runner
        with TemporaryDirectory() as directory:
            path = Path(directory)
            base = JsonCheckpointStore(path, job)
            commit_history(
                base,
                Checkpoint(1, 0, 0, "ordered_job:0", "ordered_job", job_fingerprint(job), RunStatus.READY, (StageState("first", StageStatus.PENDING, 0, None, None), StageState("second", StageStatus.PENDING, 0, None, None)), build_traceability_snapshot(job), events[:1]),
                Checkpoint(1, 1, 0, "ordered_job:0", "ordered_job", job_fingerprint(job), RunStatus.RUNNING, (StageState("first", StageStatus.PENDING, 0, None, None), StageState("second", StageStatus.PENDING, 0, None, None)), build_traceability_snapshot(job), events[:2]),
                Checkpoint(1, 2, 0, "ordered_job:0", "ordered_job", job_fingerprint(job), RunStatus.RUNNING, (StageState("first", StageStatus.RUNNING, 1, None, None), StageState("second", StageStatus.PENDING, 0, None, None)), build_traceability_snapshot(job), events[:3]),
                checkpoint,
            )
            result = Runner(job, base, Executor(), FixedClock(INSTANT)).resume()
            durable = base.load()

        self.assertEqual(
            result,
            RunResult(
                "ordered_job", "ordered_job:0", 0,
                RunStatus.FAILED, RunStatus.FAILED, (), "first",
                ExitCode.STAGE_FAILED, True, 5,
            ),
        )
        self.assertEqual(
            [event.kind for event in durable.events[-4:]],
            [
                TraceEventKind.RUN_RESUMED,
                TraceEventKind.STAGE_STARTED,
                TraceEventKind.STAGE_FAILED,
                TraceEventKind.RUN_FAILED,
            ],
        )

    def test_resume_recovers_single_running_stage(self) -> None:
        calls: list[tuple[str, int]] = []

        class Executor:
            def execute(self, spec, context):
                calls.append((spec.stage_id, context.attempt))
                return StageResult(spec.stage_id, 0, None)

        with TemporaryDirectory() as directory:
            path = Path(directory)
            job = ordered_job()
            runner = self.make_runner(job, Executor(), path)
            runner.run(interrupt_before="first")
            result = runner.resume()
            checkpoint = JsonCheckpointStore(path, job).load()

        self.assertEqual(calls, [("first", 2), ("second", 1)])
        self.assertEqual(result.resumed, True)
        self.assertEqual(result.exit_code, ExitCode.OK)
        self.assertEqual(checkpoint.revision, 8)
        self.assertEqual(
            [event.kind for event in checkpoint.events],
            [
                TraceEventKind.CHECKPOINT_CREATED,
                TraceEventKind.RUN_STARTED,
                TraceEventKind.STAGE_STARTED,
                TraceEventKind.STAGE_INTERRUPTED,
                TraceEventKind.RUN_INTERRUPTED,
                TraceEventKind.RUN_RESUMED,
                TraceEventKind.STAGE_STARTED,
                TraceEventKind.STAGE_SUCCEEDED,
                TraceEventKind.STAGE_STARTED,
                TraceEventKind.STAGE_SUCCEEDED,
                TraceEventKind.RUN_SUCCEEDED,
            ],
        )
        self.assertEqual(
            [event.recorded_at for event in checkpoint.events[3:5]],
            ["2026-09-02T12:00:00.000000Z"] * 2,
        )
        self.assertEqual(
            [event.recorded_at for event in checkpoint.events[5:7]],
            ["2026-09-02T12:00:00.000000Z"] * 2,
        )

    def test_resume_continues_running_run_without_active_stage(self) -> None:
        job = ordered_job()
        checkpoint = Checkpoint(
            1, 1, 0, "ordered_job:0", "ordered_job", job_fingerprint(job),
            RunStatus.RUNNING,
            (
                StageState("first", StageStatus.PENDING, 0, None, None),
                StageState("second", StageStatus.PENDING, 0, None, None),
            ),
            build_traceability_snapshot(job),
            (
                TraceEvent("ordered_job:0:000000", 0, 0, TraceEventKind.CHECKPOINT_CREATED, "2026-09-02T12:00:00.000000Z", RunStatus.READY, None, None, None, None, None, None, None, None),
                TraceEvent("ordered_job:0:000001", 0, 1, TraceEventKind.RUN_STARTED, "2026-09-02T12:00:00.000000Z", RunStatus.RUNNING, None, None, None, None, None, None, None, None),
            ),
        )
        calls: list[str] = []

        class Executor:
            def execute(self, spec, context):
                calls.append(spec.stage_id)
                return StageResult(spec.stage_id, 0, None)

        with TemporaryDirectory() as directory:
            path = Path(directory)
            base = JsonCheckpointStore(path, job)
            commit_history(
                base,
                Checkpoint(1, 0, 0, "ordered_job:0", "ordered_job", job_fingerprint(job), RunStatus.READY, (StageState("first", StageStatus.PENDING, 0, None, None), StageState("second", StageStatus.PENDING, 0, None, None)), build_traceability_snapshot(job), checkpoint.events[:1]),
                checkpoint,
            )
            result = self.make_runner(job, Executor(), path).resume()
            checkpoint = JsonCheckpointStore(path, job).load()

        self.assertEqual(calls, ["first", "second"])
        self.assertTrue(result.resumed)
        self.assertNotIn(TraceEventKind.RUN_RESUMED, [event.kind for event in checkpoint.events])

    def test_resume_continues_from_persisted_interrupted_state(self) -> None:
        job = ordered_job()
        events = (
            TraceEvent("ordered_job:0:000000", 0, 0, TraceEventKind.CHECKPOINT_CREATED, "2026-09-02T12:00:00.000000Z", RunStatus.READY, None, None, None, None, None, None, None, None),
            TraceEvent("ordered_job:0:000001", 0, 1, TraceEventKind.RUN_STARTED, "2026-09-02T12:00:00.000000Z", RunStatus.RUNNING, None, None, None, None, None, None, None, None),
            TraceEvent("ordered_job:0:000002", 0, 2, TraceEventKind.STAGE_STARTED, "2026-09-02T12:00:00.000000Z", RunStatus.RUNNING, "first", StageStatus.RUNNING, 1, None, None, None, None, None),
            TraceEvent("ordered_job:0:000003", 0, 3, TraceEventKind.STAGE_INTERRUPTED, "2026-09-02T12:00:00.000000Z", RunStatus.INTERRUPTED, "first", StageStatus.INTERRUPTED, 1, None, None, None, None, None),
            TraceEvent("ordered_job:0:000004", 0, 4, TraceEventKind.RUN_INTERRUPTED, "2026-09-02T12:00:00.000000Z", RunStatus.INTERRUPTED, None, None, None, None, None, None, None, None),
        )
        checkpoint = Checkpoint(
            1, 3, 0, "ordered_job:0", "ordered_job", job_fingerprint(job),
            RunStatus.INTERRUPTED,
            (
                StageState("first", StageStatus.INTERRUPTED, 1, None, None),
                StageState("second", StageStatus.PENDING, 0, None, None),
            ),
            build_traceability_snapshot(job), events,
        )
        calls: list[tuple[str, int]] = []

        class Executor:
            def execute(self, spec, context):
                calls.append((spec.stage_id, context.attempt))
                return StageResult(spec.stage_id, 0, None)

        with TemporaryDirectory() as directory:
            path = Path(directory)
            base = JsonCheckpointStore(path, job)
            commit_history(
                base,
                Checkpoint(1, 0, 0, "ordered_job:0", "ordered_job", job_fingerprint(job), RunStatus.READY, (StageState("first", StageStatus.PENDING, 0, None, None), StageState("second", StageStatus.PENDING, 0, None, None)), build_traceability_snapshot(job), events[:1]),
                Checkpoint(1, 1, 0, "ordered_job:0", "ordered_job", job_fingerprint(job), RunStatus.RUNNING, (StageState("first", StageStatus.PENDING, 0, None, None), StageState("second", StageStatus.PENDING, 0, None, None)), build_traceability_snapshot(job), events[:2]),
                Checkpoint(1, 2, 0, "ordered_job:0", "ordered_job", job_fingerprint(job), RunStatus.RUNNING, (StageState("first", StageStatus.RUNNING, 1, None, None), StageState("second", StageStatus.PENDING, 0, None, None)), build_traceability_snapshot(job), events[:3]),
                checkpoint,
            )
            result = self.make_runner(job, Executor(), path).resume()
            checkpoint = JsonCheckpointStore(path, job).load()

        self.assertEqual(calls, [("first", 2), ("second", 1)])
        self.assertTrue(result.resumed)
        self.assertEqual(
            [event.kind for event in checkpoint.events].count(TraceEventKind.STAGE_INTERRUPTED),
            1,
        )
        self.assertEqual(
            [event.kind for event in checkpoint.events].count(TraceEventKind.RUN_INTERRUPTED),
            1,
        )

    def test_resume_finalizes_all_succeeded_checkpoint(self) -> None:
        job = ordered_job()
        checkpoint = Checkpoint(
            1, 5, 0, "ordered_job:0", "ordered_job", job_fingerprint(job),
            RunStatus.RUNNING,
            (
                StageState("first", StageStatus.SUCCEEDED, 1, 0, None),
                StageState("second", StageStatus.SUCCEEDED, 1, 0, None),
            ),
            build_traceability_snapshot(job),
            (
                TraceEvent("ordered_job:0:000000", 0, 0, TraceEventKind.CHECKPOINT_CREATED, "2026-09-02T12:00:00.000000Z", RunStatus.READY, None, None, None, None, None, None, None, None),
                TraceEvent("ordered_job:0:000001", 0, 1, TraceEventKind.RUN_STARTED, "2026-09-02T12:00:00.000000Z", RunStatus.RUNNING, None, None, None, None, None, None, None, None),
                TraceEvent("ordered_job:0:000002", 0, 2, TraceEventKind.STAGE_STARTED, "2026-09-02T12:00:00.000000Z", RunStatus.RUNNING, "first", StageStatus.RUNNING, 1, None, None, None, None, None),
                TraceEvent("ordered_job:0:000003", 0, 3, TraceEventKind.STAGE_SUCCEEDED, "2026-09-02T12:00:00.000000Z", RunStatus.RUNNING, "first", StageStatus.SUCCEEDED, 1, 0, None, None, None, None),
                TraceEvent("ordered_job:0:000004", 0, 4, TraceEventKind.STAGE_STARTED, "2026-09-02T12:00:00.000000Z", RunStatus.RUNNING, "second", StageStatus.RUNNING, 1, None, None, None, None, None),
                TraceEvent("ordered_job:0:000005", 0, 5, TraceEventKind.STAGE_SUCCEEDED, "2026-09-02T12:00:00.000000Z", RunStatus.RUNNING, "second", StageStatus.SUCCEEDED, 1, 0, None, None, None, None),
            ),
        )
        with TemporaryDirectory() as directory:
            path = Path(directory)
            base = JsonCheckpointStore(path, job)
            commit_history(
                base,
                Checkpoint(1, 0, 0, "ordered_job:0", "ordered_job", job_fingerprint(job), RunStatus.READY, (StageState("first", StageStatus.PENDING, 0, None, None), StageState("second", StageStatus.PENDING, 0, None, None)), build_traceability_snapshot(job), checkpoint.events[:1]),
                Checkpoint(1, 1, 0, "ordered_job:0", "ordered_job", job_fingerprint(job), RunStatus.RUNNING, (StageState("first", StageStatus.PENDING, 0, None, None), StageState("second", StageStatus.PENDING, 0, None, None)), build_traceability_snapshot(job), checkpoint.events[:2]),
                Checkpoint(1, 2, 0, "ordered_job:0", "ordered_job", job_fingerprint(job), RunStatus.RUNNING, (StageState("first", StageStatus.RUNNING, 1, None, None), StageState("second", StageStatus.PENDING, 0, None, None)), build_traceability_snapshot(job), checkpoint.events[:3]),
                Checkpoint(1, 3, 0, "ordered_job:0", "ordered_job", job_fingerprint(job), RunStatus.RUNNING, (StageState("first", StageStatus.SUCCEEDED, 1, 0, None), StageState("second", StageStatus.PENDING, 0, None, None)), build_traceability_snapshot(job), checkpoint.events[:4]),
                Checkpoint(1, 4, 0, "ordered_job:0", "ordered_job", job_fingerprint(job), RunStatus.RUNNING, (StageState("first", StageStatus.SUCCEEDED, 1, 0, None), StageState("second", StageStatus.RUNNING, 1, None, None)), build_traceability_snapshot(job), checkpoint.events[:5]),
                checkpoint,
            )
            result = self.make_runner(job, object(), path).resume()
            durable = JsonCheckpointStore(path, job).load()

        self.assertTrue(result.resumed)
        self.assertEqual(result.exit_code, ExitCode.OK)
        self.assertEqual(durable.revision, 6)
        self.assertEqual(durable.events[-1].kind, TraceEventKind.RUN_SUCCEEDED)

    def test_resume_rejects_ready_checkpoint_without_write(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory)
            job = ordered_job()
            runner = self.make_runner(job, object(), path)
            runner._create_checkpoint()
            before = (path / "checkpoint.json").read_bytes()
            with self.assertRaisesRegex(
                InvalidStateError,
                "resume requires RUNNING or INTERRUPTED; found READY",
            ):
                runner.resume()
            self.assertEqual((path / "checkpoint.json").read_bytes(), before)

    def test_resume_rejects_failed_and_succeeded_without_write(self) -> None:
        class Executor:
            def execute(self, spec, context):
                return StageResult(spec.stage_id, 0, None)

        with TemporaryDirectory() as directory:
            path = Path(directory)
            job = ordered_job()
            runner = self.make_runner(job, Executor(), path)
            runner.run()
            before = (path / "checkpoint.json").read_bytes()
            with self.assertRaises(InvalidStateError):
                runner.resume()
            self.assertEqual((path / "checkpoint.json").read_bytes(), before)

        with TemporaryDirectory() as directory:
            path = Path(directory)
            job = load_job(FIXTURES / "failing_job.json")
            from racer.stages import FixtureStageExecutor

            runner = self.make_runner(job, FixtureStageExecutor(), path)
            runner.run()
            before = (path / "checkpoint.json").read_bytes()
            with self.assertRaises(InvalidStateError):
                runner.resume()
            self.assertEqual((path / "checkpoint.json").read_bytes(), before)

    def test_resume_rejects_multiple_running_stages_without_write(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory)
            job = ordered_job()
            runner = self.make_runner(job, object(), path)
            runner._create_checkpoint()
            content = json.loads((path / "checkpoint.json").read_bytes())
            content["run_status"] = "RUNNING"
            for stage in content["stages"]:
                stage["status"] = "RUNNING"
                stage["attempts"] = 1
            (path / "checkpoint.json").write_text(
                json.dumps(content), encoding="utf-8"
            )
            before = (path / "checkpoint.json").read_bytes()
            with self.assertRaises(CheckpointFormatError):
                runner.resume()
            self.assertEqual((path / "checkpoint.json").read_bytes(), before)

    def test_run_rejects_existing_non_ready_checkpoint_without_write(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory)
            job = ordered_job()
            runner = self.make_runner(job, object(), path)
            runner.run(interrupt_before="first")
            before = (path / "checkpoint.json").read_bytes()
            with self.assertRaises(InvalidStateError):
                runner.run()
            self.assertEqual((path / "checkpoint.json").read_bytes(), before)

    def test_reset_preserves_history_and_increments_generation_once(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory)
            job = ordered_job()
            runner = self.make_runner(job, object(), path)
            runner.run(interrupt_before="first")
            before = JsonCheckpointStore(path, job).load()
            result = runner.reset()
            checkpoint = JsonCheckpointStore(path, job).load()

        self.assertEqual(result.generation, 1)
        self.assertEqual(result.run_id, "ordered_job:1")
        self.assertEqual(checkpoint.revision, before.revision + 1)
        self.assertEqual(checkpoint.events[:-1], before.events)
        reset = checkpoint.events[-1]
        self.assertEqual(
            (reset.kind, reset.generation, reset.sequence, reset.event_id),
            (TraceEventKind.RESET, 1, 0, "ordered_job:1:000000"),
        )
        self.assertEqual(
            (reset.previous_generation, reset.previous_run_status, reset.stage_id),
            (0, RunStatus.RUNNING, None),
        )
        self.assertEqual(
            [(state.status, state.attempts, state.exit_code, state.output) for state in checkpoint.stages],
            [(StageStatus.PENDING, 0, None, None)] * 2,
        )

    def test_run_after_reset_starts_reset_generation(self) -> None:
        class Executor:
            def execute(self, spec, context):
                return StageResult(spec.stage_id, 0, None)

        with TemporaryDirectory() as directory:
            path = Path(directory)
            job = ordered_job()
            runner = self.make_runner(job, Executor(), path)
            runner.run(interrupt_before="first")
            runner.reset()
            result = runner.run()
            checkpoint = JsonCheckpointStore(path, job).load()

        self.assertEqual((result.generation, result.run_id), (1, "ordered_job:1"))
        self.assertEqual(checkpoint.generation, 1)
        self.assertEqual(
            [event.kind for event in checkpoint.events if event.generation == 1],
            [
                TraceEventKind.RESET,
                TraceEventKind.RUN_STARTED,
                TraceEventKind.STAGE_STARTED,
                TraceEventKind.STAGE_SUCCEEDED,
                TraceEventKind.STAGE_STARTED,
                TraceEventKind.STAGE_SUCCEEDED,
                TraceEventKind.RUN_SUCCEEDED,
            ],
        )

    def test_checkpoint_creation_crash_can_continue_with_run(self) -> None:
        job = ordered_job()

        class Executor:
            def execute(self, spec, context):
                return StageResult(spec.stage_id, 0, None)

        with TemporaryDirectory() as directory:
            path = Path(directory)
            runner = self.make_runner(job, Executor(), path)
            runner._create_checkpoint()
            result = runner.run()

        self.assertEqual((result.generation, result.exit_code), (0, ExitCode.OK))

    def test_unknown_interrupt_stage_creates_no_state(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "missing"
            runner = self.make_runner(ordered_job(), object(), path)
            with self.assertRaises(UnknownInterruptStageError):
                runner.run(interrupt_before="unknown")
            self.assertFalse(path.exists())

    def test_stages_and_runner_reexport_exact_frozen_contract_types(self) -> None:
        from racer.stages import StageContext as ExportedContext
        from racer.stages import StageResult as ExportedResult

        self.assertIs(ExportedContext, StageContext)
        self.assertIs(ExportedResult, StageResult)
        self.assertTrue(ExportedContext.__dataclass_params__.frozen)
        self.assertTrue(ExportedResult.__dataclass_params__.frozen)
        self.assertTrue(RunResult.__dataclass_params__.frozen)
        self.assertEqual(tuple(field.name for field in fields(ExportedContext)), ("job_id", "run_id", "generation", "stage_id", "attempt"))
        self.assertEqual(tuple(field.name for field in fields(ExportedResult)), ("stage_id", "exit_code", "output"))
        self.assertEqual(tuple(field.name for field in fields(RunResult)), ("job_id", "run_id", "generation", "status", "durable_status", "completed_stage_ids", "failed_stage_id", "exit_code", "resumed", "checkpoint_revision"))
        with self.assertRaises(FrozenInstanceError):
            ExportedContext("job", "job:0", 0, "stage", 1).attempt = 2

    def test_stage_executor_signatures_and_fixture_contract_are_exact(self) -> None:
        from racer.stages import FixtureStageExecutor, StageExecutor

        self.assertEqual(tuple(inspect.signature(StageExecutor.execute).parameters), ("self", "spec", "context"))
        self.assertEqual(tuple(inspect.signature(FixtureStageExecutor.execute).parameters), ("self", "spec", "context"))
        executor = FixtureStageExecutor()
        context = StageContext("job", "job:0", 0, "stage", 1)
        emit = StageSpec("stage", "emit", {"value": 1}, ("req",), ("api",), ("test",))
        fail = StageSpec("stage", "fail", {"reason": "x"}, ("req",), ("api",), ("test",))
        self.assertEqual(executor.execute(emit, context), StageResult("stage", 0, {"emitted": {"value": 1}}))
        self.assertEqual(executor.execute(fail, context), StageResult("stage", 1, {"error": {"reason": "x"}}))

    def test_fixture_executor_performs_no_file_or_checkpoint_write(self) -> None:
        from racer.stages import FixtureStageExecutor

        with TemporaryDirectory() as directory:
            path = Path(directory)
            before = tuple(path.iterdir())
            FixtureStageExecutor().execute(StageSpec("stage", "emit", None, ("req",), ("api",), ("test",)), StageContext("job", "job:0", 0, "stage", 1))
            self.assertEqual(tuple(path.iterdir()), before)

    def test_fresh_run_persists_generation_zero_context_and_success_result(self) -> None:
        job = ordered_job()
        contexts: list[StageContext] = []

        class Executor:
            def execute(self, spec, context):
                contexts.append(context)
                return StageResult(spec.stage_id, 0, {"done": spec.stage_id})

        with TemporaryDirectory() as directory:
            path = Path(directory)
            result = self.make_runner(job, Executor(), path).run()
            checkpoint = JsonCheckpointStore(path, job).load()

        self.assertEqual([(context.run_id, context.generation, context.stage_id, context.attempt) for context in contexts], [("ordered_job:0", 0, "first", 1), ("ordered_job:0", 0, "second", 1)])
        self.assertEqual(
            result,
            RunResult(
                "ordered_job",
                "ordered_job:0",
                0,
                RunStatus.SUCCEEDED,
                RunStatus.SUCCEEDED,
                ("first", "second"),
                None,
                ExitCode.OK,
                False,
                6,
            ),
        )
        self.assertEqual(checkpoint.generation, 0)
        self.assertEqual(checkpoint.revision, 6)
        self.assertEqual([state.output for state in checkpoint.stages], [{"done": "first"}, {"done": "second"}])
        self.assertEqual([event.kind for event in checkpoint.events], [TraceEventKind.CHECKPOINT_CREATED, TraceEventKind.RUN_STARTED, TraceEventKind.STAGE_STARTED, TraceEventKind.STAGE_SUCCEEDED, TraceEventKind.STAGE_STARTED, TraceEventKind.STAGE_SUCCEEDED, TraceEventKind.RUN_SUCCEEDED])

    def test_executor_observes_stage_started_checkpoint_with_attempt_one(self) -> None:
        job = ordered_job()
        observed: list[tuple[StageStatus, int, int]] = []
        with TemporaryDirectory() as directory:
            path = Path(directory)
            store = JsonCheckpointStore(path, job)

            class Executor:
                def execute(self, spec, context):
                    durable = store.load()
                    state = next(state for state in durable.stages if state.stage_id == spec.stage_id)
                    observed.append((state.status, state.attempts, context.attempt))
                    return StageResult(spec.stage_id, 0, None)

            result = self.make_runner(job, Executor(), path).run()

        self.assertEqual(result.exit_code, ExitCode.OK)
        self.assertEqual(observed, [(StageStatus.RUNNING, 1, 1), (StageStatus.RUNNING, 1, 1)])

    def test_event_ids_sequences_revisions_and_clock_calls_follow_commit_table(self) -> None:
        job = ordered_job()
        calls = 0

        class CountingClock:
            def now_utc(self):
                nonlocal calls
                calls += 1
                return INSTANT

        class Executor:
            def execute(self, spec, context):
                return StageResult(spec.stage_id, 0, None)

        from racer.runner import Runner
        with TemporaryDirectory() as directory:
            path = Path(directory)
            result = Runner(job, JsonCheckpointStore(path, job), Executor(), CountingClock()).run()
            checkpoint = JsonCheckpointStore(path, job).load()

        self.assertEqual(calls, 7)
        self.assertEqual(result.checkpoint_revision, 6)
        self.assertEqual([event.sequence for event in checkpoint.events], list(range(7)))
        self.assertEqual([event.event_id for event in checkpoint.events], [f"ordered_job:0:{sequence:06d}" for sequence in range(7)])
        self.assertEqual([event.recorded_at for event in checkpoint.events], ["2026-09-02T12:00:00.000000Z"] * 7)

    def test_runner_signatures_are_locked_to_lifecycle_api(self) -> None:
        from racer.runner import Runner

        self.assertEqual(tuple(inspect.signature(Runner).parameters), ("job", "store", "executor", "clock"))
        self.assertEqual(
            tuple(inspect.signature(Runner.run).parameters),
            ("self", "interrupt_before"),
        )
        self.assertEqual(
            tuple(inspect.signature(Runner.resume).parameters),
            ("self", "interrupt_before"),
        )
        self.assertEqual(tuple(inspect.signature(Runner.reset).parameters), ("self",))

    def test_runner_preserves_valid_executor_output_without_mutation(self) -> None:
        job = ordered_job()
        output = {"items": [1, {"text": "unchanged"}]}

        class Executor:
            def execute(self, spec, context):
                return StageResult(spec.stage_id, 0, output)

        with TemporaryDirectory() as directory:
            path = Path(directory)
            self.make_runner(job, Executor(), path).run()
            checkpoint = JsonCheckpointStore(path, job).load()

        self.assertEqual(output, {"items": [1, {"text": "unchanged"}]})
        self.assertEqual(checkpoint.stages[0].output, output)

    def test_explicit_failure_persists_atomic_failure_and_skips_later_stage(self) -> None:
        job = load_job(FIXTURES / "failing_job.json")
        from racer.stages import FixtureStageExecutor

        calls: list[str] = []

        class RecordingExecutor:
            def execute(self, spec, context):
                calls.append(spec.stage_id)
                return FixtureStageExecutor().execute(spec, context)

        with TemporaryDirectory() as directory:
            path = Path(directory)
            inner = JsonCheckpointStore(path, job)
            store = RecordingStore(inner)
            from racer.runner import Runner
            result = Runner(job, store, RecordingExecutor(), FixedClock(INSTANT)).run()
            checkpoint = inner.load()

        self.assertEqual(calls, ["first", "second"])
        self.assertEqual(checkpoint.revision, 5)
        self.assertEqual([state.status for state in checkpoint.stages], [StageStatus.SUCCEEDED, StageStatus.FAILED, StageStatus.PENDING])
        self.assertEqual(checkpoint.stages[1].exit_code, 1)
        self.assertEqual(checkpoint.stages[1].output, {"error": {"reason": "deterministic"}})
        self.assertEqual(
            checkpoint.stages[2],
            checkpoint.stages[2].__class__("third", StageStatus.PENDING, 0, None, None),
        )
        self.assert_exact_failure(
            checkpoint,
            result,
            store,
            expected_result=RunResult(
                "failing_job",
                "failing_job:0",
                0,
                RunStatus.FAILED,
                RunStatus.FAILED,
                ("first",),
                "second",
                ExitCode.STAGE_FAILED,
                False,
                5,
            ),
            expected_run_id="failing_job:0",
            expected_revision=5,
            failed_stage_id="second",
            stage_exit_code=1,
            result_exit_code=ExitCode.STAGE_FAILED,
            error_type="StageFailedError",
            error_message="stage 'second' failed with executor exit code 1",
        )

    def test_invalid_executor_results_persist_contract_failure(self) -> None:
        cases = (
            (object(), "executor must return StageResult for stage 'first'"),
            (StageResult("other", 0, None), "executor result stage_id does not match stage 'first'"),
            (StageResult("first", True, None), "executor result exit_code must be an integer for stage 'first'"),
            (StageResult("first", -1, None), "executor result exit_code must be between 0 and 255 for stage 'first'"),
            (StageResult("first", 256, None), "executor result exit_code must be between 0 and 255 for stage 'first'"),
            (StageResult("first", 1.0, None), "executor result exit_code must be an integer for stage 'first'"),
            (StageResult("first", ("tuple",), None), "executor result exit_code must be an integer for stage 'first'"),
            (StageResult("first", 0, ("tuple",)), "executor result output is not JSON-compatible for stage 'first': tuple is not an exact JSON-compatible value"),
            (StageResult("first", 0, {1: "key"}), "executor result output is not JSON-compatible for stage 'first': dict keys must be strings for JSON-compatible values"),
            (StageResult("first", 0, object()), "executor result output is not JSON-compatible for stage 'first': object is not an exact JSON-compatible value"),
            (StageResult("first", 0, math.nan), "executor result output is not JSON-compatible for stage 'first': non-finite float is not JSON-compatible"),
        )
        for invalid, message in cases:
            with self.subTest(invalid=invalid):
                class Executor:
                    def execute(self, spec, context):
                        return invalid

                with TemporaryDirectory() as directory:
                    path = Path(directory)
                    job = ordered_job()
                    inner = JsonCheckpointStore(path, job)
                    store = RecordingStore(inner)
                    from racer.runner import Runner
                    result = Runner(job, store, Executor(), FixedClock(INSTANT)).run()
                    checkpoint = inner.load()
                self.assertEqual(checkpoint.stages[0].exit_code, 70)
                self.assertEqual(checkpoint.stages[0].output, {"error_type": "ExecutorContractError", "message": message})
                self.assert_exact_failure(
                    checkpoint,
                    result,
                    store,
                    expected_result=RunResult(
                        "ordered_job",
                        "ordered_job:0",
                        0,
                        RunStatus.FAILED,
                        RunStatus.FAILED,
                        (),
                        "first",
                        ExitCode.INTERNAL_ERROR,
                        False,
                        3,
                    ),
                    expected_run_id="ordered_job:0",
                    expected_revision=3,
                    failed_stage_id="first",
                    stage_exit_code=70,
                    result_exit_code=ExitCode.INTERNAL_ERROR,
                    error_type="ExecutorContractError",
                    error_message=message,
                )

    def test_executor_output_with_lone_surrogate_persists_contract_failure(self) -> None:
        class Executor:
            def execute(self, spec, context):
                return StageResult(spec.stage_id, 0, {"text": "\ud800"})

        with TemporaryDirectory() as directory:
            path = Path(directory)
            job = ordered_job()
            result = self.make_runner(job, Executor(), path).run()
            checkpoint = JsonCheckpointStore(path, job).load()

        self.assertEqual(result.exit_code, ExitCode.INTERNAL_ERROR)
        self.assertEqual(checkpoint.run_status, RunStatus.FAILED)
        self.assertEqual(checkpoint.stages[0].exit_code, 70)
        self.assertEqual(checkpoint.stages[0].output["error_type"], "ExecutorContractError")

    def test_executor_output_with_chinese_and_emoji_succeeds_and_round_trips(self) -> None:
        class Executor:
            def execute(self, spec, context):
                return StageResult(spec.stage_id, 0, {"text": "中文😀"})

        with TemporaryDirectory() as directory:
            path = Path(directory)
            job = ordered_job()
            result = self.make_runner(job, Executor(), path).run()
            checkpoint = JsonCheckpointStore(path, job).load()

        self.assertEqual(result.exit_code, ExitCode.OK)
        self.assertEqual(checkpoint.stages[0].output, {"text": "中文😀"})

    def test_cycle_set_and_infinities_are_contract_failures(self) -> None:
        cyclic: list[object] = []
        cyclic.append(cyclic)
        outputs = ({"values": {1}}, cyclic, math.inf, -math.inf)
        for output in outputs:
            with self.subTest(output=repr(output)):
                class Executor:
                    def execute(self, spec, context):
                        return StageResult(spec.stage_id, 0, output)

                with TemporaryDirectory() as directory:
                    path = Path(directory)
                    result = self.make_runner(ordered_job(), Executor(), path).run()
                    checkpoint = JsonCheckpointStore(path, ordered_job()).load()
                self.assertEqual(result.exit_code, ExitCode.INTERNAL_ERROR)
                self.assertEqual(checkpoint.stages[0].exit_code, 70)

    def test_exit_code_255_is_an_explicit_failure(self) -> None:
        class Executor:
            def execute(self, spec, context):
                return StageResult(spec.stage_id, 255, {"failure": True})

        with TemporaryDirectory() as directory:
            path = Path(directory)
            result = self.make_runner(ordered_job(), Executor(), path).run()
            checkpoint = JsonCheckpointStore(path, ordered_job()).load()

        self.assertEqual(result.exit_code, ExitCode.STAGE_FAILED)
        self.assertEqual(checkpoint.stages[0].exit_code, 255)

    def test_executor_exception_persists_internal_failure(self) -> None:
        class Exploded(Exception):
            pass

        class Executor:
            def execute(self, spec, context):
                raise Exploded("boom")

        with TemporaryDirectory() as directory:
            path = Path(directory)
            job = ordered_job()
            inner = JsonCheckpointStore(path, job)
            store = RecordingStore(inner)
            from racer.runner import Runner
            result = Runner(job, store, Executor(), FixedClock(INSTANT)).run()
            checkpoint = inner.load()

        self.assertEqual(checkpoint.stages[0].output, {"error_type": "Exploded", "message": "boom"})
        self.assert_exact_failure(
            checkpoint,
            result,
            store,
            expected_result=RunResult(
                "ordered_job",
                "ordered_job:0",
                0,
                RunStatus.FAILED,
                RunStatus.FAILED,
                (),
                "first",
                ExitCode.INTERNAL_ERROR,
                False,
                3,
            ),
            expected_run_id="ordered_job:0",
            expected_revision=3,
            failed_stage_id="first",
            stage_exit_code=70,
            result_exit_code=ExitCode.INTERNAL_ERROR,
            error_type="Exploded",
            error_message="boom",
        )

    def test_executor_thrown_stage_failed_error_persists_internal_failure(self) -> None:
        class Executor:
            def execute(self, spec, context):
                raise StageFailedError(spec.stage_id, 9)

        with TemporaryDirectory() as directory:
            path = Path(directory)
            job = ordered_job()
            inner = JsonCheckpointStore(path, job)
            store = RecordingStore(inner)
            from racer.runner import Runner
            result = Runner(job, store, Executor(), FixedClock(INSTANT)).run()
            checkpoint = inner.load()

        self.assertEqual(
            checkpoint.stages[0].output,
            {
                "error_type": "StageFailedError",
                "message": "stage 'first' failed with executor exit code 9",
            },
        )
        self.assert_exact_failure(
            checkpoint,
            result,
            store,
            expected_result=RunResult(
                "ordered_job",
                "ordered_job:0",
                0,
                RunStatus.FAILED,
                RunStatus.FAILED,
                (),
                "first",
                ExitCode.INTERNAL_ERROR,
                False,
                3,
            ),
            expected_run_id="ordered_job:0",
            expected_revision=3,
            failed_stage_id="first",
            stage_exit_code=70,
            result_exit_code=ExitCode.INTERNAL_ERROR,
            error_type="StageFailedError",
            error_message="stage 'first' failed with executor exit code 9",
        )

    def test_executor_thrown_stage_failed_error_commit_failure_preserves_running_checkpoint(self) -> None:
        class Executor:
            def execute(self, spec, context):
                raise StageFailedError(spec.stage_id, 9)

        with TemporaryDirectory() as directory:
            path = Path(directory)
            checkpoint_path = path / "checkpoint.json"
            job = ordered_job()
            store = JsonCheckpointStore(path, job)
            replace_calls = 0
            failure_bytes: bytes | None = None
            real_replace = os.replace

            def fail_fourth_replace(source, destination):
                nonlocal replace_calls, failure_bytes
                replace_calls += 1
                if replace_calls == 4:
                    failure_bytes = checkpoint_path.read_bytes()
                    raise OSError("injected fourth replace failure")
                return real_replace(source, destination)

            from racer.runner import Runner
            result = None
            with patch("racer.checkpoint.os.replace", side_effect=fail_fourth_replace):
                with self.assertRaises(CheckpointCommitError):
                    result = Runner(job, store, Executor(), FixedClock(INSTANT)).run()
            durable = store.load()

            self.assertEqual(replace_calls, 4)
            self.assertEqual(checkpoint_path.read_bytes(), failure_bytes)
            self.assertEqual(tuple(path.iterdir()), (checkpoint_path,))

        self.assertIsNone(result)
        self.assertEqual(durable.revision, 2)
        self.assertEqual(durable.run_status, RunStatus.RUNNING)
        self.assertEqual(
            [(state.status, state.attempts) for state in durable.stages],
            [(StageStatus.RUNNING, 1), (StageStatus.PENDING, 0)],
        )
        self.assertEqual(
            [event.kind for event in durable.events],
            [
                TraceEventKind.CHECKPOINT_CREATED,
                TraceEventKind.RUN_STARTED,
                TraceEventKind.STAGE_STARTED,
            ],
        )

    def test_failure_commit_errors_propagate_without_durable_failure_claim(self) -> None:
        class RuntimeExecutor:
            def execute(self, spec, context):
                raise RuntimeError("boom")

        cases = (
            ("explicit", lambda spec: StageResult(spec.stage_id, 1, {"failure": True})),
            ("contract", lambda spec: StageResult("wrong", 0, None)),
            ("runtime", None),
        )
        for mode, result_factory in cases:
            with self.subTest(mode=mode):
                job = ordered_job()

                if result_factory is None:
                    executor = RuntimeExecutor()
                else:
                    class ResultExecutor:
                        def execute(self, spec, context):
                            return result_factory(spec)

                    executor = ResultExecutor()

                with TemporaryDirectory() as directory:
                    path = Path(directory)
                    inner = JsonCheckpointStore(path, job)
                    store = RecordingStore(
                        inner,
                        lambda checkpoint: checkpoint.run_status is RunStatus.FAILED,
                    )
                    from racer.runner import Runner
                    result = None
                    with self.assertRaises(CheckpointCommitError):
                        result = Runner(job, store, executor, FixedClock(INSTANT)).run()
                    durable = inner.load()

                self.assertIsNone(result)
                self.assertEqual(durable.revision, 2)
                self.assertEqual(durable.run_status, RunStatus.RUNNING)
                self.assertEqual(
                    [(state.status, state.attempts) for state in durable.stages],
                    [(StageStatus.RUNNING, 1), (StageStatus.PENDING, 0)],
                )
                self.assertEqual(
                    [event.kind for event in durable.events],
                    [
                        TraceEventKind.CHECKPOINT_CREATED,
                        TraceEventKind.RUN_STARTED,
                        TraceEventKind.STAGE_STARTED,
                    ],
                )
                failed_candidates = [
                    candidate
                    for candidate, _ in store.commits
                    if candidate.run_status is RunStatus.FAILED
                ]
                self.assertEqual(len(failed_candidates), 1)
                self.assertEqual(
                    [event.kind for event in failed_candidates[0].events[-2:]],
                    [TraceEventKind.STAGE_FAILED, TraceEventKind.RUN_FAILED],
                )

    def test_stage_started_commit_error_leaves_pending_checkpoint_and_skips_executor(self) -> None:
        job = ordered_job()
        calls: list[str] = []

        class Executor:
            def execute(self, spec, context):
                calls.append(spec.stage_id)
                return StageResult(spec.stage_id, 0, None)

        with TemporaryDirectory() as directory:
            path = Path(directory)
            inner = JsonCheckpointStore(path, job)
            store = RecordingStore(
                inner,
                lambda checkpoint: checkpoint.events[-1].kind is TraceEventKind.STAGE_STARTED,
            )
            from racer.runner import Runner
            with self.assertRaises(CheckpointCommitError):
                Runner(job, store, Executor(), FixedClock(INSTANT)).run()
            durable = inner.load()

        self.assertEqual(calls, [])
        self.assertEqual(durable.revision, 1)
        self.assertEqual(durable.run_status, RunStatus.RUNNING)
        self.assertEqual(
            [(state.status, state.attempts) for state in durable.stages],
            [(StageStatus.PENDING, 0), (StageStatus.PENDING, 0)],
        )
        self.assertEqual(
            [event.kind for event in durable.events],
            [TraceEventKind.CHECKPOINT_CREATED, TraceEventKind.RUN_STARTED],
        )

    def test_fixed_clock_makes_checkpoint_bytes_deterministic(self) -> None:
        job = ordered_job()

        class Executor:
            def execute(self, spec, context):
                return StageResult(spec.stage_id, 0, {"stage": spec.stage_id})

        with TemporaryDirectory() as directory:
            first = Path(directory) / "first"
            second = Path(directory) / "second"
            self.make_runner(job, Executor(), first).run()
            self.make_runner(job, Executor(), second).run()
            self.assertEqual((first / "checkpoint.json").read_bytes(), (second / "checkpoint.json").read_bytes())

    def test_package_exports_runner_without_changing_version(self) -> None:
        import racer
        from racer.runner import Runner

        self.assertEqual(racer.__version__, "0.1.0")
        self.assertIs(racer.Runner, Runner)

    def test_runner_and_store_snapshot_caller_job_payload_at_construction(self) -> None:
        payload = {"nested": {"value": "original"}}
        job = JobSpec(
            1,
            "isolated_job",
            (RequirementSpec("req", "Requirement"),),
            (InterfaceSpec("api", "API", ("req",)),),
            (TestSpec("test", "Test", ("req",), ("api",)),),
            (StageSpec("stage", "emit", payload, ("req",), ("api",), ("test",)),),
        )
        seen: list[object] = []

        class Executor:
            def execute(self, spec, context):
                seen.append(spec.payload)
                return StageResult(spec.stage_id, 0, None)

        with TemporaryDirectory() as directory:
            path = Path(directory)
            store = JsonCheckpointStore(path, job)
            runner = Runner(job, store, Executor(), FixedClock(INSTANT))
            expected_fingerprint = job_fingerprint(job)
            payload["nested"]["value"] = "mutated"
            runner.run()
            checkpoint = store.load()
            trace = runner.trace()

        self.assertEqual(seen, [{"nested": {"value": "original"}}])
        self.assertEqual(checkpoint.job_fingerprint, expected_fingerprint)
        self.assertEqual(trace.job_fingerprint, checkpoint.job_fingerprint)

    def test_runner_and_store_revalidate_tampered_job_before_snapshot(self) -> None:
        class RequirementSubclass(RequirementSpec):
            pass

        mutations = (
            lambda job: object.__setattr__(job, "requirements", []),
            lambda job: object.__setattr__(job, "interfaces", []),
            lambda job: object.__setattr__(job, "tests", []),
            lambda job: object.__setattr__(job, "stages", []),
            lambda job: object.__setattr__(job, "requirements", (None,)),
            lambda job: object.__setattr__(
                job,
                "requirements",
                (RequirementSubclass("req", "Requirement"),),
            ),
        )

        for mutate in mutations:
            with self.subTest(mutate=mutate):
                job = ordered_job()
                mutate(job)
                with TemporaryDirectory() as directory:
                    with self.assertRaises(JobValidationError):
                        JsonCheckpointStore(Path(directory), job)
                    with self.assertRaises(JobValidationError):
                        Runner(job, object(), object(), FixedClock(INSTANT))

    def test_executor_receives_fresh_detached_stage_specs_on_reset(self) -> None:
        job = JobSpec(
            1,
            "reset_job",
            (RequirementSpec("req", "Requirement"),),
            (InterfaceSpec("api", "API", ("req",)),),
            (TestSpec("test", "Test", ("req",), ("api",)),),
            (StageSpec("stage", "emit", {"value": "original"}, ("req",), ("api",), ("test",)),),
        )
        seen: list[object] = []

        class Executor:
            def execute(self, spec, context):
                seen.append(dict(spec.payload))
                spec.payload["value"] = "executor mutation"
                return StageResult(spec.stage_id, 0, None)

        with TemporaryDirectory() as directory:
            runner = self.make_runner(job, Executor(), Path(directory))
            runner.run()
            runner.reset()
            runner.run()

        self.assertEqual(seen, [{"value": "original"}, {"value": "original"}])

    def test_executor_output_is_snapshotted_before_success_event_construction(self) -> None:
        job = JobSpec(
            1,
            "output_job",
            (RequirementSpec("req", "Requirement"),),
            (InterfaceSpec("api", "API", ("req",)),),
            (TestSpec("test", "Test", ("req",), ("api",)),),
            (StageSpec("stage", "emit", None, ("req",), ("api",), ("test",)),),
        )
        output = {"nested": {"value": "original"}}

        class MutatingClock:
            def __init__(self):
                self.calls = 0

            def now_utc(self):
                self.calls += 1
                if self.calls == 4:
                    output["nested"]["value"] = "mutated"
                return INSTANT

        class Executor:
            def execute(self, spec, context):
                return StageResult(spec.stage_id, 0, output)

        with TemporaryDirectory() as directory:
            path = Path(directory)
            Runner(job, JsonCheckpointStore(path, job), Executor(), MutatingClock()).run()
            checkpoint = JsonCheckpointStore(path, job).load()

        self.assertEqual(checkpoint.stages[0].output, {"nested": {"value": "original"}})

    def test_executor_output_is_snapshotted_before_explicit_failure_event_construction(self) -> None:
        job = JobSpec(
            1,
            "failure_output_job",
            (RequirementSpec("req", "Requirement"),),
            (InterfaceSpec("api", "API", ("req",)),),
            (TestSpec("test", "Test", ("req",), ("api",)),),
            (StageSpec("stage", "fail", None, ("req",), ("api",), ("test",)),),
        )
        output = {"nested": {"value": "original"}}

        class MutatingClock:
            def __init__(self):
                self.calls = 0

            def now_utc(self):
                self.calls += 1
                if self.calls == 4:
                    output["nested"]["value"] = "mutated"
                return INSTANT

        class Executor:
            def execute(self, spec, context):
                return StageResult(spec.stage_id, 1, output)

        with TemporaryDirectory() as directory:
            path = Path(directory)
            Runner(job, JsonCheckpointStore(path, job), Executor(), MutatingClock()).run()
            checkpoint = JsonCheckpointStore(path, job).load()

        self.assertEqual(checkpoint.stages[0].output, {"nested": {"value": "original"}})

    def test_exception_normalization_is_safe_and_shared_by_failure_events(self) -> None:
        class BrokenString(Exception):
            calls = 0

            def __str__(self):
                type(self).calls += 1
                return "\ud800" + "x" * 5000

        class Executor:
            def execute(self, spec, context):
                raise BrokenString()

        with TemporaryDirectory() as directory:
            path = Path(directory)
            result = self.make_runner(ordered_job(), Executor(), path).run()
            checkpoint = JsonCheckpointStore(path, ordered_job()).load()

        stage_event, run_event = checkpoint.events[-2:]
        self.assertEqual(result.exit_code, ExitCode.INTERNAL_ERROR)
        self.assertEqual(BrokenString.calls, 1)
        self.assertEqual(stage_event.error_type, "BrokenString")
        self.assertEqual(stage_event.error_message, "�" + "x" * 4092 + "...")
        self.assertEqual(
            (run_event.error_type, run_event.error_message),
            (stage_event.error_type, stage_event.error_message),
        )

    def test_base_exceptions_propagate_without_durable_failure(self) -> None:
        for exception_type in (SystemExit, GeneratorExit, KeyboardInterrupt):
            with self.subTest(exception_type=exception_type.__name__):
                class Executor:
                    def execute(self, spec, context):
                        raise exception_type()

                with TemporaryDirectory() as directory:
                    path = Path(directory)
                    runner = self.make_runner(ordered_job(), Executor(), path)
                    with self.assertRaises(exception_type):
                        runner.run()
                    checkpoint = JsonCheckpointStore(path, ordered_job()).load()
                self.assertEqual(checkpoint.run_status, RunStatus.RUNNING)
                self.assertEqual(
                    [event.kind for event in checkpoint.events],
                    [
                        TraceEventKind.CHECKPOINT_CREATED,
                        TraceEventKind.RUN_STARTED,
                        TraceEventKind.STAGE_STARTED,
                    ],
                )

    def test_executor_result_stage_id_requires_exact_string_without_equality(self) -> None:
        class StringSubclass(str):
            pass

        class ExplodingEquality:
            def __eq__(self, other):
                raise AssertionError("custom equality must not run")

        for stage_id in (1, StringSubclass("first"), ExplodingEquality()):
            with self.subTest(stage_id=type(stage_id).__name__):
                job = JobSpec(
                    1,
                    "metadata_job",
                    (RequirementSpec("req", "Requirement"),),
                    (InterfaceSpec("api", "API", ("req",)),),
                    (TestSpec("test", "Test", ("req",), ("api",)),),
                    (StageSpec("first", "emit", None, ("req",), ("api",), ("test",)),),
                )

                class Executor:
                    def execute(self, spec, context):
                        return StageResult(stage_id, 0, None)

                with TemporaryDirectory() as directory:
                    path = Path(directory)
                    result = self.make_runner(job, Executor(), path).run()
                    checkpoint = JsonCheckpointStore(path, job).load()
                self.assertEqual(result.exit_code, ExitCode.INTERNAL_ERROR)
                self.assertEqual(checkpoint.events[-2].error_type, "ExecutorContractError")
                self.assertEqual(
                    checkpoint.events[-2].error_message,
                    "executor result stage_id must be an exact string for stage 'first'",
                )

    def test_snapshot_iteration_runtime_error_is_executor_contract_failure(self) -> None:
        import racer.runner as runner_module

        actual_snapshot = runner_module.snapshot_json_value
        calls = 0

        def snapshot(value, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("dictionary changed size during iteration")
            return actual_snapshot(value, *args, **kwargs)

        class Executor:
            def execute(self, spec, context):
                return StageResult(spec.stage_id, 0, {"value": 1})

        with TemporaryDirectory() as directory:
            path = Path(directory)
            with patch("racer.runner.snapshot_json_value", side_effect=snapshot):
                result = self.make_runner(ordered_job(), Executor(), path).run()
            checkpoint = JsonCheckpointStore(path, ordered_job()).load()

        self.assertEqual(result.exit_code, ExitCode.INTERNAL_ERROR)
        self.assertEqual(checkpoint.events[-2].error_type, "ExecutorContractError")

    def test_executor_output_oversized_integer_and_nested_alias_are_isolated(self) -> None:
        output = {"nested": [{"value": 1}]}

        class Executor:
            def execute(self, spec, context):
                if spec.stage_id == "first":
                    return StageResult(spec.stage_id, 0, output)
                return StageResult(spec.stage_id, 0, 2**63)

        with TemporaryDirectory() as directory:
            path = Path(directory)
            result = self.make_runner(ordered_job(), Executor(), path).run()
            output["nested"][0]["value"] = 2
            checkpoint = JsonCheckpointStore(path, ordered_job()).load()

        self.assertEqual(result.exit_code, ExitCode.INTERNAL_ERROR)
        self.assertEqual(checkpoint.stages[0].output, {"nested": [{"value": 1}]})
        self.assertEqual(checkpoint.events[-2].error_type, "ExecutorContractError")

    def test_exception_normalization_handles_raising_and_nonstring_str(self) -> None:
        class RaisingString(Exception):
            def __str__(self):
                raise RuntimeError("unprintable")

        class NonString(Exception):
            def __str__(self):
                return 3

        for error in (RaisingString(), NonString()):
            with self.subTest(error=type(error).__name__):
                class Executor:
                    def execute(self, spec, context):
                        raise error

                with TemporaryDirectory() as directory:
                    path = Path(directory)
                    self.make_runner(ordered_job(), Executor(), path).run()
                    checkpoint = JsonCheckpointStore(path, ordered_job()).load()
                self.assertEqual(checkpoint.events[-2].error_message, "<unprintable exception>")


if __name__ == "__main__":
    unittest.main()
