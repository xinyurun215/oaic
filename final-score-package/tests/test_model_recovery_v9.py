import json
import re
import unittest

from unittest.mock import patch

import test_recovery as support

main = support.main
FakeCodex = support.FakeCodex


class ModelRecoveryV9Tests(support.RecoveryTests):
    def test_remote_compaction_error_is_transient_model_failure(self):
        self.assertEqual(main.model_failure_kind("Error running remote compact task"), "transient")

    def test_proxy_error_is_transient_and_failed_batch_is_skipped_after_one_retry(self):
        FakeCodex.failure = "REQ-2"
        FakeCodex.failure_message = "proxy_error: upstream connection reset"
        with patch.object(main, "build_frontend", return_value={"status": "passed"}):
            self.assertEqual(self.execute(), 0)

        self.assertEqual([thread.ran for thread in FakeCodex.threads],
                         [["REQ-1"], ["REQ-2"], ["REQ-2"]])
        summary = self.summary()
        self.assertEqual(summary["completed_ids"], ["REQ-1"])
        self.assertEqual(summary["status"], "partial")
        self.assertTrue(summary.get("blocked_batches"))
        events = (self.output / ".arc/codex-recovery/events.jsonl").read_text(encoding="utf-8")
        self.assertIn("working_checkpoint", events)
        self.assertIn("transient_model_failure", events)
        self.assertIn("repair_restore", events)

    def test_timeout_retries_once_from_working_checkpoint(self):
        real_turn = main._completed_turn
        failed = {"done": False}

        def turn(thread, prompt, *args, **kwargs):
            if "Implement ROOT module 2/2: REQ-2" in prompt and not failed["done"]:
                failed["done"] = True
                raise main.ModelTurnTimeout(1.0)
            return real_turn(thread, prompt, *args, **kwargs)

        with patch.object(main, "_completed_turn", side_effect=turn), \
             patch.object(main, "build_frontend", return_value={"status": "passed"}):
            self.assertEqual(self.execute(), 0)

        self.assertEqual([thread.ran for thread in FakeCodex.threads],
                         [["REQ-1"], [], ["REQ-2"]])
        events = (self.output / ".arc/codex-recovery/events.jsonl").read_text(encoding="utf-8")
        self.assertIn("model turn timed out", events)
        self.assertIn("transient_model_failure", events)

    def test_second_transient_failure_skips_only_current_batch_and_runs_later_module(self):
        (self.requirements / "requirements.yaml").write_text(json.dumps({
            "id": "ROOT",
            "children": [
                {"id": identifier, "name": identifier, "children": [
                    {"id": identifier + "-1", "type": "ATOMIC", "description": "rule"}
                ]} for identifier in ("REQ-1", "REQ-2", "REQ-3")
            ],
        }), encoding="utf-8")
        original_run = support.FakeThread.run
        failures = {"REQ-2": 0}

        def run(thread, prompt):
            match = re.search(r"Implement ROOT module \d+/\d+: (\S+)", prompt)
            node_id = match.group(1) if match else ""
            if node_id == "REQ-2":
                failures[node_id] += 1
                if failures[node_id] <= 2:
                    thread.prompts.append(prompt)
                    thread.ran.append(node_id)
                    raise RuntimeError("proxy_error: connection reset")
            return original_run(thread, prompt)

        with patch.object(support.FakeThread, "run", run), \
             patch.object(main, "build_frontend", return_value={"status": "passed"}):
            self.assertEqual(self.execute(), 0)

        self.assertEqual([thread.ran for thread in FakeCodex.threads],
                         [["REQ-1"], ["REQ-2"], ["REQ-2"], ["REQ-3"]])
        summary = self.summary()
        self.assertEqual(summary["completed_ids"], ["REQ-1"])
        self.assertEqual(summary["status"], "partial")
        self.assertEqual(summary["blocked_batches"][0]["batch_id"], "REQ-2")
        self.assertTrue((self.output / "REQ-3.txt").is_file())

    def test_no_complete_checkpoint_uses_run_marker_and_continues_later_modules(self):
        (self.requirements / "requirements.yaml").write_text(json.dumps({
            "id": "ROOT",
            "children": [
                {"id": identifier, "name": identifier, "children": [
                    {"id": identifier + "-1", "type": "ATOMIC", "description": "rule"}
                ]} for identifier in ("REQ-1", "REQ-2", "REQ-3")
            ],
        }), encoding="utf-8")
        original_run = support.FakeThread.run
        failures = {"REQ-1": 0}

        def run(thread, prompt):
            match = re.search(r"Implement ROOT module \d+/\d+: (\S+)", prompt)
            node_id = match.group(1) if match else ""
            if node_id == "REQ-1":
                failures[node_id] += 1
                thread.prompts.append(prompt)
                thread.ran.append(node_id)
                if failures[node_id] <= 2:
                    raise RuntimeError("proxy_error: connection reset")
            result = original_run(thread, prompt)
            if node_id in {"REQ-2", "REQ-3"}:
                (thread.output / "frontend" / "src" / f"Generated-{node_id}.txt").write_text(
                    node_id, encoding="utf-8")
            return result

        with patch.object(support.FakeThread, "run", run), \
             patch.object(main, "build_frontend", return_value={"status": "passed"}):
            self.assertEqual(self.execute(), 0)

        summary = self.summary()
        self.assertEqual(summary["status"], "partial")
        self.assertEqual(summary["completed_ids"], [])
        self.assertEqual(summary["delivery"]["status"], "partial")
        self.assertTrue((self.output / ".arc/codex-recovery/run-recovery.json").is_file())
        self.assertEqual([thread.ran for thread in FakeCodex.threads],
                         [["REQ-1"], ["REQ-1"], ["REQ-2"], ["REQ-3"]])

    def test_live_preflight_extra_unfinished_observation_does_not_break_completed_prefix(self):
        checkpoint = self.root / "checkpoint"
        current = self.root / "current.json"
        checkpoint.mkdir()
        (checkpoint / "engineering").mkdir()
        identity = {"requirements_sha256": "r"}
        saved = {
            "identity": identity,
            "version": 1,
            "source_sha256": "source",
            "source_tree_sha256": "tree",
            "module_order": ["REQ-1", "REQ-2"],
            "external_predecessors": [],
            "observations": {"REQ-1": {"status": "prepared"}},
        }
        live = dict(saved)
        live["observations"] = {
            "REQ-1": {"status": "prepared"},
            "REQ-2": {"status": "unverified", "reason": "not started"},
        }
        (checkpoint / "engineering/preflight.json").write_text(json.dumps(saved), encoding="utf-8")
        current.write_text(json.dumps(live), encoding="utf-8")
        record = {"checkpoint": checkpoint}
        self.assertTrue(main._completed_preflight_authority_matches(
            record, current, identity, ["REQ-1", "REQ-2"], ["REQ-1"], ["REQ-1"]))


if __name__ == "__main__":
    unittest.main()
