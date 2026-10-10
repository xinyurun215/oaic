import types
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import main
try:
    import test_recovery as support
except ModuleNotFoundError:
    from tests import test_recovery as support


class Handle:
    def __init__(self, result=None, delay=0.0):
        self.interrupted = False
        self.result = result or types.SimpleNamespace(error=None, status="completed")
        self.delay = delay

    def interrupt(self):
        self.interrupted = True

    def run(self):
        if self.delay:
            time.sleep(self.delay)
        return self.result


class TurnThread:
    def __init__(self, handle):
        self.handle = handle
        self.prompts = []

    def turn(self, prompt):
        self.prompts.append(prompt)
        return self.handle


class RunOnlyThread:
    def __init__(self):
        self.prompts = []

    def run(self, prompt):
        self.prompts.append(prompt)
        return types.SimpleNamespace(error=None, status="completed")


class TurnGuardTests(unittest.TestCase):
    def test_sdk_api_probe_reports_current_turn_and_interrupt_capabilities(self):
        handle = Handle()
        capabilities = main.sdk_turn_capabilities(TurnThread(handle), handle)
        self.assertEqual(capabilities, {"thread_turn": True, "turn_interrupt": True})

    def test_bounded_turn_interrupts_an_overlong_supported_turn(self):
        handle = Handle(delay=0.05)
        thread = TurnThread(handle)
        with self.assertRaises(main.ModelTurnTimeout):
            main.run_model_turn(thread, "prompt", timeout_seconds=0.005)
        self.assertTrue(handle.interrupted)

    def test_missing_interrupt_api_keeps_thread_run_fallback(self):
        thread = RunOnlyThread()
        result = main.run_model_turn(thread, "prompt", timeout_seconds=0.1)
        self.assertEqual(result.status, "completed")
        self.assertEqual(thread.prompts, ["prompt"])

    def test_remote_compaction_classifier_does_not_match_generic_timeout(self):
        self.assertTrue(main.is_remote_compaction_error(
            "remote compaction v2 expected exactly one compaction output item, got 0 from 2 output items"))
        self.assertFalse(main.is_remote_compaction_error("model turn timed out after 30 seconds"))
        self.assertEqual(main.model_failure_kind(main.ModelTurnTimeout(1)), "transient")

    def test_retry_prompt_omits_full_root_context(self):
        module = main.RequirementModule(2, 3, "REQ-2", "Second", {"id": "REQ-2", "children": []})
        prompt = main.module_retry_prompt(
            module, Path("/requirements"), Path("/skills"),
            {"stage": "model", "error": {"message": "turn timed out"}},
        )
        self.assertIn("REQ-2", prompt)
        self.assertIn("turn timed out", prompt)
        self.assertNotIn("ROOT-wide constraints", prompt)
        self.assertIn("Atomic requirement index", prompt)


class TimeoutRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.harness = support.RecoveryTests("test_first_module_compaction_without_checkpoint_retains_current_files_and_stops")
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)

    def test_timeout_recovers_once_from_previous_module_checkpoint(self):
        original = main.run_model_turn
        calls = {"REQ-2": 0}

        def turn(thread, prompt, timeout_seconds=main.MODEL_TURN_TIMEOUT_SECONDS, **kwargs):
            if "Implement ROOT module 2/2: REQ-2" in prompt and calls["REQ-2"] == 0:
                calls["REQ-2"] += 1
                raise main.ModelTurnTimeout(timeout_seconds)
            return original(thread, prompt, timeout_seconds=timeout_seconds)

        with patch.object(main, "run_model_turn", side_effect=turn), \
                patch.object(main, "build_frontend", return_value={"status": "passed"}), \
                patch.object(main, "verify_backend_startup", return_value={"status": "passed"}):
            self.assertEqual(self.harness.execute(), 0)
        self.assertEqual(calls["REQ-2"], 1)
        self.assertEqual(self.harness.latest()["completed_ids"], ["REQ-1", "REQ-2"])
        self.assertEqual(self.harness.summary()["status"], "complete")


if __name__ == "__main__":
    unittest.main()
