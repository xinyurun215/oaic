import unittest
from copy import deepcopy
from dataclasses import MISSING, FrozenInstanceError, fields, is_dataclass
from pathlib import Path
from subprocess import run
import os
import socket
import sys
from tempfile import TemporaryDirectory
from typing import Literal
from unittest.mock import patch

import racer.traceability as traceability_module
from racer.errors import JobValidationError
from racer.job import (
    InterfaceSpec,
    JobSpec,
    RequirementSpec,
    StageSpec,
    TestSpec,
    load_job,
)
from racer.state import RunStatus, StageState, StageStatus
from racer.traceability import (
    StageTraceLink,
    TraceabilitySnapshot,
    build_traceability_snapshot,
)


FIXTURES = Path(__file__).parent / "fixtures"


class TraceabilityTests(unittest.TestCase):
    def test_builds_complete_traceability_snapshot(self) -> None:
        job = load_job(FIXTURES / "valid_job.json")

        snapshot = build_traceability_snapshot(job)

        self.assertEqual(
            snapshot,
            TraceabilitySnapshot(
                schema_version=1,
                requirements=job.requirements,
                interfaces=job.interfaces,
                tests=job.tests,
                stage_links=(
                    StageTraceLink(
                        stage_id="prepare",
                        requirement_ids=("req_input",),
                        interface_ids=("api",),
                        test_ids=("test_input",),
                    ),
                    StageTraceLink(
                        stage_id="verify",
                        requirement_ids=("req_input",),
                        interface_ids=("api",),
                        test_ids=("test_input",),
                    ),
                ),
            ),
        )

    def test_trace_event_kind_members_are_exact_and_unique(self) -> None:
        self.assertEqual(
            tuple(
                (member.name, member.value)
                for member in traceability_module.TraceEventKind
            ),
            (
                ("CHECKPOINT_CREATED", "checkpoint_created"),
                ("RUN_STARTED", "run_started"),
                ("RUN_RESUMED", "run_resumed"),
                ("STAGE_STARTED", "stage_started"),
                ("STAGE_SUCCEEDED", "stage_succeeded"),
                ("STAGE_FAILED", "stage_failed"),
                ("STAGE_INTERRUPTED", "stage_interrupted"),
                ("RUN_INTERRUPTED", "run_interrupted"),
                ("RUN_SUCCEEDED", "run_succeeded"),
                ("RUN_FAILED", "run_failed"),
                ("RESET", "reset"),
            ),
        )

    def test_traceability_data_container_schemas_are_exact_and_frozen(
        self,
    ) -> None:
        expected_schemas = {
            traceability_module.StageTraceLink: (
                ("stage_id", str),
                ("requirement_ids", tuple[str, ...]),
                ("interface_ids", tuple[str, ...]),
                ("test_ids", tuple[str, ...]),
            ),
            traceability_module.TraceEvent: (
                ("event_id", str),
                ("generation", int),
                ("sequence", int),
                ("kind", traceability_module.TraceEventKind),
                ("recorded_at", str),
                ("run_status", RunStatus),
                ("stage_id", str | None),
                ("stage_status", StageStatus | None),
                ("attempt", int | None),
                ("exit_code", int | None),
                ("error_type", str | None),
                ("error_message", str | None),
                ("previous_generation", int | None),
                ("previous_run_status", RunStatus | None),
            ),
            traceability_module.TraceabilitySnapshot: (
                ("schema_version", Literal[1]),
                ("requirements", tuple[RequirementSpec, ...]),
                ("interfaces", tuple[InterfaceSpec, ...]),
                ("tests", tuple[TestSpec, ...]),
                ("stage_links", tuple[traceability_module.StageTraceLink, ...]),
            ),
            traceability_module.TraceDocument: (
                ("schema_version", Literal[1]),
                ("job_id", str),
                ("job_fingerprint", str),
                ("current_generation", int),
                ("current_run_id", str),
                ("current_revision", int),
                ("current_run_status", RunStatus),
                ("stage_states", tuple[StageState, ...]),
                (
                    "traceability",
                    traceability_module.TraceabilitySnapshot,
                ),
                ("events", tuple[traceability_module.TraceEvent, ...]),
            ),
        }

        for value_class, expected_schema in expected_schemas.items():
            with self.subTest(value_class=value_class.__name__):
                actual_fields = fields(value_class)
                self.assertEqual(
                    tuple((field.name, field.type) for field in actual_fields),
                    expected_schema,
                )
                self.assertTrue(
                    value_class.__dataclass_params__.frozen,
                )
                self.assertTrue(
                    all(
                        field.default is MISSING
                        and field.default_factory is MISSING
                        for field in actual_fields
                    ),
                )

        self.assertEqual(
            tuple(
                sorted(
                    name
                    for name, value in vars(traceability_module).items()
                    if isinstance(value, type)
                    and value.__module__ == traceability_module.__name__
                    and is_dataclass(value)
                )
            ),
            (
                "StageTraceLink",
                "TraceDocument",
                "TraceEvent",
                "TraceabilitySnapshot",
            ),
        )

        job = load_job(FIXTURES / "valid_job.json")
        link = traceability_module.StageTraceLink(
            "prepare", ("req_input",), ("api",), ("test_input",)
        )
        event = traceability_module.TraceEvent(
            "sample_job:0:000000",
            0,
            0,
            traceability_module.TraceEventKind.CHECKPOINT_CREATED,
            "2026-09-02T00:00:00.000000Z",
            RunStatus.READY,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )
        snapshot = traceability_module.TraceabilitySnapshot(
            1, job.requirements, job.interfaces, job.tests, (link,)
        )
        document = traceability_module.TraceDocument(
            1,
            job.job_id,
            "a" * 64,
            0,
            "sample_job:0",
            0,
            RunStatus.READY,
            (
                StageState(
                    "prepare", StageStatus.PENDING, 0, None, None
                ),
            ),
            snapshot,
            (event,),
        )
        for value in (link, event, snapshot, document):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(FrozenInstanceError):
                    setattr(value, next(iter(value.__dict__)), "changed")

    def test_snapshot_preserves_declaration_order_and_reference_tuples(
        self,
    ) -> None:
        job = JobSpec(
            schema_version=1,
            job_id="trace_job",
            requirements=(
                RequirementSpec("req_z", "Z requirement"),
                RequirementSpec("req_a", "A requirement"),
            ),
            interfaces=(
                InterfaceSpec("api_z", "Z interface", ("req_z",)),
                InterfaceSpec("api_a", "A interface", ("req_a",)),
            ),
            tests=(
                TestSpec("test_z", "Z test", ("req_z",), ("api_z",)),
                TestSpec("test_a", "A test", ("req_a",), ("api_a",)),
            ),
            stages=(
                StageSpec(
                    "stage_z",
                    "emit",
                    None,
                    ("req_z", "req_a"),
                    ("api_z", "api_a"),
                    ("test_z", "test_a"),
                ),
                StageSpec(
                    "stage_a",
                    "emit",
                    None,
                    ("req_a", "req_z"),
                    ("api_a", "api_z"),
                    ("test_a", "test_z"),
                ),
            ),
        )

        snapshot = traceability_module.build_traceability_snapshot(job)

        self.assertIs(snapshot.requirements, job.requirements)
        self.assertIs(snapshot.interfaces, job.interfaces)
        self.assertIs(snapshot.tests, job.tests)
        self.assertEqual(
            tuple(link.stage_id for link in snapshot.stage_links),
            ("stage_z", "stage_a"),
        )
        self.assertEqual(
            tuple(link.requirement_ids for link in snapshot.stage_links),
            (("req_z", "req_a"), ("req_a", "req_z")),
        )
        self.assertEqual(
            tuple(link.interface_ids for link in snapshot.stage_links),
            (("api_z", "api_a"), ("api_a", "api_z")),
        )
        self.assertEqual(
            tuple(link.test_ids for link in snapshot.stage_links),
            (("test_z", "test_a"), ("test_a", "test_z")),
        )

    def test_manual_empty_job_is_rejected_at_construction(self) -> None:
        with self.assertRaises(JobValidationError):
            JobSpec(
                schema_version=1,
                job_id="empty_refs",
                requirements=(),
                interfaces=(),
                tests=(),
                stages=(
                    StageSpec("stage", "emit", None, (), (), ()),
                ),
            )

    def test_snapshot_is_deterministic_and_does_not_modify_job(self) -> None:
        job = load_job(FIXTURES / "valid_job.json")
        before = deepcopy(job)

        first = traceability_module.build_traceability_snapshot(job)
        second = traceability_module.build_traceability_snapshot(job)

        self.assertEqual(first, second)
        self.assertEqual(job, before)

    def test_builder_does_not_open_files_use_clock_or_network(self) -> None:
        job = load_job(FIXTURES / "valid_job.json")

        with (
            patch("builtins.open", side_effect=AssertionError("file I/O")),
            patch("socket.socket", side_effect=AssertionError("network I/O")),
            patch("time.time", side_effect=AssertionError("clock I/O")),
        ):
            snapshot = traceability_module.build_traceability_snapshot(job)

        self.assertEqual(snapshot.schema_version, 1)

    def test_import_has_no_output_or_filesystem_side_effects(self) -> None:
        project_root = Path(__file__).parent.parent
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(project_root)

        with TemporaryDirectory() as temp_directory:
            directory = Path(temp_directory)
            before = tuple(directory.iterdir())
            completed = run(
                [sys.executable, "-B", "-c", "import racer.traceability"],
                cwd=directory,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            after = tuple(directory.iterdir())

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr, "")
        self.assertEqual(before, after)
        self.assertEqual(
            tuple(traceability_module.TraceEventKind.__members__),
            (
                "CHECKPOINT_CREATED",
                "RUN_STARTED",
                "RUN_RESUMED",
                "STAGE_STARTED",
                "STAGE_SUCCEEDED",
                "STAGE_FAILED",
                "STAGE_INTERRUPTED",
                "RUN_INTERRUPTED",
                "RUN_SUCCEEDED",
                "RUN_FAILED",
                "RESET",
            ),
        )


if __name__ == "__main__":
    unittest.main()
