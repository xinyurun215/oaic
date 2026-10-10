"""Focused recovery checks; no model calls, dependency installs, or product generation."""
import json
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import test_recovery as support

main = support.main


class InterruptibleHandle:
    id = "offline-turn"

    def __init__(self):
        self.released = threading.Event()
        self.interrupted = False

    def interrupt(self):
        self.interrupted = True
        self.released.set()

    def run(self):
        if not self.released.wait(1):
            raise AssertionError("host failed to interrupt the turn")
        return types.SimpleNamespace(status="completed", error=None)


class TurnTimeoutTests(unittest.TestCase):
    def test_supported_turn_warns_then_interrupts_and_records_timeout(self):
        handle = InterruptibleHandle()
        thread = types.SimpleNamespace(turn=lambda prompt: handle)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            self.assertTrue(hasattr(main, "ModelTurnTimeout"), "bounded timeout exception missing")
            with self.assertRaises(main.ModelTurnTimeout):
                main._completed_turn(thread, "offline", output, timeout_seconds=0.08,
                                     soft_timeout_seconds=0.01)
            self.assertTrue(handle.interrupted)
            events = [json.loads(line) for line in
                      (output / ".arc/codex-recovery/events.jsonl").read_text().splitlines()]
            statuses = [event.get("status") for event in events]
            self.assertLess(statuses.index("soft_timeout"), statuses.index("timeout"))
            started = next(event for event in events if event.get("status") == "turn_started")
            self.assertEqual(started["turn_id"], "offline-turn")
            self.assertTrue(started["interrupt_available"])

    def test_working_checkpoint_is_atomic_and_identity_bound(self):
        saver = getattr(main, "save_working_checkpoint", None)
        self.assertTrue(callable(saver), "working checkpoint helper missing")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "frontend").mkdir()
            (output / "frontend" / "App.tsx").write_text("valid", encoding="utf-8")
            identity = {"fixture": "step-2"}
            relative = saver(output, "REQ-1", 2, identity, "post_model_turn")
            checkpoint = output / relative
            manifest = json.loads((checkpoint / "checkpoint.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["identity"], identity)
            self.assertEqual(manifest["module_id"], "REQ-1")
            self.assertEqual(manifest["attempt"], 2)
            self.assertEqual(manifest["stage"], "post_model_turn")
            self.assertEqual((checkpoint / "source" / "frontend" / "App.tsx").read_text(), "valid")
            self.assertFalse(any(path.name.startswith(".tmp-") for path in checkpoint.parent.iterdir()))

    def test_legacy_run_and_handle_without_interrupt_still_return(self):
        run_turn = getattr(main, "run_model_turn", None)
        self.assertTrue(callable(run_turn), "bounded turn wrapper missing")
        result = types.SimpleNamespace(status="completed", error=None)
        self.assertIs(run_turn(types.SimpleNamespace(run=lambda prompt: result), "offline"), result)
        handle = types.SimpleNamespace(run=lambda: result, id="legacy-handle")
        self.assertIs(run_turn(types.SimpleNamespace(turn=lambda prompt: handle), "offline"), result)

    def test_planning_repair_and_retry_select_v7_deadlines(self):
        limits = getattr(main, "model_turn_limits", None)
        self.assertTrue(callable(limits), "model deadline policy missing")
        self.assertEqual(limits("model"), (1200.0, 2700.0))
        self.assertEqual(limits("model", retry=True), (300.0, 1800.0))
        for stage in ("observation_planning", "observation_script_correction", "observation_repair"):
            self.assertEqual(limits(stage), (900.0, 1800.0))

    def test_retry_prompt_keeps_module_authority_without_repeating_root_context(self):
        builder = getattr(main, "module_retry_prompt", None)
        self.assertTrue(callable(builder), "compact module retry prompt missing")
        module = main.RequirementModule(2, 3, "REQ-2", "Second", {"id": "REQ-2", "children": []})
        prompt = builder(module, Path("/requirements"), Path("/skills"),
                         {"stage": "model", "error": {"message": "turn timed out"}})
        self.assertIn("REQ-2", prompt)
        self.assertIn("turn timed out", prompt)
        self.assertIn("Atomic requirement index", prompt)
        self.assertNotIn("ROOT-wide constraints", prompt)

    def test_remote_compaction_classifier_is_exact(self):
        classifier = getattr(main, "is_remote_compaction_error", None)
        self.assertTrue(callable(classifier), "remote compaction classifier missing")
        self.assertTrue(classifier(
            "remote compaction v2 expected exactly one compaction output item, got 0 from 2 output items"))
        self.assertFalse(classifier("Error running remote compact task"))
        self.assertFalse(classifier("model turn timed out after 30 seconds"))
        self.assertEqual(main.model_failure_kind(main.ModelTurnTimeout(1)), "transient")

    def test_repair_memory_source_separates_atomic_batches(self):
        import test_bounded_v3 as bounded

        harness = bounded.BoundedRepairTests(
            "test_successful_batches_survive_later_compaction_and_one_fresh_recovery")
        harness.setUp()
        try:
            harness.actions = ["success", "success"]
            harness.counts = {"A": 9, "PASSED": 1}
            seen = []

            def observe_request(_output, source, module_id, _run_id, _error, include_candidate=False):
                seen.append((source, module_id, include_candidate))
                return False

            with patch.object(main, "should_skip_engineering_request", side_effect=observe_request):
                harness.execute()
            self.assertEqual(len(seen), 2)
            self.assertEqual({item[1] for item in seen}, {"A"})
            self.assertTrue(all(":" in item[0] for item in seen))
            self.assertNotEqual(seen[0][0], seen[1][0])
        finally:
            harness.doCleanups()

    def test_evolution_allowlist_requires_prepared_input_and_no_verified_prior(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requirements = root / "requirements"
            requirements.mkdir()
            payload = {
                "id": "ROOT",
                "name": "Evolution Requirements for an Online Spreadsheet Data Workspace",
                "type": "FOLDER",
                "dependencies": [],
                "children": [{
                    "id": "REQ-6",
                    "type": "FOLDER",
                    "dependencies": ["REQ-1-1-1", "REQ-1-3-2", "REQ-3-1-3", "REQ-4-1-1"],
                    "children": [{"id": "REQ-6-1-1", "type": "ATOMIC", "dependencies": []}],
                }],
            }
            (requirements / "requirements.yaml").write_text(json.dumps(payload), encoding="utf-8")
            output = root / "output"
            output.mkdir()
            self.assertEqual(main._stage_external_predecessors(
                requirements, output, False, verified_prior_stage=False), set())
            self.assertEqual(main._stage_external_predecessors(
                requirements, output, True, verified_prior_stage=True), set())
            self.assertEqual(main._stage_external_predecessors(
                requirements, output, True, verified_prior_stage=False), {
                    "REQ-1-1-1", "REQ-1-3-2", "REQ-3-1-3", "REQ-4-1-1"})


if __name__ == "__main__":
    unittest.main()
