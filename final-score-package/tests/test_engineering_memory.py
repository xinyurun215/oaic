import tempfile
import unittest
from pathlib import Path
from unittest import mock
import sys

sys.path.insert(0, str(Path(__file__).parent))

import test_recovery as support

main = support.main


class EngineeringMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "output"
        self.output.mkdir()

    def test_failure_fingerprint_is_stable_for_structurally_equal_errors(self):
        first = {"message": "Connection reset by peer", "attempt": 1}
        second = {"attempt": 9, "message": "Connection reset by peer"}
        self.assertEqual(main.failure_fingerprint(first), main.failure_fingerprint(second))

    def test_memory_supports_lifecycle_statuses_and_dedupes_same_run(self):
        source = "source-a"
        run = "run-a"
        error = {"message": "Connection reset by peer"}
        entry = main.remember_engineering_memory(self.output, source, "REQ-1", run,
                                                 error, "candidate")
        self.assertEqual(entry["status"], "candidate")
        self.assertFalse(main.should_skip_engineering_request(self.output, source, "REQ-1", run, error))
        self.assertFalse(main.should_skip_engineering_request(self.output, source, "REQ-1", "run-b", error))
        self.assertFalse(main.should_skip_engineering_request(self.output, "source-b", "REQ-1", run, error))
        for status in ("verified", "stale", "rejected"):
            updated = main.remember_engineering_memory(self.output, source, "REQ-1", run,
                                                       error, status)
            self.assertEqual(updated["status"], status)
            self.assertEqual(main.should_skip_engineering_request(self.output, source, "REQ-1", run, error),
                             status == "verified")

    def test_memory_read_failure_and_write_failure_degrade_to_no_memory(self):
        path = main.engineering_memory_path(self.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken", encoding="utf-8")
        self.assertEqual(main.load_engineering_memory(self.output), {"version": 1, "entries": []})
        path.unlink()
        with mock.patch.object(main, "atomic_json", side_effect=OSError("disk full")):
            self.assertIsNone(main.remember_engineering_memory(self.output, "source", "REQ-1", "run",
                                                               {"message": "x"}, "candidate"))
        self.assertFalse(path.exists())

    def test_memory_hints_are_source_bound_and_limited(self):
        for index in range(5):
            main.remember_engineering_memory(
                self.output, "source-a", "REQ-1", f"run-{index}",
                {"message": f"failure-{index}"}, "candidate",
                {"phase": "model", "message": f"failure-{index}"})
        main.remember_engineering_memory(self.output, "source-b", "REQ-1", "run-x",
                                         {"message": "other"}, "candidate")
        hints = main.engineering_memory_hints(self.output, "source-a", "REQ-1")
        self.assertEqual(len(hints), 3)
        self.assertTrue(all(item["source_sha256"] == "source-a" for item in hints))
        self.assertTrue(all("phase" in item and "verification_evidence" in item for item in hints))

    def test_verified_same_run_failure_skips_duplicate_model_request(self):
        harness = support.ModuleObservationFlowTests("test_first_pass_needs_no_repair")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        support.FakeCodex.failure = "REQ-2"
        support.FakeCodex.failure_message = "Connection reset by peer"
        self.assertEqual(harness.execute(), 0)
        self.assertEqual(len(support.FakeCodex.threads), 2)
        summary = harness.summary()
        self.assertTrue(any(item.get("duplicate_model_request") for item in summary["pending_issues"]))
        self.assertEqual(summary["engineering_memory"]["path"], ".arc/engineering/memory.json")
        self.assertIn("skipped", summary["engineering_memory"]["actions"])


if __name__ == "__main__":
    unittest.main()
