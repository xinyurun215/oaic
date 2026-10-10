import json
import os
import unittest
from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from unittest.mock import patch

from racer.checkpoint import Checkpoint, JsonCheckpointStore
from racer.errors import (
    CheckpointCommitError,
    CheckpointFingerprintMismatchError,
    CheckpointFormatError,
    CheckpointNotFoundError,
    RevisionConflictError,
    StageFailedError,
)
from racer.job import job_fingerprint, load_job
from racer.state import RunStatus, StageState, StageStatus, initial_stage_state
from racer.traceability import (
    TraceEvent,
    TraceEventKind,
    TraceabilitySnapshot,
    build_traceability_snapshot,
)


FIXTURES = Path(__file__).parent / "fixtures"


class CheckpointTests(unittest.TestCase):
    def _run_with_output(self, directory, output):
        from datetime import datetime, timezone
        from racer.clock import FixedClock
        from racer.runner import Runner
        from racer.state import StageResult

        job = load_job(FIXTURES / "valid_job.json")
        job = replace(job, stages=(job.stages[0],))

        class Executor:
            def execute(self, spec, context):
                return StageResult(spec.stage_id, 0, output)

        store = JsonCheckpointStore(directory, job)
        result = Runner(
            job, store, Executor(), FixedClock(datetime(2026, 9, 5, tzinfo=timezone.utc))
        ).run()
        self.assertEqual(result.exit_code, 0)
        return job, store, store.load()

    @staticmethod
    def _json_depth(value):
        if type(value) is list:
            return 1 + max((CheckpointTests._json_depth(item) for item in value), default=0)
        if type(value) is dict:
            return 1 + max((CheckpointTests._json_depth(item) for item in value.values()), default=0)
        return 0

    def test_checkpoint_depth_68_round_trips_real_stage_output_65(self) -> None:
        output = "leaf"
        for _ in range(65):
            output = [output]
        with TemporaryDirectory() as directory:
            job, store, saved = self._run_with_output(directory, output)
            path = Path(directory) / "checkpoint.json"
            before = path.read_bytes()
            value = json.loads(before)
            self.assertEqual(self._json_depth(value["stages"][0]["output"]), 65)
            self.assertEqual(self._json_depth(value), 68)
            reloaded = JsonCheckpointStore(directory, job).load()
            self.assertEqual(reloaded, saved)
            self.assertEqual(reloaded.stages[0].output, output)
            self.assertEqual(path.read_bytes(), before)

    def test_checkpoint_depth_69_rejects_even_when_codec_accepts(self) -> None:
        from racer.jsonio import artifact_json_bytes, strict_json_loads

        with TemporaryDirectory() as directory:
            _, store, _ = self._run_with_output(directory, "small")
            path = Path(directory) / "checkpoint.json"
            value = json.loads(path.read_bytes())
            output = "leaf"
            for _ in range(66):
                output = [output]
            value["stages"][0]["output"] = output
            self.assertEqual(self._json_depth(value), 69)
            raw = artifact_json_bytes(value)
            self.assertEqual(strict_json_loads(raw), value)
            path.write_bytes(raw)
            before_stat = path.stat()
            before_entries = tuple(sorted(Path(directory).iterdir()))
            with self.assertRaisesRegex(CheckpointFormatError, "maximum depth 68"):
                store.load()
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(path.stat().st_mtime_ns, before_stat.st_mtime_ns)
            self.assertEqual(tuple(sorted(Path(directory).iterdir())), before_entries)

    def test_commit_rejects_output_depth_66_without_writes(self) -> None:
        from racer.jsonio import artifact_json_bytes

        with TemporaryDirectory() as directory:
            _, store, saved = self._run_with_output(directory, "small")
            path = Path(directory) / "checkpoint.json"
            output = "leaf"
            for _ in range(66):
                output = [output]
            candidate = replace(saved, stages=(replace(saved.stages[0], output=output),))
            value = json.loads(path.read_bytes())
            value["stages"][0]["output"] = output
            self.assertEqual(self._json_depth(value), 69)
            artifact_json_bytes(value)
            before = path.read_bytes()
            before_stat = path.stat()
            before_entries = tuple(sorted(Path(directory).iterdir()))
            with self.assertRaisesRegex(CheckpointFormatError, "maximum depth 65"):
                store.commit(candidate, expected_revision=saved.revision)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(path.stat().st_mtime_ns, before_stat.st_mtime_ns)
            self.assertEqual(tuple(sorted(Path(directory).iterdir())), before_entries)

    def make_checkpoint(self, *, revision: int = 0) -> tuple[object, Checkpoint]:
        job = load_job(FIXTURES / "valid_job.json")
        return job, Checkpoint(
            schema_version=1,
            revision=revision,
            generation=0,
            run_id="sample_job:0",
            job_id=job.job_id,
            job_fingerprint=job_fingerprint(job),
            run_status=RunStatus.READY,
            stages=tuple(initial_stage_state(stage.stage_id) for stage in job.stages),
            traceability=build_traceability_snapshot(job),
            events=(
                TraceEvent(
                    event_id="sample_job:0:000000",
                    generation=0,
                    sequence=0,
                    kind=TraceEventKind.CHECKPOINT_CREATED,
                    recorded_at="2026-09-02T00:00:00.000000Z",
                    run_status=RunStatus.READY,
                    stage_id=None,
                    stage_status=None,
                    attempt=None,
                    exit_code=None,
                    error_type=None,
                    error_message=None,
                    previous_generation=None,
                    previous_run_status=None,
                ),
            ),
        )

    def commit_path(self, directory: Path) -> tuple[JsonCheckpointStore, Checkpoint, Path]:
        job, checkpoint = self.make_checkpoint()
        store = JsonCheckpointStore(directory, job)
        store.commit(checkpoint, expected_revision=None)
        return store, checkpoint, directory / "checkpoint.json"

    def assert_format_error_after_mutation(self, mutate) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            store, _, path = self.commit_path(directory)
            value = json.loads(path.read_text(encoding="utf-8"))
            mutate(value)
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(CheckpointFormatError):
                store.load()

    def test_checkpoint_commit_round_trips(self) -> None:
        job = load_job(FIXTURES / "valid_job.json")
        checkpoint = Checkpoint(
            schema_version=1,
            revision=0,
            generation=0,
            run_id="sample_job:0",
            job_id=job.job_id,
            job_fingerprint=(
                "e0873f2d33d476d9d8ad868b1a3e643b6b99b9ecce507a45a4d57a0eee723fdf"
            ),
            run_status=RunStatus.READY,
            stages=tuple(initial_stage_state(stage.stage_id) for stage in job.stages),
            traceability=build_traceability_snapshot(job),
            events=(
                TraceEvent(
                    event_id="sample_job:0:000000",
                    generation=0,
                    sequence=0,
                    kind=TraceEventKind.CHECKPOINT_CREATED,
                    recorded_at="2026-09-02T00:00:00.000000Z",
                    run_status=RunStatus.READY,
                    stage_id=None,
                    stage_status=None,
                    attempt=None,
                    exit_code=None,
                    error_type=None,
                    error_message=None,
                    previous_generation=None,
                    previous_run_status=None,
                ),
            ),
        )

        with TemporaryDirectory() as temporary_directory:
            store = JsonCheckpointStore(temporary_directory, job)
            store.commit(checkpoint, expected_revision=None)
            checkpoint_path = Path(temporary_directory) / "checkpoint.json"

            self.assertTrue(checkpoint_path.is_file())
            self.assertEqual(store.load(), checkpoint)
            self.assertTrue(checkpoint_path.read_bytes().endswith(b"\n"))
            self.assertFalse(checkpoint_path.read_bytes().endswith(b"\n\n"))

    def test_checkpoint_schema_is_frozen_and_exact(self) -> None:
        self.assertTrue(Checkpoint.__dataclass_params__.frozen)
        self.assertEqual(
            tuple((field.name, field.type) for field in fields(Checkpoint)),
            (
                ("schema_version", Literal[1]),
                ("revision", int),
                ("generation", int),
                ("run_id", str),
                ("job_id", str),
                ("job_fingerprint", str),
                ("run_status", RunStatus),
                ("stages", tuple[StageState, ...]),
                ("traceability", TraceabilitySnapshot),
                ("events", tuple[TraceEvent, ...]),
            ),
        )
        _, checkpoint = self.make_checkpoint()
        with self.assertRaises(FrozenInstanceError):
            checkpoint.revision = 1

    def test_checkpoint_bytes_are_canonical_and_preserve_unicode(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            store, checkpoint, path = self.commit_path(Path(temporary_directory))
            first = path.read_bytes()
            self.assertEqual(first, json.dumps(
                json.loads(first), sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False,
            ).encode("utf-8") + b"\n")
            self.assertIn(b"checkpoint_created", first)
            changed = Checkpoint(
                **{**checkpoint.__dict__, "revision": 1, "run_status": RunStatus.RUNNING,
                   "stages": (checkpoint.stages[0], checkpoint.stages[1].__class__(
                        "verify", checkpoint.stages[1].status, 0, None, {"text": "雪"}
                    )), "events": checkpoint.events + (TraceEvent(
                        "sample_job:0:000001", 0, 1, TraceEventKind.RUN_STARTED,
                        "2026-09-02T00:00:01.000000Z", RunStatus.RUNNING,
                        None, None, None, None, None, None, None, None,
                    ),)}
            )
            store.commit(changed, expected_revision=0)
            self.assertIn("雪".encode("utf-8"), path.read_bytes())

    def test_commit_rejects_boolean_traceability_schema_before_any_write(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory) / "state"
            job, checkpoint = self.make_checkpoint()
            candidate = replace(
                checkpoint,
                traceability=replace(checkpoint.traceability, schema_version=True),
            )
            with self.assertRaises(CheckpointFormatError):
                JsonCheckpointStore(directory, job).commit(
                    candidate,
                    expected_revision=None,
                )
            self.assertFalse(directory.exists())

    def test_load_rejects_bool_in_every_persisted_integer_field_without_write(self) -> None:
        mutators = (
            lambda value: value.update(schema_version=True),
            lambda value: value.update(revision=True),
            lambda value: value.update(generation=True),
            lambda value: value["stages"][0].update(attempts=True),
            lambda value: value["stages"][0].update(exit_code=True),
            lambda value: value["traceability"].update(schema_version=True),
            lambda value: value["events"][0].update(generation=True),
            lambda value: value["events"][0].update(sequence=True),
            lambda value: value["events"][0].update(attempt=True),
            lambda value: value["events"][0].update(exit_code=True),
            lambda value: value["events"][0].update(previous_generation=True),
        )
        with TemporaryDirectory() as temporary_directory:
            for index, mutate in enumerate(mutators):
                with self.subTest(index=index):
                    directory = Path(temporary_directory) / str(index)
                    store, _, path = self.commit_path(directory)
                    before = path.read_bytes()
                    value = json.loads(before)
                    mutate(value)
                    path.write_bytes(json.dumps(value).encode("utf-8"))
                    malformed = path.read_bytes()
                    with self.assertRaises(CheckpointFormatError):
                        store.load()
                    self.assertEqual(path.read_bytes(), malformed)

    def test_commit_rejects_revision_not_closed_by_event_history_before_any_write(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory) / "state"
            job, checkpoint = self.make_checkpoint()
            started = TraceEvent(
                "sample_job:0:000001", 0, 1, TraceEventKind.RUN_STARTED,
                "2026-09-02T00:00:01.000000Z", RunStatus.RUNNING,
                None, None, None, None, None, None, None, None,
            )
            candidate = replace(
                checkpoint,
                run_status=RunStatus.RUNNING,
                events=checkpoint.events + (started,),
            )
            with self.assertRaises(CheckpointFormatError):
                JsonCheckpointStore(directory, job).commit(
                    candidate,
                    expected_revision=None,
                )
            self.assertFalse(directory.exists())

    def test_load_rejects_revision_not_closed_by_event_history_without_write(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            store, _, path = self.commit_path(Path(temporary_directory))
            value = json.loads(path.read_bytes())
            value["revision"] = 1005
            path.write_bytes(json.dumps(value).encode("utf-8"))
            before = path.read_bytes()
            with self.assertRaises(CheckpointFormatError):
                store.load()
            self.assertEqual(path.read_bytes(), before)

    def test_load_is_read_only_and_missing_checkpoint_creates_nothing(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            missing_directory = Path(temporary_directory) / "missing"
            job, _ = self.make_checkpoint()
            store = JsonCheckpointStore(missing_directory, job)
            with self.assertRaises(CheckpointNotFoundError):
                store.load()
            self.assertFalse(missing_directory.exists())
            directory = Path(temporary_directory) / "present"
            present_store, checkpoint, path = self.commit_path(directory)
            before = path.read_bytes()
            self.assertEqual(present_store.load(), checkpoint)
            self.assertEqual(path.read_bytes(), before)

    def test_load_rejects_malformed_json_and_duplicate_keys(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            store, _, path = self.commit_path(directory)
            for source in (b'{"broken":', b'{"revision":0,"revision":1}'):
                path.write_bytes(source)
                with self.subTest(source=source):
                    with self.assertRaises(CheckpointFormatError):
                        store.load()

    def test_load_rejects_unknown_and_missing_root_or_nested_fields(self) -> None:
        self.assert_format_error_after_mutation(lambda value: value.update(extra=True))
        self.assert_format_error_after_mutation(lambda value: value.pop("events"))
        self.assert_format_error_after_mutation(
            lambda value: value["stages"][0].update(extra=True)
        )
        self.assert_format_error_after_mutation(
            lambda value: value["traceability"].pop("requirements")
        )

    def test_load_rejects_bool_integers_invalid_enums_and_bad_json_values(self) -> None:
        self.assert_format_error_after_mutation(lambda value: value.update(revision=True))
        self.assert_format_error_after_mutation(lambda value: value.update(run_status="OTHER"))
        self.assert_format_error_after_mutation(
            lambda value: value["stages"][0].update(status="OTHER")
        )
        self.assert_format_error_after_mutation(
            lambda value: value["stages"][0].update(output=float("inf"))
        )

    def test_load_rejects_stage_order_and_illegal_state_shapes(self) -> None:
        self.assert_format_error_after_mutation(lambda value: value["stages"].reverse())
        self.assert_format_error_after_mutation(lambda value: value["stages"].pop())
        self.assert_format_error_after_mutation(
            lambda value: value["stages"][0].update(status="SUCCEEDED")
        )

    def test_load_rejects_job_fingerprint_and_traceability_mismatches(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            store, _, path = self.commit_path(directory)
            value = json.loads(path.read_text(encoding="utf-8"))
            value["job_fingerprint"] = "0" * 64
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(CheckpointFingerprintMismatchError):
                store.load()
        self.assert_format_error_after_mutation(
            lambda value: value["traceability"]["requirements"][0].update(text="changed")
        )

    def test_load_rejects_event_identifier_generation_sequence_and_nullability(self) -> None:
        self.assert_format_error_after_mutation(
            lambda value: value["events"][0].update(event_id="wrong")
        )
        self.assert_format_error_after_mutation(
            lambda value: value["events"][0].update(generation=1)
        )
        self.assert_format_error_after_mutation(
            lambda value: value["events"][0].update(sequence=1)
        )
        self.assert_format_error_after_mutation(
            lambda value: value["events"][0].update(stage_id="prepare")
        )

    def test_revision_conflicts_are_zero_write(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            store, checkpoint, path = self.commit_path(Path(temporary_directory))
            before = path.read_bytes()
            started = replace(
                checkpoint,
                revision=1,
                run_status=RunStatus.RUNNING,
                events=checkpoint.events + (TraceEvent(
                    "sample_job:0:000001", 0, 1, TraceEventKind.RUN_STARTED,
                    "2026-09-02T00:00:01.000000Z", RunStatus.RUNNING,
                    None, None, None, None, None, None, None, None,
                ),),
            )
            for expected_revision, candidate in (
                (None, checkpoint),
                (1, started),
                (-1, checkpoint),
                (True, checkpoint),
            ):
                with self.subTest(expected_revision=expected_revision):
                    with self.assertRaises(RevisionConflictError):
                        store.commit(candidate, expected_revision=expected_revision)
                    self.assertEqual(path.read_bytes(), before)
            missing = JsonCheckpointStore(Path(temporary_directory) / "missing", self.make_checkpoint()[0])
            with self.assertRaises(RevisionConflictError):
                missing.commit(started, expected_revision=0)
            self.assertFalse((Path(temporary_directory) / "missing").exists())

    def test_commit_fsyncs_and_replaces_a_same_directory_temporary_file(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory) / "state"
            job, checkpoint = self.make_checkpoint()
            store = JsonCheckpointStore(directory, job)
            with (
                patch("racer.checkpoint.os.fsync", wraps=os.fsync) as fsync,
                patch("racer.checkpoint.os.replace", wraps=os.replace) as replace,
            ):
                store.commit(checkpoint, expected_revision=None)
            fsync.assert_called_once()
            replace.assert_called_once()
            temporary_path, final_path = replace.call_args.args
            self.assertEqual(Path(temporary_path).parent, directory.resolve())
            self.assertEqual(Path(final_path), directory.resolve() / "checkpoint.json")
            self.assertEqual(tuple(path.name for path in directory.iterdir()), ("checkpoint.json",))

    def test_replace_failure_preserves_old_bytes_and_cleans_temporary_file(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            store, checkpoint, path = self.commit_path(directory)
            before = path.read_bytes()
            updated = replace(
                checkpoint,
                revision=1,
                run_status=RunStatus.RUNNING,
                events=checkpoint.events + (TraceEvent(
                    "sample_job:0:000001", 0, 1, TraceEventKind.RUN_STARTED,
                    "2026-09-02T00:00:01.000000Z", RunStatus.RUNNING,
                    None, None, None, None, None, None, None, None,
                ),),
            )
            with patch("racer.checkpoint.os.replace", side_effect=OSError("replace")):
                with self.assertRaises(CheckpointCommitError) as caught:
                    store.commit(updated, expected_revision=0)
            self.assertIsInstance(caught.exception.__cause__, OSError)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(tuple(item.name for item in directory.iterdir()), ("checkpoint.json",))

    def test_invalid_candidate_creates_no_directory_and_repeated_input_is_identical(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory) / "missing"
            job, checkpoint = self.make_checkpoint()
            store = JsonCheckpointStore(directory, job)
            invalid = Checkpoint(**{**checkpoint.__dict__, "revision": True})
            with self.assertRaises(CheckpointFormatError):
                store.commit(invalid, expected_revision=None)
            self.assertFalse(directory.exists())
            first_directory = Path(temporary_directory) / "first"
            second_directory = Path(temporary_directory) / "second"
            first_store = JsonCheckpointStore(first_directory, job)
            second_store = JsonCheckpointStore(second_directory, job)
            first_store.commit(checkpoint, expected_revision=None)
            second_store.commit(checkpoint, expected_revision=None)
            self.assertEqual(
                (first_directory / "checkpoint.json").read_bytes(),
                (second_directory / "checkpoint.json").read_bytes(),
            )

    def test_constructor_and_exists_are_zero_write(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            missing_directory = Path(temporary_directory) / "state"
            job, _ = self.make_checkpoint()
            store = JsonCheckpointStore(missing_directory, job)

            self.assertFalse(store.exists())
            self.assertFalse(missing_directory.exists())

    def test_load_rejects_nonfinite_constants_and_bad_recorded_at(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            store, _, path = self.commit_path(directory)
            for constant in ("NaN", "Infinity", "-Infinity"):
                path.write_text('{"value":' + constant + "}", encoding="utf-8")
                with self.subTest(constant=constant):
                    with self.assertRaises(CheckpointFormatError):
                        store.load()
        self.assert_format_error_after_mutation(
            lambda value: value["events"][0].update(recorded_at="2026-09-02")
        )

    def test_load_rejects_run_id_mismatch_as_format_error(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            store, _, path = self.commit_path(directory)
            value = json.loads(path.read_text(encoding="utf-8"))
            value["run_id"] = "sample_job:7"
            path.write_text(json.dumps(value), encoding="utf-8")

            with self.assertRaises(CheckpointFormatError):
                store.load()

    def test_load_rejects_event_type_and_semantic_violations(self) -> None:
        self.assert_format_error_after_mutation(
            lambda value: value["events"][0].update(exit_code=True)
        )
        self.assert_format_error_after_mutation(
            lambda value: value["events"][0].update(error_type="unexpected")
        )
        self.assert_format_error_after_mutation(
            lambda value: value["events"][0].update(previous_generation=0)
        )

    def test_commit_rejects_invalid_in_memory_event_without_creating_directory(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory) / "state"
            job, checkpoint = self.make_checkpoint()
            invalid = replace(
                checkpoint,
                events=(replace(checkpoint.events[0], exit_code=True),),
            )

            with self.assertRaises(CheckpointFormatError):
                JsonCheckpointStore(directory, job).commit(
                    invalid,
                    expected_revision=None,
                )
            self.assertFalse(directory.exists())

    def test_reset_generation_event_round_trips_and_requires_previous_fields(self) -> None:
        job, checkpoint = self.make_checkpoint()
        failed_events = (
            checkpoint.events[0],
            TraceEvent("sample_job:0:000001", 0, 1, TraceEventKind.RUN_STARTED, "2026-09-02T00:00:01.000000Z", RunStatus.RUNNING, None, None, None, None, None, None, None, None),
            TraceEvent("sample_job:0:000002", 0, 2, TraceEventKind.STAGE_STARTED, "2026-09-02T00:00:02.000000Z", RunStatus.RUNNING, "prepare", StageStatus.RUNNING, 1, None, None, None, None, None),
            TraceEvent("sample_job:0:000003", 0, 3, TraceEventKind.STAGE_FAILED, "2026-09-02T00:00:03.000000Z", RunStatus.FAILED, "prepare", StageStatus.FAILED, 1, 1, "StageFailedError", "stage 'prepare' failed with executor exit code 1", None, None),
            TraceEvent("sample_job:0:000004", 0, 4, TraceEventKind.RUN_FAILED, "2026-09-02T00:00:03.000000Z", RunStatus.FAILED, None, None, None, 4, "StageFailedError", "stage 'prepare' failed with executor exit code 1", None, None),
        )
        reset_event = TraceEvent(
            event_id="sample_job:1:000000",
            generation=1,
            sequence=0,
            kind=TraceEventKind.RESET,
            recorded_at="2026-09-02T00:00:01.000000Z",
            run_status=RunStatus.READY,
            stage_id=None,
            stage_status=None,
            attempt=None,
            exit_code=None,
            error_type=None,
            error_message=None,
            previous_generation=0,
            previous_run_status=RunStatus.FAILED,
        )
        reset_checkpoint = replace(
            checkpoint,
            revision=4,
            generation=1,
            run_id="sample_job:1",
            events=failed_events + (reset_event,),
        )

        with TemporaryDirectory() as temporary_directory:
            store = JsonCheckpointStore(temporary_directory, job)
            store.commit(checkpoint, expected_revision=None)
            running = replace(
                checkpoint,
                revision=1,
                run_status=RunStatus.RUNNING,
                events=failed_events[:2],
            )
            active = replace(
                running,
                revision=2,
                stages=(StageState("prepare", StageStatus.RUNNING, 1, None, None), checkpoint.stages[1]),
                events=failed_events[:3],
            )
            failed = replace(
                active,
                revision=3,
                run_status=RunStatus.FAILED,
                stages=(StageState("prepare", StageStatus.FAILED, 1, 1, None), checkpoint.stages[1]),
                events=failed_events,
            )
            store.commit(running, expected_revision=0)
            store.commit(active, expected_revision=1)
            store.commit(failed, expected_revision=2)
            store.commit(reset_checkpoint, expected_revision=3)
            self.assertEqual(store.load(), reset_checkpoint)
        self.assert_format_error_after_mutation(
            lambda value: value["events"][0].update(kind="reset", previous_generation=None)
        )

    def test_failure_mapping_uses_run_exit_channel_not_error_type(self) -> None:
        job, checkpoint = self.make_checkpoint()
        error = StageFailedError("prepare", 9)
        events = checkpoint.events + (
            TraceEvent("sample_job:0:000001", 0, 1, TraceEventKind.RUN_STARTED, "2026-09-02T00:00:01.000000Z", RunStatus.RUNNING, None, None, None, None, None, None, None, None),
            TraceEvent("sample_job:0:000002", 0, 2, TraceEventKind.STAGE_STARTED, "2026-09-02T00:00:02.000000Z", RunStatus.RUNNING, "prepare", StageStatus.RUNNING, 1, None, None, None, None, None),
            TraceEvent("sample_job:0:000003", 0, 3, TraceEventKind.STAGE_FAILED, "2026-09-02T00:00:03.000000Z", RunStatus.FAILED, "prepare", StageStatus.FAILED, 1, 70, "StageFailedError", str(error), None, None),
            TraceEvent("sample_job:0:000004", 0, 4, TraceEventKind.RUN_FAILED, "2026-09-02T00:00:03.000000Z", RunStatus.FAILED, None, None, None, 70, "StageFailedError", str(error), None, None),
        )
        running = replace(checkpoint, revision=1, run_status=RunStatus.RUNNING, events=events[:2])
        active = replace(
            running,
            revision=2,
            stages=(StageState("prepare", StageStatus.RUNNING, 1, None, None), checkpoint.stages[1]),
            events=events[:3],
        )
        failed = replace(
            active,
            revision=3,
            run_status=RunStatus.FAILED,
            stages=(StageState("prepare", StageStatus.FAILED, 1, 70, {"error_type": "StageFailedError", "message": str(error)}), checkpoint.stages[1]),
            events=events,
        )

        with TemporaryDirectory() as temporary_directory:
            store = JsonCheckpointStore(temporary_directory, job)
            store.commit(checkpoint, expected_revision=None)
            store.commit(running, expected_revision=0)
            store.commit(active, expected_revision=1)
            store.commit(failed, expected_revision=2)

            self.assertEqual(store.load(), failed)

    def test_load_rejects_event_history_with_wrong_durable_status(self) -> None:
        self.assert_format_error_after_mutation(
            lambda value: value["events"].append(
                {
                    "event_id": "sample_job:0:000001",
                    "generation": 0,
                    "sequence": 1,
                    "kind": "run_started",
                    "recorded_at": "2026-09-02T00:00:01.000000Z",
                    "run_status": "READY",
                    "stage_id": None,
                    "stage_status": None,
                    "attempt": None,
                    "exit_code": None,
                    "error_type": None,
                    "error_message": None,
                    "previous_generation": None,
                    "previous_run_status": None,
                }
            )
        )

    def test_load_rejects_stage_event_attempt_not_matching_durable_state(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            store, _, path = self.commit_path(directory)
            value = json.loads(path.read_text(encoding="utf-8"))
            value["run_status"] = "RUNNING"
            value["stages"][0].update(status="RUNNING", attempts=1)
            value["events"].extend(
                (
                    {
                        "event_id": "sample_job:0:000001",
                        "generation": 0,
                        "sequence": 1,
                        "kind": "run_started",
                        "recorded_at": "2026-09-02T00:00:01.000000Z",
                        "run_status": "RUNNING",
                        "stage_id": None,
                        "stage_status": None,
                        "attempt": None,
                        "exit_code": None,
                        "error_type": None,
                        "error_message": None,
                        "previous_generation": None,
                        "previous_run_status": None,
                    },
                    {
                        "event_id": "sample_job:0:000002",
                        "generation": 0,
                        "sequence": 2,
                        "kind": "stage_started",
                        "recorded_at": "2026-09-02T00:00:02.000000Z",
                        "run_status": "RUNNING",
                        "stage_id": "prepare",
                        "stage_status": "RUNNING",
                        "attempt": 2,
                        "exit_code": None,
                        "error_type": None,
                        "error_message": None,
                        "previous_generation": None,
                        "previous_run_status": None,
                    },
                )
            )
            path.write_text(json.dumps(value), encoding="utf-8")

            with self.assertRaises(CheckpointFormatError):
                store.load()

    def test_load_rejects_reset_outside_new_generation_sequence_zero(self) -> None:
        self.assert_format_error_after_mutation(
            lambda value: value["events"].append(
                {
                    "event_id": "sample_job:0:000001",
                    "generation": 0,
                    "sequence": 1,
                    "kind": "reset",
                    "recorded_at": "2026-09-02T00:00:01.000000Z",
                    "run_status": "READY",
                    "stage_id": None,
                    "stage_status": None,
                    "attempt": None,
                    "exit_code": None,
                    "error_type": None,
                    "error_message": None,
                    "previous_generation": -1,
                    "previous_run_status": "READY",
                }
            )
        )

    def test_commit_normalizes_malformed_memory_objects_to_format_error(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory) / "state"
            job, checkpoint = self.make_checkpoint()
            malformed_stages = replace(checkpoint, stages=(None,))
            malformed_event = replace(
                checkpoint,
                events=(replace(checkpoint.events[0], stage_id=[]),),
            )
            store = JsonCheckpointStore(directory, job)

            for candidate in (malformed_stages, malformed_event):
                with self.subTest(candidate=candidate):
                    with self.assertRaises(CheckpointFormatError):
                        store.commit(candidate, expected_revision=None)
                    self.assertFalse(directory.exists())

    def test_revision_conflicts_create_no_temp_file_or_replace(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            store, checkpoint, _ = self.commit_path(Path(temporary_directory))
            with (
                patch("racer.checkpoint.NamedTemporaryFile") as temporary_file,
                patch("racer.checkpoint.os.replace") as replace_file,
            ):
                with self.assertRaises(CheckpointFormatError):
                    store.commit(replace(checkpoint, revision=2), expected_revision=0)
            temporary_file.assert_not_called()
            replace_file.assert_not_called()

    def test_existing_damaged_checkpoint_precedes_initial_revision_conflict(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            job, checkpoint = self.make_checkpoint()
            (directory / "checkpoint.json").write_text("{broken", encoding="utf-8")
            store = JsonCheckpointStore(directory, job)

            with self.assertRaises(CheckpointFormatError):
                store.commit(checkpoint, expected_revision=None)
            self.assertEqual((directory / "checkpoint.json").read_text(encoding="utf-8"), "{broken")

    def test_atomic_commit_order_closes_before_replace(self) -> None:
        calls: list[str] = []

        class TemporaryFile:
            name = "temporary-checkpoint"

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                calls.append("close")

            def write(self, data):
                calls.append("write")

            def flush(self):
                calls.append("flush")

            def fileno(self):
                return 1

        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory) / "state"
            job, checkpoint = self.make_checkpoint()
            with (
                patch("racer.checkpoint.NamedTemporaryFile", return_value=TemporaryFile()),
                patch("racer.checkpoint.os.fsync", side_effect=lambda _: calls.append("fsync")),
                patch("racer.checkpoint.os.replace", side_effect=lambda *_: calls.append("replace")),
            ):
                JsonCheckpointStore(directory, job).commit(checkpoint, expected_revision=None)

        self.assertEqual(calls, ["write", "flush", "fsync", "close", "replace"])

    def failed_checkpoint(self) -> tuple[object, Checkpoint]:
        job, checkpoint = self.make_checkpoint()
        events = checkpoint.events + (
            TraceEvent("sample_job:0:000001", 0, 1, TraceEventKind.RUN_STARTED, "2026-09-02T00:00:01.000000Z", RunStatus.RUNNING, None, None, None, None, None, None, None, None),
            TraceEvent("sample_job:0:000002", 0, 2, TraceEventKind.STAGE_STARTED, "2026-09-02T00:00:02.000000Z", RunStatus.RUNNING, "prepare", StageStatus.RUNNING, 1, None, None, None, None, None),
            TraceEvent("sample_job:0:000003", 0, 3, TraceEventKind.STAGE_FAILED, "2026-09-02T00:00:03.000000Z", RunStatus.FAILED, "prepare", StageStatus.FAILED, 1, 1, "StageFailedError", "stage 'prepare' failed with executor exit code 1", None, None),
            TraceEvent("sample_job:0:000004", 0, 4, TraceEventKind.RUN_FAILED, "2026-09-02T00:00:03.000000Z", RunStatus.FAILED, None, None, None, 4, "StageFailedError", "stage 'prepare' failed with executor exit code 1", None, None),
        )
        return job, replace(
            checkpoint,
            revision=3,
            run_status=RunStatus.FAILED,
            stages=(
                StageState("prepare", StageStatus.FAILED, 1, 1, None),
                checkpoint.stages[1],
            ),
            events=events,
        )

    def interrupted_checkpoint(self) -> tuple[object, Checkpoint]:
        job, checkpoint = self.make_checkpoint()
        events = checkpoint.events + (
            TraceEvent("sample_job:0:000001", 0, 1, TraceEventKind.RUN_STARTED, "2026-09-02T00:00:01.000000Z", RunStatus.RUNNING, None, None, None, None, None, None, None, None),
            TraceEvent("sample_job:0:000002", 0, 2, TraceEventKind.STAGE_STARTED, "2026-09-02T00:00:02.000000Z", RunStatus.RUNNING, "prepare", StageStatus.RUNNING, 1, None, None, None, None, None),
            TraceEvent("sample_job:0:000003", 0, 3, TraceEventKind.STAGE_INTERRUPTED, "2026-09-02T00:00:03.000000Z", RunStatus.INTERRUPTED, "prepare", StageStatus.INTERRUPTED, 1, None, None, None, None, None),
            TraceEvent("sample_job:0:000004", 0, 4, TraceEventKind.RUN_INTERRUPTED, "2026-09-02T00:00:03.000000Z", RunStatus.INTERRUPTED, None, None, None, None, None, None, None, None),
        )
        return job, replace(
            checkpoint,
            revision=3,
            run_status=RunStatus.INTERRUPTED,
            stages=(
                StageState("prepare", StageStatus.INTERRUPTED, 1, None, None),
                checkpoint.stages[1],
            ),
            events=events,
        )

    def test_failure_events_must_be_adjacent_matched_and_unique(self) -> None:
        job, checkpoint = self.failed_checkpoint()
        cases = (
            replace(checkpoint, events=checkpoint.events[:-1]),
            replace(checkpoint, events=checkpoint.events + (
                replace(checkpoint.events[-1], sequence=5, event_id="sample_job:0:000005"),
            )),
            replace(checkpoint, events=checkpoint.events[:-1] + (
                replace(checkpoint.events[-1], error_message="other"),
            )),
            replace(checkpoint, events=checkpoint.events[:-1] + (
                replace(checkpoint.events[-1], exit_code=70),
            )),
            replace(checkpoint, events=checkpoint.events[:-2] + (
                replace(checkpoint.events[-2], recorded_at="2026-09-02T00:00:04.000000Z"),
                checkpoint.events[-1],
            )),
        )
        with TemporaryDirectory() as temporary_directory:
            for index, candidate in enumerate(cases):
                candidate = replace(candidate, revision=0)
                store = JsonCheckpointStore(
                    Path(temporary_directory) / str(index),
                    job,
                )
                with self.subTest(candidate=candidate.events[-1]):
                    with self.assertRaises(CheckpointFormatError):
                        store.commit(candidate, expected_revision=None)

    def test_interrupted_and_resumed_pairs_must_be_adjacent_and_same_stage(self) -> None:
        job, interrupted = self.interrupted_checkpoint()
        resumed_events = interrupted.events + (
            TraceEvent("sample_job:0:000005", 0, 5, TraceEventKind.RUN_RESUMED, "2026-09-02T00:00:04.000000Z", RunStatus.RUNNING, None, None, None, None, None, None, None, None),
            TraceEvent("sample_job:0:000006", 0, 6, TraceEventKind.STAGE_STARTED, "2026-09-02T00:00:04.000000Z", RunStatus.RUNNING, "prepare", StageStatus.RUNNING, 2, None, None, None, None, None),
        )
        resumed = replace(
            interrupted,
            revision=4,
            run_status=RunStatus.RUNNING,
            stages=(StageState("prepare", StageStatus.RUNNING, 2, None, None), interrupted.stages[1]),
            events=resumed_events,
        )
        cases = (
            replace(interrupted, events=interrupted.events[:-1]),
            replace(resumed, events=resumed.events[:-1]),
            replace(resumed, events=resumed.events[:-1] + (
                replace(resumed.events[-1], stage_id="verify"),
            )),
            replace(resumed, events=resumed.events[:-2] + (
                replace(resumed.events[-2], exit_code=1), resumed.events[-1],
            )),
        )
        with TemporaryDirectory() as temporary_directory:
            for index, candidate in enumerate(cases):
                candidate = replace(candidate, revision=0)
                store = JsonCheckpointStore(
                    Path(temporary_directory) / str(index),
                    job,
                )
                with self.subTest(candidate=candidate.events[-1]):
                    with self.assertRaises(CheckpointFormatError):
                        store.commit(candidate, expected_revision=None)

    def test_run_started_reset_and_resumed_require_null_exit_codes(self) -> None:
        job, failed = self.failed_checkpoint()
        reset = TraceEvent("sample_job:1:000000", 1, 0, TraceEventKind.RESET, "2026-09-02T00:00:04.000000Z", RunStatus.READY, None, None, None, 1, None, None, 0, RunStatus.FAILED)
        invalid_reset = replace(
            failed,
            revision=2,
            generation=1,
            run_id="sample_job:1",
            run_status=RunStatus.READY,
            stages=tuple(initial_stage_state(stage.stage_id) for stage in job.stages),
            events=failed.events + (reset,),
        )
        _, initial = self.make_checkpoint()
        invalid_started = replace(
            initial,
            revision=1,
            run_status=RunStatus.RUNNING,
            events=initial.events + (
                replace(failed.events[1], exit_code=1),
            ),
        )
        with TemporaryDirectory() as temporary_directory:
            for index, candidate in enumerate((invalid_reset, invalid_started)):
                candidate = replace(candidate, revision=0)
                store = JsonCheckpointStore(
                    Path(temporary_directory) / str(index),
                    job,
                )
                with self.subTest(candidate=candidate.events[-1]):
                    with self.assertRaises(CheckpointFormatError):
                        store.commit(candidate, expected_revision=None)

    def test_deep_memory_json_value_is_format_error_without_writes(self) -> None:
        value: list[object] = []
        for _ in range(1500):
            value = [value]
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory) / "state"
            job, checkpoint = self.make_checkpoint()
            candidate = replace(
                checkpoint,
                stages=(
                    replace(checkpoint.stages[0], output=value),
                    checkpoint.stages[1],
                ),
            )
            with self.assertRaises(CheckpointFormatError):
                JsonCheckpointStore(directory, job).commit(
                    candidate,
                    expected_revision=None,
                )
            self.assertFalse(directory.exists())

    def test_explicit_stage_failure_exit_is_limited_to_executor_byte_range(self) -> None:
        job, checkpoint = self.failed_checkpoint()

        def candidate_for(exit_code: int) -> Checkpoint:
            message = (
                "stage 'prepare' failed with executor exit code "
                f"{exit_code}"
            )
            events = checkpoint.events[:-2] + (
                replace(
                    checkpoint.events[-2],
                    exit_code=exit_code,
                    error_message=message,
                ),
                replace(checkpoint.events[-1], error_message=message),
            )
            return replace(
                checkpoint,
                revision=3,
                stages=(
                    StageState("prepare", StageStatus.FAILED, 1, exit_code, None),
                    checkpoint.stages[1],
                ),
                events=events,
            )

        def commit_prefix(directory: Path) -> JsonCheckpointStore:
            initial = replace(
                checkpoint,
                revision=0,
                run_status=RunStatus.READY,
                stages=tuple(initial_stage_state(stage.stage_id) for stage in job.stages),
                events=checkpoint.events[:1],
            )
            started = replace(
                initial,
                revision=1,
                run_status=RunStatus.RUNNING,
                events=checkpoint.events[:2],
            )
            active = replace(
                started,
                revision=2,
                stages=(StageState("prepare", StageStatus.RUNNING, 1, None, None), initial.stages[1]),
                events=checkpoint.events[:3],
            )
            store = JsonCheckpointStore(directory, job)
            store.commit(initial, expected_revision=None)
            store.commit(started, expected_revision=0)
            store.commit(active, expected_revision=1)
            return store

        with TemporaryDirectory() as temporary_directory:
            valid_directory = Path(temporary_directory) / "valid"
            commit_prefix(valid_directory).commit(
                candidate_for(255),
                expected_revision=2,
            )
            self.assertTrue((valid_directory / "checkpoint.json").is_file())

            invalid_directory = Path(temporary_directory) / "invalid"
            invalid_store = commit_prefix(invalid_directory)
            before = (invalid_directory / "checkpoint.json").read_bytes()
            with self.assertRaises(CheckpointFormatError):
                invalid_store.commit(
                    candidate_for(256),
                    expected_revision=2,
                )
            self.assertEqual((invalid_directory / "checkpoint.json").read_bytes(), before)

    def test_checkpoint_rejects_unsafe_or_oversized_failure_error_fields_without_write(self) -> None:
        job, failed = self.failed_checkpoint()

        def candidate(error_type: str, message: str) -> Checkpoint:
            stage_event = replace(
                failed.events[-2],
                exit_code=70,
                error_type=error_type,
                error_message=message,
            )
            run_event = replace(
                failed.events[-1],
                exit_code=70,
                error_type=error_type,
                error_message=message,
            )
            return replace(
                failed,
                stages=(
                    StageState(
                        "prepare",
                        StageStatus.FAILED,
                        1,
                        70,
                        {"error_type": error_type, "message": message},
                    ),
                    failed.stages[1],
                ),
                events=failed.events[:-2] + (stage_event, run_event),
            )

        invalid_values = (
            ("x" * 129, "message"),
            ("Error", "x" * 4097),
            ("bad\ud800", "message"),
            ("Error", "bad\ud800"),
        )

        def seeded_store(directory: Path) -> JsonCheckpointStore:
            initial = replace(
                failed,
                revision=0,
                run_status=RunStatus.READY,
                stages=tuple(initial_stage_state(stage.stage_id) for stage in job.stages),
                events=failed.events[:1],
            )
            running = replace(
                initial,
                revision=1,
                run_status=RunStatus.RUNNING,
                events=failed.events[:2],
            )
            active = replace(
                running,
                revision=2,
                stages=(
                    StageState("prepare", StageStatus.RUNNING, 1, None, None),
                    initial.stages[1],
                ),
                events=failed.events[:3],
            )
            store = JsonCheckpointStore(directory, job)
            store.commit(initial, expected_revision=None)
            store.commit(running, expected_revision=0)
            store.commit(active, expected_revision=1)
            return store

        with TemporaryDirectory() as temporary_directory:
            for index, values in enumerate(invalid_values):
                with self.subTest(values=values):
                    directory = Path(temporary_directory) / str(index)
                    store = seeded_store(directory)
                    before = (directory / "checkpoint.json").read_bytes()
                    with self.assertRaises(CheckpointFormatError):
                        store.commit(candidate(*values), expected_revision=2)
                    self.assertEqual((directory / "checkpoint.json").read_bytes(), before)

            directory = Path(temporary_directory) / "load"
            store = seeded_store(directory)
            valid = candidate("Error", "message")
            store.commit(valid, expected_revision=2)
            path = directory / "checkpoint.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            value["events"][-2]["error_message"] = "x" * 4097
            value["events"][-1]["error_message"] = "x" * 4097
            path.write_text(json.dumps(value), encoding="utf-8")
            before = path.read_bytes()
            with self.assertRaises(CheckpointFormatError):
                store.load()
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
