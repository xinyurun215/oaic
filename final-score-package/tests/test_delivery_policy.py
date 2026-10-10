"""Delivery-outcome policy: an auxiliary check failure must never withhold a real candidate.

The pure table is tested first, then the real ``main()`` entry point with only the model, build and
browser boundaries replaced. The last test runs ``__main__`` in a real subprocess so the module
level exception handler is exercised too.
"""

import contextlib
import io
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import test_recovery as support  # unittest discover puts tests/ on the module path

main = support.main


class DeliveryPolicyTests(unittest.TestCase):
    """The truth table itself, with no application fixture."""

    def test_complete_candidate_is_delivered(self):
        self.assertEqual(main.delivery_outcome(True, True),
                         {"status": "ready", "exit_code": 0})

    def test_partial_candidate_is_not_discarded(self):
        self.assertEqual(main.delivery_outcome(True, False),
                         {"status": "partial", "exit_code": 0})

    def test_absent_candidate_is_not_fake_success(self):
        self.assertEqual(main.delivery_outcome(False, False),
                         {"status": "unavailable", "exit_code": 1})

    def test_absent_candidate_stays_unavailable_even_if_generation_claims_complete(self):
        self.assertEqual(main.delivery_outcome(False, True),
                         {"status": "unavailable", "exit_code": 1})


