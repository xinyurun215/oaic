import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from racer.jsonio import artifact_json_bytes


ROOT = Path(__file__).parent.parent


class VerticalSliceE2ETests(unittest.TestCase):
    @staticmethod
    def _json_depth(value):
        if type(value) is list:
            return 1 + max((VerticalSliceE2ETests._json_depth(item) for item in value), default=0)
        if type(value) is dict:
            return 1 + max((VerticalSliceE2ETests._json_depth(item) for item in value.values()), default=0)
        return 0

    def _write_depth_job(self, root, depth, action="emit"):
        value = json.loads((ROOT / "tests" / "fixtures" / "valid_job.json").read_bytes())
        payload = "leaf"
        for _ in range(depth):
            payload = [payload]
        for stage in value["stages"]:
            stage["action"] = action
            stage["payload"] = payload
        job_path = root / "job.json"
        job_path.write_text(json.dumps(value), encoding="utf-8")
        return job_path, payload

    def test_depth_64_payload_survives_cli_interrupt_resume_trace(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            job_path, payload = self._write_depth_job(root, 64)
            state_dir = root / "state"
            self.assertEqual(self._json_depth(payload), 64)
            interrupted = self._failure_envelope(
                self._invoke("run", job_path, state_dir, "--interrupt-before", "verify"), 130
            )
            self.assertEqual(interrupted["result"]["completed_stage_ids"], ["prepare"])
            checkpoint = self._checkpoint(state_dir)
            self.assertEqual(checkpoint["stages"][0]["output"], {"emitted": payload})
            self.assertEqual(self._json_depth(checkpoint), 68)
            checkpoint_path = state_dir / "checkpoint.json"
            before = checkpoint_path.read_bytes()
            before_stat = checkpoint_path.stat()
            interrupted_trace = self._success_envelope(self._invoke("trace", job_path, state_dir))
            self.assertEqual(self._json_depth(interrupted_trace), 69)
            self.assertEqual(checkpoint_path.read_bytes(), before)
            self.assertEqual(checkpoint_path.stat().st_mtime_ns, before_stat.st_mtime_ns)

            resumed = self._success_envelope(self._invoke("resume", job_path, state_dir))
            self.assertEqual(resumed["result"]["durable_status"], "SUCCEEDED")
            checkpoint = self._checkpoint(state_dir)
            self.assertEqual([stage["attempts"] for stage in checkpoint["stages"]], [1, 2])
            self.assertEqual(self._json_depth(checkpoint), 68)
            for stage in checkpoint["stages"]:
                self.assertEqual(stage["output"], {"emitted": payload})
                self.assertEqual(self._json_depth(stage["output"]), 65)
            before = checkpoint_path.read_bytes()
            before_stat = checkpoint_path.stat()
            traced = self._success_envelope(self._invoke("trace", job_path, state_dir))
            self.assertEqual(self._json_depth(traced["result"]), 68)
            self.assertEqual(self._json_depth(traced), 69)
            self.assertEqual(traced["result"]["stage_states"], checkpoint["stages"])
            self.assertEqual(checkpoint_path.read_bytes(), before)
            self.assertEqual(checkpoint_path.stat().st_mtime_ns, before_stat.st_mtime_ns)
            self.assertEqual(tuple(state_dir.iterdir()), (checkpoint_path,))
            self._success_envelope(self._invoke("reset", job_path, state_dir))
            self._success_envelope(self._invoke("run", job_path, state_dir))

    def test_depth_64_fail_cli_keeps_explicit_failure_and_trace(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            job_path, payload = self._write_depth_job(root, 64, "fail")
            state_dir = root / "state"
            failed = self._failure_envelope(self._invoke("run", job_path, state_dir), 4)
            self.assertEqual(failed["error"]["type"], "StageFailedError")
            checkpoint = self._checkpoint(state_dir)
            self.assertEqual(checkpoint["stages"][0]["output"], {"error": payload})
            self.assertEqual(self._json_depth(checkpoint), 68)
            path = state_dir / "checkpoint.json"
            before = path.read_bytes()
            traced = self._success_envelope(self._invoke("trace", job_path, state_dir))
            self.assertEqual(traced["result"]["current_run_status"], "FAILED")
            self.assertEqual(self._json_depth(traced), 69)
            self.assertEqual(path.read_bytes(), before)

    def test_cli_rejects_payload_over_64_before_creating_state(self) -> None:
        for depth in (65, 66):
            with self.subTest(depth=depth):
                with TemporaryDirectory() as directory:
                    root = Path(directory)
                    job_path, _ = self._write_depth_job(root, depth)
                    state_dir = root / "missing-state"
                    completed = self._invoke("run", job_path, state_dir)
                    self.assertEqual((completed.returncode, completed.stdout), (2, ""))
                    envelope = json.loads(completed.stderr)
                    self.assertIsNone(envelope["result"])
                    self.assertEqual(envelope["error"]["type"], "JobValidationError")
                    self.assertFalse(state_dir.exists())
                    self.assertEqual(tuple(root.iterdir()), (job_path,))

    def test_interrupted_resume_trace_reset_run(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job_path = root / "job.json"
            state_dir = root / "state"
            job_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "job_id": "e2e_job",
                        "requirements": [{"requirement_id": "req", "text": "E2E"}],
                        "interfaces": [
                            {
                                "interface_id": "api",
                                "name": "E2E API",
                                "requirement_ids": ["req"],
                            }
                        ],
                        "tests": [
                            {
                                "test_id": "test",
                                "name": "E2E test",
                                "requirement_ids": ["req"],
                                "interface_ids": ["api"],
                            }
                        ],
                        "stages": [
                            {
                                "stage_id": stage_id,
                                "action": "emit",
                                "payload": {"stage": stage_id},
                                "requirement_ids": ["req"],
                                "interface_ids": ["api"],
                                "test_ids": ["test"],
                            }
                            for stage_id in ("first", "second", "third")
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            interrupted = self._invoke(
                "run",
                job_path,
                state_dir,
                "--interrupt-before",
                "second",
            )
            interrupted_payload = self._failure_envelope(interrupted, 130)
            self.assertEqual(interrupted_payload["result"]["status"], "INTERRUPTED")
            self.assertEqual(interrupted_payload["result"]["durable_status"], "RUNNING")
            interrupted_checkpoint = self._checkpoint(state_dir)
            self.assertEqual(interrupted_checkpoint["run_status"], "RUNNING")
            self.assertEqual(
                [(item["status"], item["attempts"]) for item in interrupted_checkpoint["stages"]],
                [("SUCCEEDED", 1), ("RUNNING", 1), ("PENDING", 0)],
            )
            self.assertEqual(
                [item["kind"] for item in interrupted_checkpoint["events"]],
                [
                    "checkpoint_created",
                    "run_started",
                    "stage_started",
                    "stage_succeeded",
                    "stage_started",
                ],
            )
            self.assertNotIn(
                "stage_interrupted",
                [item["kind"] for item in interrupted_checkpoint["events"]],
            )
            self.assertNotIn(
                "run_interrupted",
                [item["kind"] for item in interrupted_checkpoint["events"]],
            )

            resumed = self._invoke("resume", job_path, state_dir)
            resumed_payload = self._success_envelope(resumed)
            self.assertTrue(resumed_payload["result"]["resumed"])
            self.assertEqual(resumed_payload["result"]["generation"], 0)
            resumed_checkpoint = self._checkpoint(state_dir)
            self.assertEqual(resumed_checkpoint["run_status"], "SUCCEEDED")
            self.assertEqual(
                [(item["status"], item["attempts"]) for item in resumed_checkpoint["stages"]],
                [("SUCCEEDED", 1), ("SUCCEEDED", 2), ("SUCCEEDED", 1)],
            )
            self.assertEqual(
                [item["kind"] for item in resumed_checkpoint["events"]],
                [
                    "checkpoint_created",
                    "run_started",
                    "stage_started",
                    "stage_succeeded",
                    "stage_started",
                    "stage_interrupted",
                    "run_interrupted",
                    "run_resumed",
                    "stage_started",
                    "stage_succeeded",
                    "stage_started",
                    "stage_succeeded",
                    "run_succeeded",
                ],
            )
            self.assertEqual(
                [item["sequence"] for item in resumed_checkpoint["events"]],
                list(range(13)),
            )
            self.assertEqual(resumed_checkpoint["revision"], 10)

            checkpoint_path = state_dir / "checkpoint.json"
            trace_before = checkpoint_path.read_bytes()
            trace_before_stat = checkpoint_path.stat()
            trace_before_revision = resumed_checkpoint["revision"]
            trace_before_generation = resumed_checkpoint["generation"]
            trace_before_events = resumed_checkpoint["events"]
            trace_before_stages = resumed_checkpoint["stages"]
            traced = self._invoke("trace", job_path, state_dir)
            trace_payload = self._success_envelope(traced)
            self.assertEqual(trace_payload["command"], "trace")
            self.assertEqual(
                trace_payload["result"],
                {
                    "schema_version": resumed_checkpoint["schema_version"],
                    "job_id": resumed_checkpoint["job_id"],
                    "job_fingerprint": resumed_checkpoint["job_fingerprint"],
                    "current_generation": resumed_checkpoint["generation"],
                    "current_run_id": resumed_checkpoint["run_id"],
                    "current_revision": resumed_checkpoint["revision"],
                    "current_run_status": resumed_checkpoint["run_status"],
                    "stage_states": resumed_checkpoint["stages"],
                    "traceability": resumed_checkpoint["traceability"],
                    "events": resumed_checkpoint["events"],
                },
            )
            self.assertEqual(checkpoint_path.read_bytes(), trace_before)
            trace_after = self._checkpoint(state_dir)
            self.assertEqual(trace_after["revision"], trace_before_revision)
            self.assertEqual(trace_after["generation"], trace_before_generation)
            self.assertEqual(trace_after["events"], trace_before_events)
            self.assertEqual(trace_after["stages"], trace_before_stages)
            self.assertEqual(tuple(state_dir.iterdir()), (checkpoint_path,))
            self.assertEqual(checkpoint_path.stat().st_size, trace_before_stat.st_size)
            self.assertEqual(checkpoint_path.stat().st_mtime_ns, trace_before_stat.st_mtime_ns)

            reset = self._invoke("reset", job_path, state_dir)
            reset_payload = self._success_envelope(reset)
            self.assertEqual(reset_payload["result"]["generation"], 1)
            reset_checkpoint = self._checkpoint(state_dir)
            self.assertEqual((reset_checkpoint["generation"], reset_checkpoint["run_id"], reset_checkpoint["run_status"]), (1, "e2e_job:1", "READY"))
            self.assertEqual(
                [(item["status"], item["attempts"], item["output"]) for item in reset_checkpoint["stages"]],
                [("PENDING", 0, None), ("PENDING", 0, None), ("PENDING", 0, None)],
            )
            self.assertEqual(reset_checkpoint["events"][: len(trace_before_events)], trace_before_events)
            self.assertEqual(
                reset_checkpoint["events"][-1],
                {
                    "event_id": "e2e_job:1:000000",
                    "generation": 1,
                    "sequence": 0,
                    "kind": "reset",
                    "recorded_at": reset_checkpoint["events"][-1]["recorded_at"],
                    "run_status": "READY",
                    "stage_id": None,
                    "stage_status": None,
                    "attempt": None,
                    "exit_code": None,
                    "error_type": None,
                    "error_message": None,
                    "previous_generation": 0,
                    "previous_run_status": "SUCCEEDED",
                },
            )

            rerun = self._invoke("run", job_path, state_dir)
            rerun_payload = self._success_envelope(rerun)
            self.assertFalse(rerun_payload["result"]["resumed"])
            self.assertEqual(rerun_payload["result"]["generation"], 1)
            final_checkpoint = self._checkpoint(state_dir)
            self.assertEqual(final_checkpoint["run_status"], "SUCCEEDED")
            self.assertEqual(final_checkpoint["generation"], 1)
            self.assertTrue(all(item["status"] == "SUCCEEDED" for item in final_checkpoint["stages"]))
            self.assertEqual(final_checkpoint["events"][: len(trace_before_events)], trace_before_events)
            self.assertEqual(final_checkpoint["events"][len(trace_before_events)]["kind"], "reset")

    def _invoke(self, command: str, job_path: Path, state_dir: Path, *extra: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "racer",
                command,
                "--job",
                str(job_path),
                "--state-dir",
                str(state_dir),
                *extra,
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

    def _success_envelope(self, completed: subprocess.CompletedProcess[str]) -> dict[str, object]:
        self.assertEqual((completed.returncode, completed.stderr), (0, ""))
        self.assertEqual(completed.stdout.encode("utf-8"), artifact_json_bytes(json.loads(completed.stdout)))
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["error"])
        self.assertEqual(payload["exit_code"], 0)
        self.assertIsNotNone(payload["result"])
        return payload

    def _failure_envelope(self, completed: subprocess.CompletedProcess[str], code: int) -> dict[str, object]:
        self.assertEqual((completed.returncode, completed.stdout), (code, ""))
        self.assertEqual(completed.stderr.encode("utf-8"), artifact_json_bytes(json.loads(completed.stderr)))
        payload = json.loads(completed.stderr)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["exit_code"], code)
        self.assertIsNotNone(payload["error"])
        self.assertIsNotNone(payload["result"])
        return payload

    def _checkpoint(self, state_dir: Path) -> dict[str, object]:
        return json.loads((state_dir / "checkpoint.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
