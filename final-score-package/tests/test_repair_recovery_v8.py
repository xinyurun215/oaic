import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_final_repair_flow as final_flow
import test_recovery as support

main = support.main


class RepairRecoveryContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.requirements = self.root / "requirements"
        self.requirements.mkdir()
        (self.requirements / "requirements.yaml").write_text(
            json.dumps({"id": "ROOT", "children": [{"id": "REQ-1", "children": [
                {"id": "REQ-1-1", "type": "ATOMIC", "description": "rule"}]}]}),
            encoding="utf-8")
        self.output = self.root / "output"
        (self.output / "frontend/src").mkdir(parents=True)
        (self.output / "backend").mkdir(parents=True)
        (self.output / "frontend/src/App.tsx").write_text("valid", encoding="utf-8")
        self.module = main.load_root_modules(self.requirements)[0]

    def test_repair_retry_hard_timeout_is_1800_seconds(self):
        self.assertEqual(main.model_turn_limits("observation_repair", retry=True), (300.0, 1800.0))

    def test_invalid_observation_script_is_separate_from_environment_failure(self):
        self.assertEqual(
            main.classify_observation_failure({
                "status": "error",
                "error": "Missing observation script: module-observations/REQ-1/checks.spec.js",
            }),
            "invalid_observation_script",
        )

    def test_missing_checks_spec_is_recorded_as_invalid_observation_script(self):
        observed = main.run_module_observation(self.output, self.module, "early")
        self.assertEqual(observed["status"], "error")
        self.assertEqual(main.classify_observation_failure(observed), "invalid_observation_script")
        self.assertIn("checks.spec.js", observed["configuration_error"])

    def test_timeout_restore_keeps_independent_attempt_checkpoints_and_logs_restore(self):
        failures = [{"module_id": "REQ-1", "atomic_id": "REQ-1-1",
                     "error_excerpt": "assertion failed"}]
        calls = []
        repair = [main.ModelTurnTimeout(1800.0), None]

        def fake_repair(*_args, **_kwargs):
            calls.append("repair")
            error = repair.pop(0)
            if error:
                raise error

        def build(_output):
            calls.append("build")
            return {"status": "passed"}

        def backend(_output):
            calls.append("backend")
            return {"status": "passed"}

        def observe(_output, _module, _phase):
            calls.append("observation")
            return {"status": "passed", "tests": [], "evidence_path": "module-observations/REQ-1/early"}

        with patch.object(main, "repair_module_batch", side_effect=fake_repair), \
             patch.object(main, "build_frontend", side_effect=build), \
             patch.object(main, "verify_backend_startup", side_effect=backend), \
             patch.object(main, "run_module_observation", side_effect=observe):
            result = main.repair_early_batch(
                object(), self.module, self.requirements, self.output, self.root / "skills", "fake",
                failures, {"fixture": "timeout"}, {"status": "passed"}, {"status": "passed"})

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(len(result["working_checkpoints"]), 2)
        events = (self.output / ".arc/codex-recovery/events.jsonl").read_text(encoding="utf-8")
        self.assertIn('"status": "restored"', events)
        self.assertEqual(calls[-3:], ["build", "backend", "observation"])

    def test_remote_compaction_records_original_error_and_restores_working_checkpoint(self):
        failures = [{"module_id": "REQ-1", "atomic_id": "REQ-1-1",
                     "error_excerpt": "assertion failed"}]
        compact = "remote compaction v2 expected exactly one compaction output item, got 0 from 2 output items"
        repair = [RuntimeError(compact), None]

        def fake_repair(*_args, **_kwargs):
            error = repair.pop(0)
            if error:
                raise error

        with patch.object(main, "repair_module_batch", side_effect=fake_repair), \
             patch.object(main, "build_frontend", return_value={"status": "passed"}), \
             patch.object(main, "verify_backend_startup", return_value={"status": "passed"}), \
             patch.object(main, "run_module_observation", return_value={
                 "status": "passed", "tests": [], "evidence_path": "module-observations/REQ-1/early"}):
            result = main.repair_early_batch(
                object(), self.module, self.requirements, self.output, self.root / "skills", "fake",
                failures, {"fixture": "compact"}, {"status": "passed"}, {"status": "passed"})

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["attempts"], 2)
        self.assertIn(compact, (self.output / ".arc/codex-recovery/events.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(result["failures"][0].get("failure_class"), "remote_compaction")


class FinalRepairBatchContractTests(unittest.TestCase):
    def _fixture(self):
        fixture = final_flow.RepairFlowTests("test_provisional_failure_reaches_next_coding_prompt_but_final_rechecks")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_failed_batch_retries_once_then_continues_next_batch(self):
        fixture = self._fixture()
        fixture.observations = {"REQ-1": ["failed"], "REQ-2": ["failed"]}
        support.FakeCodex.repair_actions = ["connection", "connection", None]
        self.assertEqual(fixture.execute(), 0)
        repair = fixture.summary()["acceptance"]["repair"]
        self.assertEqual([batch["attempts"] for batch in repair["batches"]], [2, 1])
        self.assertEqual(repair["batches"][0]["status"], "interrupted")
        self.assertEqual(repair["batches"][1]["status"], "runtime_valid")

    def test_final_repair_prompt_contains_only_current_batch_context(self):
        fixture = self._fixture()
        fixture.observations = {"REQ-1": ["failed"]}
        self.assertEqual(fixture.execute(), 0)
        prompt = next(thread.prompts[0] for thread in support.FakeCodex.threads
                      if thread.prompts and "Observation repair" in thread.prompts[0])
        self.assertNotIn("ROOT constraints file", prompt)
        self.assertNotIn('"id": "REQ-2"', prompt)
        self.assertNotIn('"id": "REQ-3"', prompt)

if __name__ == "__main__":
    unittest.main()
