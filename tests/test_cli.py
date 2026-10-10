import ast
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from racer import cli as cli_module
from racer.checkpoint import JsonCheckpointStore
from racer.errors import CheckpointCommitError, SimulatedInterruption, StageFailedError
from racer.jsonio import artifact_json_bytes
from racer.runner import Runner
from racer.state import RunStatus, StageResult


FIXTURES = Path(__file__).parent / "fixtures"


class CliTests(unittest.TestCase):
    def test_cli_error_output_matches_locked_contract(self) -> None:
        self._assert_locked_unknown_command_error()

    def test_cli_error_output_matches_the_locked_contract(self) -> None:
        self._assert_locked_unknown_command_error()

    def test_cli_success_uses_stdout_only(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job_path = self._success_job(root)

            exit_code, stdout, stderr = self._invoke(
                ["run", "--job", str(job_path), "--state-dir", str(root / "state")]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertTrue(stdout.endswith("\n"))
        self.assertTrue(json.loads(stdout)["ok"])

    def test_cli_error_uses_stderr_only(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            exit_code, stdout, stderr = self._invoke(
                [
                    "resume",
                    "--job",
                    str(FIXTURES / "valid_job.json"),
                    "--state-dir",
                    str(root / "missing"),
                ]
            )
            state_created = (root / "missing").exists()

        self.assertEqual(exit_code, 3)
        self.assertEqual(stdout, "")
        self.assertFalse(json.loads(stderr)["ok"])
        self.assertFalse(state_created)

    def test_real_keyboard_interrupt_returns_canonical_130_envelope_without_result(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job_path = self._success_job(root)
            with patch.object(cli_module.Runner, "run", side_effect=KeyboardInterrupt):
                exit_code, stdout, stderr = self._invoke(
                    ["run", "--job", str(job_path), "--state-dir", str(root / "state")]
                )

        self.assertEqual((exit_code, stdout), (130, ""))
        self.assertEqual(
            json.loads(stderr),
            {
                "command": "run",
                "error": {"type": "KeyboardInterrupt", "message": "interrupted"},
                "exit_code": 130,
                "ok": False,
                "result": None,
            },
        )

    def test_cli_argument_error_is_canonical_json(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cases = (
                ([], None),
                (["unknown"], None),
                (["run"], "run"),
                (["resume", "--job", "job.json"], "resume"),
                (["reset", "--job", "job.json", "--state-dir", "state", "--interrupt-before", "x"], "reset"),
                (["trace", "--job", "job.json", "--state-dir", "state", "--interrupt-before", "x"], "trace"),
                (["run", "--unknown"], "run"),
            )
            for argv, command in cases:
                with self.subTest(argv=argv):
                    exit_code, stdout, stderr = self._invoke(argv)
                    payload = json.loads(stderr)
                    self.assertEqual(exit_code, 2)
                    self.assertEqual(stdout, "")
                    self.assertEqual(payload["command"], command)
                    self.assertEqual(payload["error"]["type"], "CliArgumentError")
                    self.assertNotIn("usage:", stderr)
            self.assertEqual(tuple(root.iterdir()), ())

    def test_cli_exit_codes(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job_path = self._success_job(root)
            cases = (
                (
                    ["run", "--job", str(job_path), "--state-dir", str(root / "ok")],
                    0,
                ),
                (["unknown"], 2),
                (
                    ["resume", "--job", str(job_path), "--state-dir", str(root / "missing")],
                    3,
                ),
                (
                    ["run", "--job", str(FIXTURES / "failing_job.json"), "--state-dir", str(root / "failed")],
                    4,
                ),
                (
                    ["run", "--job", str(job_path), "--state-dir", str(root / "interrupted"), "--interrupt-before", "prepare"],
                    130,
                ),
            )
            for argv, expected_exit_code in cases:
                with self.subTest(expected_exit_code=expected_exit_code):
                    exit_code, stdout, stderr = self._invoke(argv)
                    payload = json.loads(stdout or stderr)
                    self.assertEqual(exit_code, expected_exit_code)
                    self.assertEqual(payload["exit_code"], expected_exit_code)
            with patch(
                "racer.cli.FixtureStageExecutor",
                return_value=self._RaisingExecutor(),
            ):
                exit_code, stdout, stderr = self._invoke(
                    ["run", "--job", str(job_path), "--state-dir", str(root / "internal")]
                )
            self.assertEqual(exit_code, 70)
            self.assertEqual(stdout, "")
            self.assertEqual(json.loads(stderr)["exit_code"], 70)

    def test_cli_run_resume_and_reset_succeed(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job_path = self._success_job(root)
            state_dir = root / "state"

            interrupted, _, _ = self._invoke(
                ["run", "--job", str(job_path), "--state-dir", str(state_dir), "--interrupt-before", "prepare"]
            )
            resumed, resume_stdout, resume_stderr = self._invoke(
                ["resume", "--job", str(job_path), "--state-dir", str(state_dir)]
            )
            reset, reset_stdout, reset_stderr = self._invoke(
                ["reset", "--job", str(job_path), "--state-dir", str(state_dir)]
            )
            run, run_stdout, run_stderr = self._invoke(
                ["run", "--job", str(job_path), "--state-dir", str(state_dir)]
            )

        self.assertEqual((interrupted, resumed, reset, run), (130, 0, 0, 0))
        self.assertEqual((resume_stderr, reset_stderr, run_stderr), ("", "", ""))
        self.assertTrue(json.loads(resume_stdout)["result"]["resumed"])
        self.assertEqual(json.loads(reset_stdout)["result"]["status"], "READY")
        self.assertEqual(json.loads(run_stdout)["result"]["status"], "SUCCEEDED")

    def test_cli_serializes_exact_run_result_and_canonical_unicode(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job_path = self._success_job(root)
            exit_code, stdout, stderr = self._invoke(
                ["run", "--job", str(job_path), "--state-dir", str(root / "state")]
            )
            unknown_exit_code, unknown_stdout, unknown_stderr = self._invoke(
                ["run", "--job", str(job_path), "--state-dir", str(root / "unknown"), "--interrupt-before", "雪"]
            )

        payload = json.loads(stdout)
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(list(payload), ["command", "error", "exit_code", "ok", "result"])
        self.assertEqual(
            set(payload["result"]),
            {
                "job_id",
                "run_id",
                "generation",
                "status",
                "durable_status",
                "completed_stage_ids",
                "failed_stage_id",
                "exit_code",
                "resumed",
                "checkpoint_revision",
            },
        )
        self.assertEqual(type(payload["result"]["exit_code"]), int)
        self.assertEqual(type(payload["result"]["completed_stage_ids"]), list)
        self.assertEqual(type(payload["result"]["resumed"]), bool)
        self.assertEqual(stdout.count("\n"), 1)
        self.assertEqual(unknown_exit_code, 2)
        self.assertEqual(unknown_stdout, "")
        self.assertIn("雪", unknown_stderr)
        self.assertNotIn("\\u96ea", unknown_stderr)

    def test_cli_persists_stage_and_internal_failure_truth(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            stage_exit_code, stage_stdout, stage_stderr = self._invoke(
                ["run", "--job", str(FIXTURES / "failing_job.json"), "--state-dir", str(root / "failed")]
            )
            job_path = self._success_job(root)
            with patch(
                "racer.cli.FixtureStageExecutor",
                return_value=self._RaisingExecutor(),
            ):
                internal_exit_code, internal_stdout, internal_stderr = self._invoke(
                    ["run", "--job", str(job_path), "--state-dir", str(root / "internal")]
                )

        stage_payload = json.loads(stage_stderr)
        internal_payload = json.loads(internal_stderr)
        self.assertEqual((stage_exit_code, stage_stdout), (4, ""))
        self.assertEqual(stage_payload["error"], {
            "type": "StageFailedError",
            "message": "stage 'second' failed with executor exit code 1",
        })
        self.assertIsNotNone(stage_payload["result"])
        self.assertEqual(stage_payload["result"]["durable_status"], "FAILED")
        self.assertEqual((internal_exit_code, internal_stdout), (70, ""))
        self.assertEqual(internal_payload["error"], {
            "type": "RuntimeError",
            "message": "executor exploded",
        })
        self.assertIsNotNone(internal_payload["result"])
        self.assertEqual(internal_payload["result"]["durable_status"], "FAILED")

    def test_cli_persists_executor_contract_failure_truth(self) -> None:
        class WrongStageExecutor:
            def execute(self, spec, context):
                return StageResult("wrong", 0, None)

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job_path = self._success_job(root)
            with patch(
                "racer.cli.FixtureStageExecutor",
                return_value=WrongStageExecutor(),
            ):
                exit_code, stdout, stderr = self._invoke(
                    ["run", "--job", str(job_path), "--state-dir", str(root / "state")]
                )

        payload = json.loads(stderr)
        self.assertEqual((exit_code, stdout), (70, ""))
        self.assertEqual(payload["error"]["type"], "ExecutorContractError")
        self.assertIsNotNone(payload["result"])

    def test_cli_executor_thrown_stage_failed_error_is_internal_exit_70(self) -> None:
        class Executor:
            def execute(self, spec, context):
                raise StageFailedError(spec.stage_id, 9)

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job_path = self._success_job(root)
            with patch("racer.cli.FixtureStageExecutor", return_value=Executor()):
                exit_code, stdout, stderr = self._invoke(
                    ["run", "--job", str(job_path), "--state-dir", str(root / "state")]
                )

        payload = json.loads(stderr)
        self.assertEqual((exit_code, stdout), (70, ""))
        self.assertEqual(payload["error"], {
            "type": "StageFailedError",
            "message": "stage 'prepare' failed with executor exit code 9",
        })
        self.assertEqual(payload["result"]["exit_code"], 70)
        self.assertEqual(payload["result"]["durable_status"], "FAILED")

    def test_cli_generic_exception_is_internal_error_with_no_result(self) -> None:
        with patch("racer.cli.load_job", side_effect=RuntimeError("loader exploded")):
            exit_code, stdout, stderr = self._invoke(
                ["run", "--job", "job.json", "--state-dir", "state"]
            )

        payload = json.loads(stderr)
        self.assertEqual((exit_code, stdout), (70, ""))
        self.assertEqual(payload["error"], {
            "type": "RuntimeError",
            "message": "loader exploded",
        })
        self.assertIsNone(payload["result"])

    def test_cli_commit_failure_has_no_result(self) -> None:
        class FailureCommitStore(JsonCheckpointStore):
            def commit(self, checkpoint, *, expected_revision):
                if checkpoint.run_status is RunStatus.FAILED:
                    raise CheckpointCommitError("injected commit failure")
                return super().commit(checkpoint, expected_revision=expected_revision)

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with patch("racer.cli.JsonCheckpointStore", FailureCommitStore):
                exit_code, stdout, stderr = self._invoke(
                    ["run", "--job", str(FIXTURES / "failing_job.json"), "--state-dir", str(root / "state")]
                )

        payload = json.loads(stderr)
        self.assertEqual((exit_code, stdout), (70, ""))
        self.assertEqual(payload["error"]["type"], "CheckpointCommitError")
        self.assertIsNone(payload["result"])

    def test_cli_interruption_keeps_result_and_uses_locked_error(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job_path = self._success_job(root)
            exit_code, stdout, stderr = self._invoke(
                ["run", "--job", str(job_path), "--state-dir", str(root / "state"), "--interrupt-before", "prepare"]
            )

        payload = json.loads(stderr)
        self.assertEqual((exit_code, stdout), (130, ""))
        self.assertEqual(payload["error"], {
            "type": "SimulatedInterruption",
            "message": "interrupted before stage 'prepare'",
        })
        self.assertEqual(payload["result"]["status"], "INTERRUPTED")
        self.assertEqual(payload["result"]["durable_status"], "RUNNING")

    def test_cli_rejected_commands_preserve_checkpoint_bytes_and_revision(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job_path = self._success_job(root)
            state_dir = root / "state"
            self._invoke(
                ["run", "--job", str(job_path), "--state-dir", str(state_dir), "--interrupt-before", "prepare"]
            )
            checkpoint_path = state_dir / "checkpoint.json"
            before = checkpoint_path.read_bytes()
            revision = JsonCheckpointStore(state_dir, cli_module.load_job(job_path)).load().revision
            exit_code, stdout, stderr = self._invoke(
                ["run", "--job", str(job_path), "--state-dir", str(state_dir)]
            )
            bytes_unchanged = checkpoint_path.read_bytes() == before
            revision_unchanged = (
                JsonCheckpointStore(
                    state_dir,
                    cli_module.load_job(job_path),
                ).load().revision
                == revision
            )

        self.assertEqual((exit_code, stdout), (3, ""))
        self.assertEqual(json.loads(stderr)["error"]["type"], "InvalidStateError")
        self.assertTrue(bytes_unchanged)
        self.assertTrue(revision_unchanged)

    def test_cli_validation_and_unknown_stage_do_not_create_state(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            invalid_state = root / "invalid"
            unknown_state = root / "unknown"
            invalid_exit_code, invalid_stdout, invalid_stderr = self._invoke(
                ["run", "--job", str(FIXTURES / "invalid_job.json"), "--state-dir", str(invalid_state)]
            )
            unknown_exit_code, unknown_stdout, unknown_stderr = self._invoke(
                ["run", "--job", str(FIXTURES / "valid_job.json"), "--state-dir", str(unknown_state), "--interrupt-before", "missing"]
            )

        self.assertEqual((invalid_exit_code, invalid_stdout), (2, ""))
        self.assertEqual((unknown_exit_code, unknown_stdout), (2, ""))
        self.assertEqual(json.loads(invalid_stderr)["error"]["type"], "JobValidationError")
        self.assertEqual(json.loads(unknown_stderr)["error"]["type"], "UnknownInterruptStageError")
        self.assertFalse(invalid_state.exists())
        self.assertFalse(unknown_state.exists())

    def test_cli_lone_surrogate_job_is_invalid_input_and_creates_no_state(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            value = json.loads((FIXTURES / "valid_job.json").read_text(encoding="utf-8"))
            value["requirements"][0]["text"] = "\ud800"
            job_path = root / "lone-surrogate-job.json"
            job_path.write_bytes(json.dumps(value).encode("utf-8"))
            state_dir = root / "state"
            exit_code, stdout, stderr = self._invoke(
                ["run", "--job", str(job_path), "--state-dir", str(state_dir)]
            )

        self.assertEqual((exit_code, stdout), (2, ""))
        self.assertEqual(json.loads(stderr)["error"]["type"], "JobValidationError")
        self.assertFalse(state_dir.exists())

    def test_cli_lone_surrogate_checkpoint_is_invalid_state_and_zero_write(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job_path = self._success_job(root)
            state_dir = root / "state"
            self._invoke(["run", "--job", str(job_path), "--state-dir", str(state_dir)])
            checkpoint_path = state_dir / "checkpoint.json"
            value = json.loads(checkpoint_path.read_bytes())
            value["stages"][0]["output"] = {"text": "\ud800"}
            checkpoint_path.write_bytes(json.dumps(value).encode("utf-8"))
            before = checkpoint_path.read_bytes()
            exit_code, stdout, stderr = self._invoke(
                ["trace", "--job", str(job_path), "--state-dir", str(state_dir)]
            )
            after = checkpoint_path.read_bytes()

        self.assertEqual((exit_code, stdout), (3, ""))
        self.assertEqual(json.loads(stderr)["error"]["type"], "CheckpointFormatError")
        self.assertEqual(after, before)

    def test_cli_fingerprint_and_format_rejections_preserve_checkpoint_bytes(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job_path = self._success_job(root)
            state_dir = root / "state"
            self._invoke(["run", "--job", str(job_path), "--state-dir", str(state_dir)])
            checkpoint_path = state_dir / "checkpoint.json"
            fingerprint_before = checkpoint_path.read_bytes()
            changed_directory = root / "changed"
            changed_directory.mkdir()
            changed_job_path = self._success_job(changed_directory)
            changed_value = json.loads(changed_job_path.read_text(encoding="utf-8"))
            changed_value["job_id"] = "changed_job"
            changed_job_path.write_text(
                json.dumps(changed_value, ensure_ascii=False),
                encoding="utf-8",
            )
            fingerprint_exit_code, fingerprint_stdout, fingerprint_stderr = self._invoke(
                ["run", "--job", str(changed_job_path), "--state-dir", str(state_dir)]
            )
            fingerprint_unchanged = checkpoint_path.read_bytes() == fingerprint_before
            checkpoint_path.write_bytes(b"{broken")
            format_before = checkpoint_path.read_bytes()
            format_exit_code, format_stdout, format_stderr = self._invoke(
                ["resume", "--job", str(job_path), "--state-dir", str(state_dir)]
            )
            format_unchanged = checkpoint_path.read_bytes() == format_before

        self.assertEqual((fingerprint_exit_code, fingerprint_stdout), (3, ""))
        self.assertEqual(
            json.loads(fingerprint_stderr)["error"]["type"],
            "CheckpointFingerprintMismatchError",
        )
        self.assertTrue(fingerprint_unchanged)
        self.assertEqual((format_exit_code, format_stdout), (3, ""))
        self.assertEqual(
            json.loads(format_stderr)["error"]["type"],
            "CheckpointFormatError",
        )
        self.assertTrue(format_unchanged)

    def test_cli_reset_missing_checkpoint_is_zero_write_state_error(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state_dir = root / "missing"
            exit_code, stdout, stderr = self._invoke(
                ["reset", "--job", str(FIXTURES / "valid_job.json"), "--state-dir", str(state_dir)]
            )
            state_created = state_dir.exists()

        self.assertEqual((exit_code, stdout), (3, ""))
        self.assertEqual(json.loads(stderr)["error"]["type"], "CheckpointNotFoundError")
        self.assertFalse(state_created)

    def test_cli_help_is_standard_and_zero_write(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for argv in (["--help"], ["run", "--help"], ["trace", "--help"]):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with self.subTest(argv=argv), redirect_stdout(stdout), redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as raised:
                        cli_module.main(argv)
                self.assertEqual(raised.exception.code, 0)
                self.assertIn("usage:", stdout.getvalue())
                self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(tuple(root.iterdir()), ())

    def test_python_module_entry_is_narrow(self) -> None:
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            [sys.executable, "-B", "-m", "racer", "unknown"],
            cwd=Path(__file__).parent.parent,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(json.loads(completed.stderr)["command"], None)

    def test_cli_only_dispatches_runner_public_methods_and_has_no_task9(self) -> None:
        class RecordingInterruption(SimulatedInterruption):
            messages: list[str] = []

            def __init__(self, message: str) -> None:
                super().__init__(message)
                self.messages.append(message)

        source = Path(cli_module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        runner_calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "runner"
        }

        self.assertEqual(runner_calls, {"run", "resume", "reset", "trace"})
        self.assertIn("trace", source)
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job_path = self._success_job(root)
            with patch.object(
                cli_module,
                "SimulatedInterruption",
                RecordingInterruption,
                create=True,
            ):
                exit_code, stdout, stderr = self._invoke(
                    [
                        "run",
                        "--job",
                        str(job_path),
                        "--state-dir",
                        str(root / "state"),
                        "--interrupt-before",
                        "prepare",
                    ]
                )

        self.assertEqual((exit_code, stdout), (130, ""))
        self.assertEqual(
            RecordingInterruption.messages,
            ["interrupted before stage 'prepare'"],
        )
        self.assertEqual(
            json.loads(stderr)["error"],
            {
                "type": "SimulatedInterruption",
                "message": "interrupted before stage 'prepare'",
            },
        )

    def _assert_locked_unknown_command_error(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = cli_module.main(["unknown"])

        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            stderr.getvalue(),
            '{"command":null,"error":{"message":"argument command: invalid '
            "choice: 'unknown' (choose from run, resume, reset, trace)"
            '","type":"CliArgumentError"},"exit_code":2,"ok":false,'
            '"result":null}\n',
        )

    def test_trace_is_read_only_and_preserves_revision(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job_path = self._success_job(root)
            state_dir = root / "state"
            self._invoke(["run", "--job", str(job_path), "--state-dir", str(state_dir)])
            checkpoint_path = state_dir / "checkpoint.json"
            before = checkpoint_path.read_bytes()
            checkpoint = JsonCheckpointStore(state_dir, cli_module.load_job(job_path)).load()

            with patch(
                "racer.cli.FixtureStageExecutor",
                side_effect=AssertionError("trace constructed an executor"),
            ), patch(
                "racer.cli.SystemClock",
                side_effect=AssertionError("trace constructed a clock"),
            ):
                exit_code, stdout, stderr = self._invoke(
                    ["trace", "--job", str(job_path), "--state-dir", str(state_dir)]
                )
            after = checkpoint_path.read_bytes()
            loaded_after = JsonCheckpointStore(state_dir, cli_module.load_job(job_path)).load()

            class ReadOnlyStore:
                def load(self):
                    return checkpoint

                def __getattr__(self, name):
                    raise AssertionError(f"trace attempted store.{name}")

            class ExplodingExecutor:
                def execute(self, spec, context):
                    raise AssertionError("trace executed a stage")

            class ExplodingClock:
                def now_utc(self):
                    raise AssertionError("trace read the clock")

            document = Runner(
                cli_module.load_job(job_path),
                ReadOnlyStore(),
                ExplodingExecutor(),
                ExplodingClock(),
            ).trace()

            missing = root / "missing"
            missing_exit, missing_stdout, missing_stderr = self._invoke(
                ["trace", "--job", str(job_path), "--state-dir", str(missing)]
            )
            invalid_state = root / "invalid"
            invalid_exit, invalid_stdout, invalid_stderr = self._invoke(
                ["trace", "--job", str(FIXTURES / "invalid_job.json"), "--state-dir", str(invalid_state)]
            )
            missing_created = missing.exists()
            invalid_created = invalid_state.exists()
            changed_job = root / "changed_job.json"
            changed_value = json.loads(job_path.read_text(encoding="utf-8"))
            changed_value["job_id"] = "other"
            changed_job.write_text(json.dumps(changed_value), encoding="utf-8")
            mismatch_exit, mismatch_stdout, mismatch_stderr = self._invoke(
                ["trace", "--job", str(changed_job), "--state-dir", str(state_dir)]
            )
            mismatch_after = checkpoint_path.read_bytes()
            illegal_dir = root / "illegal"
            illegal_dir.mkdir()
            illegal_value = json.loads(before)
            illegal_value["stages"][0]["status"] = "PENDING"
            illegal_path = illegal_dir / "checkpoint.json"
            illegal_path.write_bytes(artifact_json_bytes(illegal_value))
            illegal_before = illegal_path.read_bytes()
            illegal_exit, illegal_stdout, illegal_stderr = self._invoke(
                ["trace", "--job", str(job_path), "--state-dir", str(illegal_dir)]
            )
            illegal_after = illegal_path.read_bytes()
            checkpoint_path.write_bytes(b"{broken")
            corrupt_before = checkpoint_path.read_bytes()
            corrupt_exit, corrupt_stdout, corrupt_stderr = self._invoke(
                ["trace", "--job", str(job_path), "--state-dir", str(state_dir)]
            )
            corrupt_after = checkpoint_path.read_bytes()

        self.assertEqual((exit_code, stderr), (0, ""))
        payload = json.loads(stdout)
        self.assertEqual(before, after)
        self.assertEqual(loaded_after.revision, checkpoint.revision)
        self.assertEqual(loaded_after.generation, checkpoint.generation)
        self.assertEqual(loaded_after.events, checkpoint.events)
        self.assertEqual(loaded_after.stages, checkpoint.stages)
        self.assertEqual(document.current_revision, checkpoint.revision)
        self.assertEqual(document.events, checkpoint.events)
        self.assertEqual(payload["result"]["current_revision"], checkpoint.revision)
        self.assertEqual((missing_exit, missing_stdout), (3, ""))
        self.assertEqual(json.loads(missing_stderr)["error"]["type"], "CheckpointNotFoundError")
        self.assertFalse(missing_created)
        self.assertEqual((invalid_exit, invalid_stdout), (2, ""))
        self.assertEqual(json.loads(invalid_stderr)["error"]["type"], "JobValidationError")
        self.assertFalse(invalid_created)
        self.assertEqual((mismatch_exit, mismatch_stdout), (3, ""))
        self.assertEqual(json.loads(mismatch_stderr)["error"]["type"], "CheckpointFingerprintMismatchError")
        self.assertEqual(mismatch_after, before)
        self.assertEqual((illegal_exit, illegal_stdout), (3, ""))
        self.assertEqual(json.loads(illegal_stderr)["error"]["type"], "CheckpointFormatError")
        self.assertEqual(illegal_after, illegal_before)
        self.assertEqual((corrupt_exit, corrupt_stdout), (3, ""))
        self.assertEqual(json.loads(corrupt_stderr)["error"]["type"], "CheckpointFormatError")
        self.assertEqual(corrupt_after, corrupt_before)

    def test_trace_output_is_canonical_json(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job_path = self._success_job(root)
            state_dir = root / "state"
            self._invoke(["run", "--job", str(job_path), "--state-dir", str(state_dir)])
            exit_code, stdout, stderr = self._invoke(
                ["trace", "--job", str(job_path), "--state-dir", str(state_dir)]
            )

        self.assertEqual((exit_code, stderr), (0, ""))
        payload = json.loads(stdout)
        result = payload["result"]
        self.assertEqual(stdout.encode("utf-8"), artifact_json_bytes(payload))
        self.assertEqual(set(payload), {"command", "error", "exit_code", "ok", "result"})
        self.assertEqual(payload["command"], "trace")
        self.assertEqual(payload["error"], None)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["exit_code"], 0)
        self.assertEqual(
            set(result),
            {
                "schema_version",
                "job_id",
                "job_fingerprint",
                "current_generation",
                "current_run_id",
                "current_revision",
                "current_run_status",
                "stage_states",
                "traceability",
                "events",
            },
        )
        self.assertTrue(all(set(stage) == {"stage_id", "status", "attempts", "exit_code", "output"} for stage in result["stage_states"]))
        self.assertTrue(
            all(
                set(event)
                == {
                    "event_id",
                    "generation",
                    "sequence",
                    "kind",
                    "recorded_at",
                    "run_status",
                    "stage_id",
                    "stage_status",
                    "attempt",
                    "exit_code",
                    "error_type",
                    "error_message",
                    "previous_generation",
                    "previous_run_status",
                }
                for event in result["events"]
            )
        )
        self.assertIsNone(result["events"][0]["stage_id"])
        self.assertIsNone(result["events"][0]["previous_generation"])

    def _invoke(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = cli_module.main(argv)
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def _success_job(self, directory: Path) -> Path:
        value = json.loads((FIXTURES / "valid_job.json").read_text(encoding="utf-8"))
        value["stages"][1]["action"] = "emit"
        path = directory / "success_job.json"
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    class _RaisingExecutor:
        def execute(self, spec, context):
            raise RuntimeError("executor exploded")


if __name__ == "__main__":
    unittest.main()