class DeliveryEntrypointTests(unittest.TestCase):
    """Every auxiliary failure path still returns a delivered candidate through the real main()."""

    def setUp(self):
        self.harness = support.ModuleObservationFlowTests(
            methodName="test_first_pass_needs_no_repair")
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)

    def execute(self, output=None, resume=None):
        return self.harness.execute(output, resume)

    def summary(self):
        return self.harness.summary()

    def output(self):
        return self.harness.output

    def observe(self, sequence):
        return self.harness.observe(sequence)

    def requirements(self):
        return self.harness.requirements

    def test_acceptance_exception_keeps_generated_output(self):
        self.observe(["passed", "passed"])
        original_run = support.FakeThread.run

        def write_neutral_product(thread, prompt):
            result = original_run(thread, prompt)
            page = thread.output / "frontend" / "src" / "pages" / "HomePage.tsx"
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_text("export default function HomePage() { "
                            "return <main>Neutral generated fixture</main>; }", encoding="utf-8")
            return result

        with patch.object(support.FakeThread, "run", new=write_neutral_product), \
             patch.object(main, "run_final_acceptance",
                          side_effect=RuntimeError("acceptance tool unavailable")):
            self.assertEqual(self.execute(), 0)
        self.assertEqual((self.output() / "REQ-1.txt").read_text(), "REQ-1")
        self.assertEqual((self.output() / "REQ-2.txt").read_text(), "REQ-2")
        summary = self.summary()
        self.assertEqual(summary["delivery"]["exit_code"], 0)
        self.assertNotEqual(summary["acceptance"]["status"], "passed")
        self.assertEqual(summary["status"], "complete")

    def test_repeated_compaction_failure_delivers_pre_repair_candidate(self):
        self.observe(["failed", "passed", "passed"])
        with patch.object(support.FakeThread, "repair",
                          side_effect=RuntimeError("Error running remote compact task")):
            self.assertEqual(self.execute(), 0)
        summary = self.summary()
        repair = summary["acceptance"]["repair"]
        self.assertTrue(repair["attempted"])
        self.assertFalse(repair["kept"])
        self.assertTrue(repair["restored"])
        self.assertEqual(repair["recovery_attempts"], 1)
        self.assertEqual(repair["model_turns"], 2)
        self.assertEqual(summary["delivery"], {"status": "ready", "exit_code": 0})
        self.assertEqual(summary["acceptance"]["technical_failure"]["stage"], "observation_repair")
        # Delivered bytes are the pre-repair candidate, never a half-written repair.
        self.assertEqual((self.output() / "REQ-1.txt").read_text(), "REQ-1")
        self.assertEqual((self.output() / "REQ-2.txt").read_text(), "REQ-2")

    def test_planning_quota_stops_before_any_candidate_is_generated(self):
        with patch.object(main, "plan_module_observation",
                          side_effect=RuntimeError("Quota exceeded")) as planner:
            self.assertEqual(self.execute(), 1)
        self.assertEqual(planner.call_count, 1)
        summary = self.summary()
        self.assertEqual(summary["delivery"], {"status": "unavailable", "exit_code": 1})
        self.assertEqual(support.FakeCodex.threads, [])
        self.assertEqual(summary["status"], "blocked")
        self.assertEqual(summary["unverified_count"], 4)

    def test_final_report_write_failure_cannot_gate_delivery(self):
        self.observe(["passed", "passed"])
        original = main.atomic_json

        def broken(path, payload):
            if path.name == "summary.json" and path.parent.name == "codex-recovery":
                raise OSError("summary sink unavailable")
            return original(path, payload)

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(main, "atomic_json", side_effect=broken), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(self.execute(), 0)
        self.assertIn("delivery=ready", stdout.getvalue())
        self.assertIn("summary write failed", stderr.getvalue())
        self.assertEqual((self.output() / "REQ-1.txt").read_text(), "REQ-1")
        self.assertEqual((self.output() / "REQ-2.txt").read_text(), "REQ-2")

    def test_final_commit_failure_cannot_gate_delivery(self):
        self.observe(["passed", "passed"])
        original = main.commit_module

        def commit(runtime, module_id, output_dir, label):
            if module_id == "final-candidate":
                raise RuntimeError("neutral Git permission failure")
            return original(runtime, module_id, output_dir, label)

        with patch.object(main, "commit_module", side_effect=commit):
            self.assertEqual(self.execute(), 0)
        summary = self.summary()
        self.assertEqual(summary["delivery"], {"status": "ready", "exit_code": 0})
        self.assertEqual(summary["pending_issues"][-1]["stage"], "commit")
        self.assertEqual(summary["delivery_warnings"][-1]["stage"], "commit")
        self.assertEqual((self.output() / "REQ-1.txt").read_text(), "REQ-1")
        self.assertEqual((self.output() / "REQ-2.txt").read_text(), "REQ-2")

    def test_stale_acceptance_is_unverified_not_undeliverable(self):
        self.observe(["passed", "passed"])
        builds = []

        def build(output):
            builds.append(output)
            if len(builds) == 3:
                (output / "frontend" / "src" / "late-change.ts").write_text(
                    "export const lateChange = true;", encoding="utf-8")
            return {"status": "passed"}

        with patch.object(main, "build_frontend", side_effect=build):
            self.assertEqual(self.execute(), 0)
        summary = self.summary()
        self.assertEqual(summary["acceptance"]["status"], "unverified")
        self.assertEqual(summary["delivery"], {"status": "ready", "exit_code": 0})
        self.assertTrue(any(str(issue.get("message", "")).startswith("Application changed")
                            for issue in summary["acceptance"]["issues"]))
        self.assertEqual((self.output() / "REQ-1.txt").read_text(), "REQ-1")

    def test_empty_scaffold_is_not_reported_as_delivered(self):
        blocked = {"status": "blocked", "completed_ids": [], "pending_issues": [],
                   "observation_issues": [], "unattempted_ids": ["REQ-1", "REQ-2"],
                   "module_results": []}
        with patch.object(main, "implement_modules", return_value=blocked):
            self.assertEqual(self.execute(), 1)
        summary = self.summary()
        self.assertEqual(summary["delivery"], {"status": "unavailable", "exit_code": 1})
        self.assertFalse((self.output() / "REQ-1.txt").exists())

    def test_acceptance_crash_exits_zero_through_the_real_entrypoint(self):
        code = "\n".join([
            "import sys, types",
            "sdk=types.ModuleType('openai_codex')",
            "sdk.ApprovalMode=types.SimpleNamespace(deny_all='deny_all')",
            "sdk.Sandbox=types.SimpleNamespace(full_access='full_access')",
            "sdk.CodexConfig=lambda **kwargs: kwargs",
            "sdk.Codex=object",
            "sys.modules['openai_codex']=sdk",
            "import main",
            "class Thread:",
            " def run(self, prompt): return types.SimpleNamespace(error=None)",
            "class Fake:",
            " def __init__(self, config=None): pass",
            " def __enter__(self): return self",
            " def __exit__(self, *args): pass",
            " def login_api_key(self, key): pass",
            " def thread_start(self, **kwargs): return Thread()",
            "main.Codex=Fake",
            "from tests.test_recovery import fake_plan",
            "main.plan_module_observation=fake_plan",
            "main.build_frontend=lambda output: {'status':'passed'}",
            "main.verify_backend_startup=lambda output: {'status':'passed'}",
            "def crash(*args, **kwargs): raise RuntimeError('acceptance tool unavailable')",
            "main.run_final_acceptance=crash",
            "sys.argv=['main.py',sys.argv[1],'--output-dir',sys.argv[2]]",
            "raise SystemExit(main.main())",
        ])
        result = subprocess.run([sys.executable, "-c", code, str(self.requirements()), str(self.output())],
                                cwd=Path(main.__file__).parent, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("delivery=ready", result.stdout)
        self.assertIn("acceptance=unverified", result.stdout)
        # Real generation facts survive: a verified checkpoint exists even though acceptance crashed.
        summary = self.summary()
        self.assertEqual(summary["delivery"], {"status": "ready", "exit_code": 0})
        self.assertEqual(summary["acceptance"]["status"], "unverified")
        self.assertEqual(summary["status"], "complete")


if __name__ == "__main__":
    unittest.main()
