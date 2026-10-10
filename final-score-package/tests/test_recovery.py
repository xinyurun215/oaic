"""Model-free recovery tests; the SDK boundary is replaced, file/build work is real."""

import json
import contextlib
import io
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sdk = types.ModuleType("openai_codex")
sdk.ApprovalMode = types.SimpleNamespace(deny_all="deny_all")
sdk.CodexConfig = lambda **kwargs: kwargs
sdk.Sandbox = types.SimpleNamespace(full_access="full_access")
sdk.Codex = object
sys.modules.setdefault("openai_codex", sdk)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "arcbench-agent-runtime" / "src"))

import main


def fake_plan(_codex, module, _requirements, output, _root, _model):
    folder = output / "module-observations" / module.node_id
    folder.mkdir(parents=True, exist_ok=True)
    script = b"// original two chains"
    contract = main.observation_contract(module)
    (folder / "checks.spec.js").write_bytes(script)
    (folder / "frozen-v1.spec.js").write_bytes(script)
    main.atomic_json(folder / "contract.json", contract)
    return contract, script


def fake_observe_pass(output, module, phase):
    evidence = output / "module-observations" / module.node_id / phase
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "checks.spec.js").write_bytes(module.frozen_script or b"")
    (evidence / "result.json").write_text("{}", encoding="utf-8")
    return {"status": "passed", "tests": [{"title": title, "status": "expected",
                                          "executed": True, "result_status": "passed"} for title in module.chain_titles],
            "duration_seconds": 0.1, "evidence_path": evidence.relative_to(output).as_posix()}


class FakeThread:
    def __init__(self, output, failure=None, break_build=False, return_error=None,
                 failure_message="429 Too Many Requests; token=secret-example"):
        self.output = Path(output)
        self.failure = failure
        self.break_build = break_build
        self.return_error = return_error
        self.failure_message = failure_message
        self.ran = []
        self.prompts = []

    def run(self, prompt):
        self.prompts.append(prompt)
        if "Observation repair" in prompt:
            return self.repair(prompt)
        node_id = re.search(r"Implement ROOT module \d+/\d+: (\S+)", prompt).group(1)
        self.ran.append(node_id)
        actions = FakeCodex.actions.get(node_id, [])
        if actions:
            action = actions.pop(0)
            if action == "compact_after_write":
                (self.output / "unfinished.txt").write_text("current work", encoding="utf-8")
                raise RuntimeError("Error running remote compact task")
            if action == "require_current_work" and not (self.output / "unfinished.txt").is_file():
                raise RuntimeError("Current work was lost")
        if node_id == self.failure:
            raise RuntimeError(self.failure_message)
        if node_id == "REQ-2" and self.return_error is not None:
            return types.SimpleNamespace(error=self.return_error)
        (self.output / f"{node_id}.txt").write_text(node_id, encoding="utf-8")
        if node_id == "REQ-2" and self.break_build:
            (self.output / "frontend" / "src" / "App.tsx").write_text("<bad", encoding="utf-8")
        return types.SimpleNamespace(error=None)

    def repair(self, prompt):
        """Emulate the single global application repair turn for the modules the host named."""
        self.ran.append("final-candidate")
        action = (FakeCodex.repair_actions.pop(0) if FakeCodex.repair_actions
                  else FakeCodex.repair_action)
        FakeCodex.repair_action = None
        if action == "quota":
            raise RuntimeError("Quota exceeded. Check billing details.")
        if action == "connection":
            raise RuntimeError("Connection reset by peer")
        if action == "compact":
            raise RuntimeError("Error running remote compact task")
        if action == "corrupt_connection":
            (self.output / "damaged.txt").write_text("damaged", encoding="utf-8")
            raise RuntimeError("Connection reset by peer")
        for target in sorted(set(re.findall(r'"module_id": "([^"]+)"', prompt))):
            (self.output / f"{target}.txt").write_text("repaired", encoding="utf-8")
        if action in {"create_script", "create_script_without_note"}:
            folder = self.output / "module-observations" / "REQ-1"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "checks.spec.js").write_text("// REQ-1-1 and REQ-1-2 browser chains",
                                                   encoding="utf-8")
        return types.SimpleNamespace(error=None)


class FakeCodex:
    thread = None
    threads = []
    actions = {}
    repair_action = None
    repair_actions = []

    def __init__(self, config=None):
        self.config = config

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def login_api_key(self, key):
        pass

    def thread_start(self, **kwargs):
        if kwargs.get("developer_instructions", "").startswith("Read only copied requirements"):
            # New source-only design turn is separate from these coding/recovery substitutes.
            return types.SimpleNamespace(run=lambda prompt: types.SimpleNamespace(error=None))
        self.thread = FakeThread(kwargs["cwd"], self.failure, self.break_build,
                                 self.return_error, self.failure_message)
        FakeCodex.thread = self.thread
        FakeCodex.threads.append(self.thread)
        return self.thread


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.requirements = self.root / "requirements"
        self.requirements.mkdir()
        (self.requirements / "requirements.yaml").write_text(
            "id: ROOT\nchildren:\n  - id: REQ-1\n    name: First\n"
            "  - id: REQ-2\n    name: Second\n", encoding="utf-8"
        )
        (self.requirements / 'requirements.yaml').write_text(json.dumps({'id': 'ROOT', 'children': [
            {'id': identifier, 'name': label, 'description': 'name', 'children': [
                {'id': identifier + '-' + str(index), 'type': 'ATOMIC', 'description': 'Neutral rule'}
                for index in (1, 2)]} for identifier, label in (('REQ-1', 'First'), ('REQ-2', 'Second'))]}))
        self.output = self.root / "output"
        FakeCodex.failure = None
        FakeCodex.break_build = False
        FakeCodex.return_error = None
        FakeCodex.failure_message = "429 Too Many Requests; token=secret-example"
        FakeCodex.thread = None
        FakeCodex.threads = []
        FakeCodex.actions = {}
        FakeCodex.repair_action = None
        FakeCodex.repair_actions = []
        self.sdk_patch = patch.object(main, "Codex", FakeCodex)
        self.sdk_patch.start()
        self.addCleanup(self.sdk_patch.stop)
        for name, replacement in (("plan_module_observation", fake_plan),
                                  ("run_module_observation", fake_observe_pass)):
            item = patch.object(main, name, side_effect=replacement)
            item.start()
            self.addCleanup(item.stop)
        self.startup_patch = patch.object(main, "verify_backend_startup", create=True,
                                          return_value={"status": "passed"})
        self.startup_patch.start()
        self.addCleanup(self.startup_patch.stop)
        self.env_patch = patch.dict(os.environ, {"MODEL": "glm-5.3-flash", "OPENAI_API_KEY": "secret-example"})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def execute(self, output=None, resume=None):
        args = ["main.py", str(self.requirements), "--output-dir", str(output or self.output)]
        if resume:
            args += ["--resume-from", str(resume)]
        with patch.object(sys, "argv", args):
            return main.main()

    def latest(self, output=None):
        return json.loads(((output or self.output) / ".arc" / "codex-recovery" / "latest.json").read_text())

    def summary(self, output=None):
        path = (output or self.output) / ".arc" / "codex-recovery" / "summary.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def runtime_events(self):
        path = self.output / ".arc" / "runner-events.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if json.loads(line).get("type") == "requirement_state"]

    def test_host_reports_stages_after_real_work_and_final_checkpoint(self):
        (self.requirements / "requirements.yaml").write_text(
            json.dumps({"id":"ROOT","children":[{"id":"REQ-1","name":"First","description":"name","children":[{"id":"REQ-1-1","type":"ATOMIC","description":"neutral"},{"id":"REQ-1-2","type":"ATOMIC","description":"neutral"}]}]}), encoding="utf-8")
        order = []
        real_save = main.save_checkpoint

        def build(output):
            order.append("build")
            return {"status": "passed"}

        def start(output):
            order.append("startup")
            return {"status": "passed"}

        def observe(output, module, phase):
            order.append("observe")
            return fake_observe_pass(output, module, phase)

        def checkpoint(output, completed, identity, result):
            order.append("checkpoint")
            if order.count('checkpoint') == 1:
                self.assertFalse(any(event.get("phase") == "implement" and
                                     event.get("status") == "completed" for event in self.runtime_events()))
            return real_save(output, completed, identity, result)

        with patch.object(main, "build_frontend", side_effect=build), patch.object(
            main, "verify_backend_startup", side_effect=start), patch.object(
            main, "run_module_observation", side_effect=observe), patch.object(
            main, "save_checkpoint", side_effect=checkpoint):
            self.assertEqual(self.execute(), 0)
        self.assertEqual(order, ["build", "startup", "checkpoint", "observe", "build", "startup", "checkpoint"])
        states = [(event["node_id"], event["phase"], event["status"])
                  for event in self.runtime_events()]
        self.assertLess(states.index(("REQ-1", "implement", "completed")),
                        states.index(("REQ-1", "design", "running")))
        self.assertIn(("REQ-1", "implement", "completed"), states)
        self.assertIn(("REQ-1", "test", "passed"), states)
        self.assertEqual({row["req_id"]: row["state"] for row in
                          main.AgentRuntime.from_env(project_dir=str(self.output)).traceability.list_node_states()},
                         {"REQ-1": "PASSED", "REQ-1-1": "PASSED", "REQ-1-2": "PASSED"})

    def test_final_checkpoint_failure_cannot_report_completion_or_pass(self):
        (self.requirements / "requirements.yaml").write_text(
            json.dumps({"id":"ROOT","children":[{"id":"REQ-1","name":"First","description":"name","children":[{"id":"REQ-1-1","type":"ATOMIC","description":"neutral"},{"id":"REQ-1-2","type":"ATOMIC","description":"neutral"}]}]}), encoding="utf-8")
        real_save = main.save_checkpoint

        def checkpoint(output, completed, identity, result):
            if completed:
                raise RuntimeError("final save failed")
            return real_save(output, completed, identity, result)

        with patch.object(main, "build_frontend", return_value={"status": "passed"}), patch.object(
            main, "save_checkpoint", side_effect=checkpoint):
            self.assertEqual(self.execute(), 1)
        states = [(event["node_id"], event["phase"], event["status"])
                  for event in self.runtime_events()]
        self.assertNotIn(("REQ-1", "implement", "completed"), states)
        self.assertFalse(any(phase == "test" and status == "passed" for _, phase, status in states))
        self.assertIn(("REQ-1", "implement", "failed"), states)

    def test_only_selected_successful_atoms_pass_when_another_chain_fails(self):
        (self.requirements / "requirements.yaml").write_text(
            json.dumps({"id":"ROOT","children":[{"id":"REQ-1","name":"First","description":"name","children":[{"id":"REQ-1-1","type":"ATOMIC","description":"neutral"},{"id":"REQ-1-2","type":"ATOMIC","description":"neutral"}]}]}), encoding="utf-8")

        def observe(output, module, phase):
            result = fake_observe_pass(output, module, phase)
            result["status"] = "failed"
            result["tests"][1]["status"] = "unexpected"
            return result

        with patch.object(main, "build_frontend", return_value={"status": "passed"}), patch.object(
            main, "run_module_observation", side_effect=observe):
            self.assertEqual(self.execute(), 0)
        states = [(event["node_id"], event["phase"], event["status"])
                  for event in self.runtime_events()]
        self.assertIn(("REQ-1-1", "test", "passed"), states)
        self.assertNotIn(("REQ-1-2", "test", "failed"), states)
        self.assertIn(("REQ-1", "test", "failed"), states)
        self.assertNotIn(("REQ-1", "test", "passed"), states)

    def test_observation_error_does_not_turn_model_claim_into_test_pass(self):
        (self.requirements / "requirements.yaml").write_text(
            json.dumps({"id":"ROOT","children":[{"id":"REQ-1","name":"First","description":"name","children":[{"id":"REQ-1-1","type":"ATOMIC","description":"neutral"},{"id":"REQ-1-2","type":"ATOMIC","description":"neutral"}]}]}), encoding="utf-8")

        def observe(output, module, phase):
            result = fake_observe_pass(output, module, phase)
            result["status"] = "error"
            return result

        with patch.object(main, "build_frontend", return_value={"status": "passed"}), patch.object(
            main, "run_module_observation", side_effect=observe):
            self.assertEqual(self.execute(), 0)
        self.assertFalse(any(event["phase"] == "test" and event["status"] == "passed"
                             for event in self.runtime_events()))

    def test_root_context_retains_global_rules_without_sibling_subtrees(self):
        (self.requirements / "requirements.yaml").write_text(
            "id: ROOT\nname: Demo\ndescription: Global rule\n"
            "data: [seed contract]\nchildren:\n"
            "  - id: REQ-1\n    name: First\n    description: first only\n"
            "  - id: REQ-2\n    name: Second\n    description: second only\n",
            encoding="utf-8",
        )
        context = main.load_root_context(self.requirements)
        self.assertEqual(context, {"id": "ROOT", "name": "Demo",
                                   "description": "Global rule", "data": ["seed contract"]})
        with patch.object(main, "build_frontend", return_value={"status": "passed"}):
            self.assertEqual(self.execute(), 0)
        first_prompt = FakeCodex.threads[0].prompts[0]
        self.assertIn("Global rule", first_prompt)
        self.assertIn("seed contract", first_prompt)
        self.assertIn("first only", first_prompt)
        self.assertNotIn("second only", first_prompt)

    def test_root_context_without_data_and_separate_module_sessions(self):
        (self.requirements / "requirements.yaml").write_text(
            "id: ROOT\nname: Demo\ndescription: Shared constraint\n"
            "children:\n  - id: REQ-1\n    name: First\n"
            "  - id: REQ-2\n    name: Second\n", encoding="utf-8"
        )
        self.assertEqual(main.load_root_context(self.requirements),
                         {"id": "ROOT", "name": "Demo", "description": "Shared constraint"})
        with patch.object(main, "build_frontend", return_value={"status": "passed"}):
            self.assertEqual(self.execute(), 0)
        self.assertEqual(len(FakeCodex.threads), 2)
        self.assertIsNot(FakeCodex.threads[0], FakeCodex.threads[1])
        self.assertEqual([thread.ran for thread in FakeCodex.threads], [["REQ-1"], ["REQ-2"]])
        self.assertTrue(all("Shared constraint" in thread.prompts[0] for thread in FakeCodex.threads))

    def test_one_build_repair_uses_new_session_and_then_continues(self):
        builds = 0

        def build(_output):
            nonlocal builds
            builds += 1
            if builds == 1:
                raise RuntimeError("actual build failure")
            return {"status": "passed"}

        with patch.object(main, "build_frontend", side_effect=build), patch.object(
            main, "verify_backend_startup", create=True, return_value={"status": "passed"}
        ):
            self.assertEqual(self.execute(), 0)
        self.assertEqual([thread.ran for thread in FakeCodex.threads],
                         [["REQ-1"], ["REQ-1"], ["REQ-2"]])
        self.assertIn("actual build failure", FakeCodex.threads[1].prompts[0])
        self.assertEqual(self.summary()["completed_ids"], ["REQ-1", "REQ-2"])
        self.assertEqual(self.summary()["status"], "complete")

    def test_backend_start_failure_gets_one_repair_call(self):
        startups = 0

        def startup(_output):
            nonlocal startups
            startups += 1
            if startups == 1:
                raise RuntimeError("backend exited with code 1: broken route")
            return {"status": "passed"}

        with patch.object(main, "build_frontend", return_value={"status": "passed"}), patch.object(
            main, "verify_backend_startup", side_effect=startup
        ):
            self.assertEqual(self.execute(), 0)
        self.assertEqual(startups, 4)
        self.assertEqual([thread.ran for thread in FakeCodex.threads],
                         [["REQ-1"], ["REQ-1"], ["REQ-2"]])
        self.assertIn("broken route", FakeCodex.threads[1].prompts[0])
        self.assertEqual(self.latest()["completed_ids"], ["REQ-1", "REQ-2"])

    def test_sqlite_schema_startup_failure_is_redacted_repaired_and_rechecked(self):
        startups = 0

        def startup(_output):
            nonlocal startups
            startups += 1
            if startups == 1:
                raise RuntimeError("SQLITE_ERROR: table items has no column named "
                                   "origin_id "
                                   "token=secret-example")
            return {"status": "passed"}

        with patch.object(main, "build_frontend", return_value={"status": "passed"}), patch.object(
            main, "verify_backend_startup", side_effect=startup
        ):
            self.assertEqual(self.execute(), 0)
        self.assertEqual(startups, 4)
        self.assertEqual([thread.ran for thread in FakeCodex.threads],
                         [["REQ-1"], ["REQ-1"], ["REQ-2"]])
        repair_prompt = FakeCodex.threads[1].prompts[0]
        self.assertIn("SQLITE_ERROR", repair_prompt)
        self.assertIn("origin_id", repair_prompt)
        self.assertNotIn("secret-example", repair_prompt)
        events = (self.output / ".arc" / "codex-recovery" / "events.jsonl").read_text()
        self.assertIn("SQLITE_ERROR", events)
        self.assertNotIn("secret-example", events)
        self.assertEqual(self.summary()["status"], "complete")

    def test_transport_then_backend_failure_gets_third_turn_with_backend_error(self):
        FakeCodex.actions = {"REQ-2": ["compact_after_write"]}
        startups = 0

        def startup(_output):
            nonlocal startups
            startups += 1
            if startups == 2:
                raise RuntimeError("SQLITE_ERROR: table items has no column named origin_id "
                                   "token=secret-example")
            return {"status": "passed"}

        with patch.object(main, "build_frontend", return_value={"status": "passed"}), patch.object(
            main, "verify_backend_startup", side_effect=startup
        ):
            self.assertEqual(self.execute(), 0)
        self.assertEqual([thread.ran for thread in FakeCodex.threads],
                         [["REQ-1"], ["REQ-2"], ["REQ-2"], ["REQ-2"]])
        third_prompt = FakeCodex.threads[3].prompts[0]
        self.assertIn("SQLITE_ERROR", third_prompt)
        self.assertIn("origin_id", third_prompt)
        self.assertNotIn("secret-example", third_prompt)
        self.assertEqual(self.summary()["module_results"][1]["attempts"], 3)
        self.assertEqual(self.summary()["completed_ids"], ["REQ-1", "REQ-2"])

    def test_backend_failure_then_transport_keeps_technical_error_for_third_turn(self):
        FakeCodex.actions = {"REQ-2": ["pass", "compact_after_write"]}
        startups = 0

        def startup(_output):
            nonlocal startups
            startups += 1
            if startups == 2:
                raise RuntimeError("backend exited with code 1: neutral startup marker")
            return {"status": "passed"}

        with patch.object(main, "build_frontend", return_value={"status": "passed"}), patch.object(
            main, "verify_backend_startup", side_effect=startup
        ):
            self.assertEqual(self.execute(), 0)
        self.assertEqual([thread.ran for thread in FakeCodex.threads],
                         [["REQ-1"], ["REQ-2"], ["REQ-2"], ["REQ-2"]])
        self.assertIn("neutral startup marker", FakeCodex.threads[3].prompts[0])
        self.assertEqual(self.summary()["module_results"][1]["attempts"], 3)

    def test_repeated_transient_does_not_borrow_technical_retry(self):
        FakeCodex.failure = "REQ-2"
        FakeCodex.failure_message = "Connection reset by peer"
        with patch.object(main, "build_frontend", return_value={"status": "passed"}):
            self.assertEqual(self.execute(), 0)
        self.assertEqual([thread.ran for thread in FakeCodex.threads],
                         [["REQ-1"], ["REQ-2"], ["REQ-2"]])
        self.assertEqual(self.summary()["module_results"][-1]["attempts"], 2)

    def test_repeated_backend_failure_does_not_borrow_transport_retry(self):
        def startup(_output):
            if FakeCodex.thread.ran[-1] == "REQ-2":
                raise RuntimeError("neutral startup failure")
            return {"status": "passed"}

        with patch.object(main, "build_frontend", return_value={"status": "passed"}), patch.object(
            main, "verify_backend_startup", side_effect=startup
        ):
            self.assertEqual(self.execute(), 0)
        self.assertEqual([thread.ran for thread in FakeCodex.threads],
                         [["REQ-1"], ["REQ-2"], ["REQ-2"]])
        self.assertEqual(self.summary()["module_results"][-1]["attempts"], 2)

    def test_failed_module_restores_checkpoint_in_delivery_directory(self):
        original_run = FakeThread.run
        app_file = self.output / "frontend" / "src" / "App.tsx"
        skill = self.output / ".codex" / "skills" / "arcbench-runtime-signals" / "SKILL.md"
        database = self.output / "backend" / "database.db"
        marker = self.output / ".arc" / "codex-recovery" / "evidence-marker.txt"
        observation = self.output / "module-observations" / "REQ-2" / "failure.txt"
        dependency_state = self.output / ".arc" / "codex-recovery" / "installed-dependencies.json"

        def broken_second_module(thread, prompt):
            result = original_run(thread, prompt)
            if thread.ran[-1] == "REQ-2":
                (self.output / "REQ-1.txt").write_text("corrupted", encoding="utf-8")
                app_file.unlink(missing_ok=True)
                (self.output / "new-source.txt").write_text("failed draft", encoding="utf-8")
                skill.unlink(missing_ok=True)
                database.write_bytes(b"keep-original-database")
                marker.write_text("keep-event", encoding="utf-8")
                observation.parent.mkdir(parents=True, exist_ok=True)
                observation.write_text("keep-observation", encoding="utf-8")
                dependency_state.write_text("stale", encoding="utf-8")
            return result

        def startup(_output):
            if FakeCodex.thread.ran[-1] == "REQ-2":
                raise RuntimeError("neutral backend startup failure")
            return {"status": "passed"}

        with patch.object(FakeThread, "run", broken_second_module), patch.object(
            main, "build_frontend", return_value={"status": "passed"}), patch.object(
            main, "verify_backend_startup", side_effect=startup
        ):
            self.assertEqual(self.execute(), 0)
        checkpoint = (self.output / ".arc" / "codex-recovery" / "checkpoints" /
                      self.latest()["checkpoint"] / "source")
        self.assertEqual(self.latest()["completed_ids"], ["REQ-1"])
        self.assertEqual((self.output / "REQ-1.txt").read_bytes(), (checkpoint / "REQ-1.txt").read_bytes())
        self.assertEqual(app_file.read_bytes(), (checkpoint / "frontend" / "src" / "App.tsx").read_bytes())
        self.assertFalse((self.output / "new-source.txt").exists())
        self.assertFalse((self.output / "REQ-2.txt").exists())
        self.assertTrue(skill.is_file())
        self.assertEqual(database.read_bytes(), b"keep-original-database")
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep-event")
        self.assertEqual(observation.read_text(encoding="utf-8"), "keep-observation")
        self.assertFalse(dependency_state.exists())
        failed = self.summary()["module_results"][-1]
        self.assertEqual(failed["status"], "pending")
        self.assertEqual(failed["recovery"]["status"], "restored")
        failed_source = self.output / failed["recovery"]["failed_source"]
        self.assertEqual((failed_source / "REQ-1.txt").read_text(encoding="utf-8"), "corrupted")
        self.assertEqual((failed_source / "new-source.txt").read_text(encoding="utf-8"), "failed draft")
        self.assertEqual(self.summary()["completed_ids"], ["REQ-1"])
        self.assertEqual(self.summary()["delivery"], {"status": "partial", "exit_code": 0})

    def test_recovery_without_checkpoint_or_with_wrong_identity_keeps_source(self):
        self.output.mkdir()
        (self.output / "draft.txt").write_text("failed draft", encoding="utf-8")
        unavailable = main.restore_failed_module(self.output, "REQ-1", {"identity": "expected"},
                                                  Path(main.__file__).parent)
        self.assertEqual(unavailable["status"], "unavailable")
        self.assertEqual((self.output / "draft.txt").read_text(encoding="utf-8"), "failed draft")
        main.save_checkpoint(self.output, ["REQ-1"], {"identity": "old"}, {"status": "passed"})
        (self.output / "draft.txt").write_text("new failed draft", encoding="utf-8")
        mismatch = main.restore_failed_module(self.output, "REQ-2", {"identity": "expected"},
                                               Path(main.__file__).parent)
        self.assertEqual(mismatch["status"], "unavailable")
        self.assertIn("differs", mismatch["warning"]["message"])
        self.assertEqual((self.output / "draft.txt").read_text(encoding="utf-8"), "new failed draft")

    def test_interrupted_checkpoint_apply_rolls_back_to_failed_draft(self):
        self.output.mkdir()
        identity = {"identity": "same"}
        (self.output / "draft.txt").write_text("checkpoint", encoding="utf-8")
        main.save_checkpoint(self.output, ["REQ-1"], identity, {"status": "passed"})
        (self.output / "draft.txt").write_text("failed draft", encoding="utf-8")
        (self.output / "new.txt").write_text("keep failed addition", encoding="utf-8")
        real_restore = main.restore_repair_snapshot
        calls = 0

        def interrupted_apply(snapshot, output):
            nonlocal calls
            calls += 1
            if calls == 1:
                (output / "draft.txt").write_text("partly restored", encoding="utf-8")
                raise OSError("injected apply interruption")
            return real_restore(snapshot, output)

        with patch.object(main, "restore_repair_snapshot", side_effect=interrupted_apply):
            result = main.restore_failed_module(self.output, "REQ-2", identity, Path(main.__file__).parent)
        self.assertEqual(result["status"], "rolled_back")
        self.assertIn("injected apply interruption", result["warning"]["message"])
        self.assertEqual(calls, 2)
        self.assertEqual((self.output / "draft.txt").read_text(encoding="utf-8"), "failed draft")
        self.assertEqual((self.output / "new.txt").read_text(encoding="utf-8"), "keep failed addition")

    def test_failed_apply_and_rollback_reports_indeterminate_source(self):
        self.output.mkdir()
        identity = {"identity": "same"}
        (self.output / "draft.txt").write_text("checkpoint", encoding="utf-8")
        main.save_checkpoint(self.output, ["REQ-1"], identity, {"status": "passed"})
        (self.output / "draft.txt").write_text("failed draft", encoding="utf-8")
        with patch.object(main, "restore_repair_snapshot", side_effect=OSError("restore unavailable")) as restore:
            result = main.restore_failed_module(self.output, "REQ-2", identity, Path(main.__file__).parent)
        self.assertEqual(restore.call_count, 2)
        self.assertEqual(result["status"], "indeterminate")
        self.assertIn("restore unavailable", result["warning"]["message"])
        self.assertEqual((self.output / "draft.txt").read_text(encoding="utf-8"), "failed draft")

    def test_indeterminate_recovery_stops_later_modules_but_keeps_checkpoint_delivery(self):
        (self.requirements / "requirements.yaml").write_text(
            "id: ROOT\nchildren:\n  - id: REQ-1\n  - id: REQ-2\n  - id: REQ-3\n",
            encoding="utf-8",
        )

        def startup(_output):
            if FakeCodex.thread.ran[-1] == "REQ-2":
                raise RuntimeError("neutral backend failure")
            return {"status": "passed"}

        with patch.object(main, "build_frontend", return_value={"status": "passed"}), patch.object(
            main, "verify_backend_startup", side_effect=startup), patch.object(
            main, "restore_repair_snapshot", side_effect=OSError("restore unavailable")
        ):
            self.assertEqual(self.execute(), 0)
        self.assertEqual([thread.ran for thread in FakeCodex.threads],
                         [["REQ-1"], ["REQ-2"], ["REQ-2"]])
        self.assertEqual(self.summary()["status"], "blocked")
        self.assertEqual(self.summary()["unattempted_ids"], ["REQ-3"])
        self.assertEqual(self.summary()["module_results"][-1]["recovery"]["status"], "indeterminate")
        self.assertEqual(self.summary()["delivery"], {"status": "partial", "exit_code": 0})

    def test_unfixed_sqlite_startup_failure_stays_partial_and_later_module_runs(self):
        (self.requirements / "requirements.yaml").write_text(
            "id: ROOT\nchildren:\n  - id: REQ-1\n  - id: REQ-2\n  - id: REQ-3\n",
            encoding="utf-8",
        )
        startups = 0

        def startup(_output):
            nonlocal startups
            startups += 1
            if startups in (2, 3):
                raise RuntimeError("SQLITE_CONSTRAINT_NOTNULL: NOT NULL constraint failed: probe.value")
            return {"status": "passed"}

        with patch.object(main, "build_frontend", return_value={"status": "passed"}), patch.object(
            main, "verify_backend_startup", side_effect=startup
        ):
            self.assertEqual(self.execute(), 0)
        self.assertEqual(startups, 4)
        self.assertEqual([thread.ran for thread in FakeCodex.threads],
                         [["REQ-1"], ["REQ-2"], ["REQ-2"], ["REQ-3"]])
        self.assertIn("SQLITE_CONSTRAINT_NOTNULL", FakeCodex.threads[2].prompts[0])
        self.assertIn("SQLITE_CONSTRAINT_NOTNULL", FakeCodex.threads[3].prompts[0])
        self.assertEqual(self.summary()["completed_ids"], ["REQ-1"])
        self.assertEqual(self.summary()["status"], "partial")
        self.assertEqual([issue["module_id"] for issue in self.summary()["pending_issues"]], ["REQ-2"])

    def test_build_repair_and_transport_use_separate_extra_calls(self):
        builds = 0

        def build(_output):
            nonlocal builds
            builds += 1
            if builds == 2:
                raise RuntimeError("broken build")
            return {"status": "passed"}

        FakeCodex.actions = {"REQ-2": ["require_current_work", "compact_after_write"]}
        (self.output / "unfinished.txt").parent.mkdir(parents=True)
        (self.output / "unfinished.txt").write_text("existing", encoding="utf-8")
        with patch.object(main, "build_frontend", side_effect=build):
            self.assertEqual(self.execute(), 0)
        self.assertEqual([thread.ran for thread in FakeCodex.threads],
                         [["REQ-1"], ["REQ-2"], ["REQ-2"], ["REQ-2"]])
        self.assertIn("broken build", FakeCodex.threads[3].prompts[0])
        self.assertEqual(self.summary()["module_results"][1]["attempts"], 3)
        self.assertEqual(self.summary()["status"], "complete")

    def test_failed_repair_keeps_prefix_and_passes_issue_to_later_module(self):
        (self.requirements / "requirements.yaml").write_text(
            "id: ROOT\nchildren:\n  - id: REQ-1\n  - id: REQ-2\n  - id: REQ-3\n",
            encoding="utf-8",
        )
        builds = 0

        def build(_output):
            nonlocal builds
            builds += 1
            if builds in (2, 3):
                raise RuntimeError("broken shared component")
            return {"status": "passed"}

        with patch.object(main, "build_frontend", side_effect=build), patch.object(
            main, "verify_backend_startup", create=True, return_value={"status": "passed"}
        ):
            self.assertEqual(self.execute(), 0)
        self.assertEqual([thread.ran for thread in FakeCodex.threads],
                         [["REQ-1"], ["REQ-2"], ["REQ-2"], ["REQ-3"]])
        self.assertIn("broken shared component", FakeCodex.threads[2].prompts[0])
        self.assertIn("broken shared component", FakeCodex.threads[3].prompts[0])
        self.assertTrue((self.output / "REQ-3.txt").is_file())
        self.assertEqual(self.latest()["completed_ids"], ["REQ-1"])
        self.assertEqual(self.summary()["completed_ids"], ["REQ-1"])
        self.assertEqual(self.summary()["status"], "partial")
        self.assertEqual([issue["module_id"] for issue in self.summary()["pending_issues"]], ["REQ-2"])
        self.assertEqual(self.summary()["module_results"][-1]["status"], "runtime_passed_uncheckpointed")

    def test_first_module_compaction_without_checkpoint_retains_current_files_and_stops(self):
        FakeCodex.actions = {"REQ-1": ["compact_after_write", "require_current_work"]}
        with patch.object(main, "build_frontend", return_value={"status": "passed"}), patch.object(
            main, "verify_backend_startup", create=True, return_value={"status": "passed"}
        ):
            self.assertEqual(self.execute(), 1)
        self.assertEqual([thread.ran for thread in FakeCodex.threads],
                         [["REQ-1"]])
        self.assertEqual((self.output / "unfinished.txt").read_text(encoding="utf-8"), "current work")
        self.assertEqual(self.summary()["completed_ids"], [])
        self.assertEqual(self.summary()["status"], "blocked")

    def test_explicit_quota_denial_stops_without_extra_call(self):
        FakeCodex.failure = "REQ-1"
        FakeCodex.failure_message = "Quota exceeded. Check your plan and billing details."
        with patch.object(main, "build_frontend", return_value={"status": "passed"}), patch.object(
            main, "verify_backend_startup", create=True, return_value={"status": "passed"}
        ):
            self.assertEqual(self.execute(), 1)
        self.assertEqual([thread.ran for thread in FakeCodex.threads], [["REQ-1"]])
        self.assertEqual(self.summary()["status"], "blocked")
        self.assertEqual(self.summary()["unattempted_ids"], ["REQ-2"])

    def test_connection_failure_keeps_first_checkpoint_and_redacts_error(self):
        FakeCodex.failure = "REQ-2"
        with patch.object(main, "build_frontend", create=True, return_value={"command": "npm run build", "status": "passed"}):
            self.assertEqual(self.execute(), 0)
        self.assertEqual(self.latest()["completed_ids"], ["REQ-1"])
        events = (self.output / ".arc" / "codex-recovery" / "events.jsonl").read_text()
        self.assertIn("REQ-2", events)
        self.assertIn("429", events)
        self.assertNotIn("secret-example", events)
        self.assertIn("unavailable", events)

    def test_resume_uses_new_directory_and_skips_completed_module(self):
        FakeCodex.failure = "REQ-2"
        with patch.object(main, "build_frontend", create=True, return_value={"command": "npm run build", "status": "passed"}):
            self.assertEqual(self.execute(), 0)
            old = (self.output / "REQ-2.txt").exists()
            FakeCodex.failure = None
            resumed = self.root / "resumed"
            self.assertEqual(self.execute(resumed, self.output), 0)
        self.assertEqual(FakeCodex.thread.ran, ["REQ-2"])
        self.assertEqual(self.latest(resumed)["completed_ids"], ["REQ-1", "REQ-2"])
        self.assertEqual((self.output / "REQ-2.txt").exists(), old)

    def test_sdk_turn_error_uses_exposed_status_and_marks_unknown_request_fields(self):
        detail = types.SimpleNamespace(http_status_code=429)
        variant = types.SimpleNamespace(response_too_many_failed_attempts=detail)
        FakeCodex.return_error = types.SimpleNamespace(
            message="upstream retry limit; token=secret-example",
            codex_error_info=types.SimpleNamespace(root=variant),
        )
        with patch.object(main, "build_frontend", return_value={"status": "passed"}):
            self.assertEqual(self.execute(), 0)
        events = [json.loads(line) for line in
                  (self.output / ".arc" / "codex-recovery" / "events.jsonl").read_text().splitlines()]
        failed = next(event for event in events if event.get("status") == "failed" and "error" in event)
        self.assertEqual(failed["error"]["status_code"], "429")
        self.assertEqual(failed["error"]["request_id"], "unavailable")
        self.assertEqual(failed["error"]["retry_count"], "unavailable")
        self.assertEqual(failed["error"]["retry_exhausted"], "yes")
        self.assertNotIn("secret-example", json.dumps(failed))

    def test_resume_rejects_changed_requirements_without_touching_old_output(self):
        with patch.object(main, "build_frontend", create=True, return_value={"command": "npm run build", "status": "passed"}):
            self.execute()
            (self.requirements / "requirements.yaml").write_text("id: ROOT\nchildren:\n  - id: OTHER\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                self.execute(self.root / "resumed", self.output)
        self.assertEqual(self.latest()["completed_ids"], ["REQ-1", "REQ-2"])

    def test_platform_metadata_is_preserved_on_first_run(self):
        (self.output / ".arc").mkdir(parents=True)
        (self.output / ".arc" / "runner-events.jsonl").write_text("platform\n")
        (self.output / "requirements").mkdir()
        (self.output / "requirements" / "source.txt").write_text("platform")
        with patch.object(main, "build_frontend", return_value={"status": "passed"}):
            self.execute()
        events = (self.output / ".arc" / "runner-events.jsonl").read_text()
        self.assertTrue(events.startswith("platform\n"))
        self.assertIn('"type": "requirement_state"', events)
        self.assertEqual((self.output / "requirements" / "source.txt").read_text(), "platform")

    def test_prepared_application_is_preserved_on_first_run(self):
        shutil.copytree(Path(main.__file__).parent / "template" / "frontend", self.output / "frontend")
        shutil.copytree(Path(main.__file__).parent / "template" / "backend", self.output / "backend")
        marker = self.output / "frontend" / "platform-marker.txt"
        marker.write_text("prepared application")
        with patch.object(main, "build_frontend", return_value={"status": "passed"}):
            self.execute()
        self.assertEqual(marker.read_text(), "prepared application")

    def test_fresh_copy_installs_guide_and_prepared_app_can_find_bundled_guide(self):
        template = Path(main.__file__).parent / "template"
        fresh = self.root / "fresh"
        main.copy_template_contents(template, fresh)
        self.assertEqual((fresh / "TEMPLATE_GUIDE.md").read_bytes(),
                         (template / "TEMPLATE_GUIDE.md").read_bytes())

        prepared = self.root / "prepared"
        (prepared / "frontend").mkdir(parents=True)
        (prepared / "backend").mkdir()
        marker = prepared / "frontend" / "platform-owned.txt"
        marker.write_text("keep this", encoding="utf-8")
        with patch.object(main, "build_frontend", return_value={"status": "passed"}):
            self.execute(prepared)
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep this")
        self.assertFalse((prepared / "TEMPLATE_GUIDE.md").exists())
        prompt = main._developer_instructions(prepared / "skills")
        self.assertIn("TEMPLATE_GUIDE.md in the project root", prompt)
        self.assertIn(str((template / "TEMPLATE_GUIDE.md").resolve()), prompt)
        self.assertTrue((template / "TEMPLATE_GUIDE.md").is_file())

    def test_new_checkpoint_restores_guide_and_public_control_dependencies(self):
        FakeCodex.failure = "REQ-2"
        with patch.object(main, "build_frontend", return_value={"status": "passed"}):
            self.assertEqual(self.execute(), 0)
            source = (self.output / ".arc" / "codex-recovery" / "checkpoints" /
                      self.latest()["checkpoint"] / "source")
            guide = (Path(main.__file__).parent / "template" / "TEMPLATE_GUIDE.md").read_bytes()
            self.assertEqual((source / "TEMPLATE_GUIDE.md").read_bytes(), guide)
            FakeCodex.failure = None
            resumed = self.root / "resumed"
            self.assertEqual(self.execute(resumed, self.output), 0)
        self.assertEqual(FakeCodex.thread.ran, ["REQ-2"])
        self.assertEqual((resumed / "TEMPLATE_GUIDE.md").read_bytes(), guide)
        self.assertEqual((resumed / "frontend" / "package-lock.json").read_bytes(),
                         (source / "frontend" / "package-lock.json").read_bytes())
        manifest = json.loads((resumed / "frontend" / "package.json").read_text())
        lock = json.loads((resumed / "frontend" / "package-lock.json").read_text())
        for name, version in (("@radix-ui/react-dialog", "1.1.23"),
                              ("@radix-ui/react-dropdown-menu", "2.1.24")):
            self.assertEqual(manifest["dependencies"][name], version)
            self.assertEqual(lock["packages"][f"node_modules/{name}"]["version"], version)

    def test_old_template_or_entry_checkpoint_rejects_new_identity(self):
        repo = Path(main.__file__).parent
        with patch.object(main, "build_frontend", return_value={"status": "passed"}):
            for changed in ("template", "entry"):
                agent = self.root / f"old-{changed}"
                agent.mkdir()
                for file in ("main.py", "requirements.txt"):
                    shutil.copy2(repo / file, agent / file)
                for tree in ("template", "skills", "arcbench-agent-runtime"):
                    shutil.copytree(repo / tree, agent / tree)
                if changed == "template":
                    (agent / "template" / "TEMPLATE_GUIDE.md").unlink()
                else:
                    entry = agent / "main.py"
                    entry.write_bytes(entry.read_bytes() + b"\n# prior entry identity\n")
                old_output = self.root / f"old-output-{changed}"
                with patch.object(main, "__file__", str(agent / "main.py")):
                    self.execute(old_output)
                old_latest = self.latest(old_output)
                with self.subTest(changed=changed):
                    with self.assertRaisesRegex(ValueError, "configuration differs"):
                        self.execute(self.root / f"resume-{changed}", old_output)
                self.assertEqual(self.latest(old_output), old_latest)

    def test_no_complete_checkpoint_cannot_skip_modules(self):
        self.output.mkdir()
        resumed = self.root / "resumed"
        self.assertEqual(self.execute(resumed, self.output), 1)
        summary = json.loads((resumed / ".arc/codex-recovery/summary.json").read_text())
        self.assertEqual(summary["status"], "blocked")
        self.assertEqual(summary["delivery"]["status"], "unavailable")
        self.assertFalse((self.output / "REQ-1.txt").exists())

    def test_resume_rejects_changed_model(self):
        with patch.object(main, "build_frontend", return_value={"status": "passed"}):
            self.execute()
            with patch.dict(os.environ, {"MODEL": "another-model"}):
                with self.assertRaisesRegex(ValueError, "configuration differs"):
                    self.execute(self.root / "resumed", self.output)

    def test_resume_from_copied_checkpoint_using_environment_default(self):
        FakeCodex.failure = "REQ-2"
        with patch.object(main, "build_frontend", return_value={"status": "passed"}):
            self.assertEqual(self.execute(), 0)
            bundle = self.root / "recovery-input"
            shutil.copytree(self.output / ".arc" / "codex-recovery", bundle)
            FakeCodex.failure = None
            resumed = self.root / "resumed"
            args = ["main.py", str(self.requirements), "--output-dir", str(resumed)]
            with patch.dict(os.environ, {"ARCBENCH_RESUME_FROM": str(bundle)}), patch.object(sys, "argv", args):
                self.assertEqual(main.main(), 0)
        self.assertEqual(FakeCodex.thread.ran, ["REQ-2"])

    def test_checkpoint_excludes_dependencies_and_credentials(self):
        self.output.mkdir()
        (self.output / ".env").write_text("secret-example")
        (self.output / ".npmrc").write_text("secret-example")
        (self.output / ".codex" / "auth").mkdir(parents=True)
        (self.output / ".codex" / "auth" / "tokens.json").write_text("secret-example")
        with patch.object(main, "build_frontend", return_value={"status": "passed"}):
            self.execute()
        recovery = self.output / ".arc" / "codex-recovery"
        source = recovery / "checkpoints" / self.latest()["checkpoint"] / "source"
        self.assertFalse((source / ".env").exists())
        self.assertFalse((source / ".npmrc").exists())
        self.assertFalse((source / ".codex").exists())
        self.assertTrue((source / "frontend" / "package-lock.json").is_file())
        self.assertTrue((source / "REQ-2.txt").is_file())

    def test_business_auth_secret_and_credential_sources_survive_snapshot(self):
        self.output.mkdir()
        shutil.copytree(Path(main.__file__).parent / "template" / "frontend", self.output / "frontend")
        shutil.copytree(Path(main.__file__).parent / "template" / "backend", self.output / "backend")
        frontend_src = self.output / "frontend" / "src"
        names = ("auth.js", "secret-notes.ts", "credential-form.ts")
        for name in names:
            (frontend_src / name).write_text("export const feature = true;", encoding="utf-8")
        with patch.object(main, "build_frontend", return_value={"status": "passed"}):
            self.execute()
        recovery = self.output / ".arc" / "codex-recovery"
        source = recovery / "checkpoints" / self.latest()["checkpoint"] / "source" / "frontend" / "src"
        for name in names:
            self.assertEqual((source / name).read_text(encoding="utf-8"), "export const feature = true;")

    def test_changed_skill_or_bundled_runtime_rejects_resume(self):
        with patch.object(main, "build_frontend", return_value={"status": "passed"}):
            self.execute()
            repo = Path(main.__file__).parent
            for directory, changed in (("skills", "arcbench-runtime-signals/SKILL.md"),
                                       ("arcbench-agent-runtime", "src/arcbench_agent_runtime/runtime.py")):
                agent = self.root / f"agent-{directory}"
                agent.mkdir()
                for file in ("main.py", "requirements.txt"):
                    shutil.copy2(repo / file, agent / file)
                for tree in ("template", "skills", "arcbench-agent-runtime"):
                    shutil.copytree(repo / tree, agent / tree)
                target = agent / directory / changed
                target.write_text(target.read_text(encoding="utf-8") + "\n# changed behavior\n", encoding="utf-8")
                with self.subTest(directory=directory), patch.object(main, "__file__", str(agent / "main.py")):
                    resumed = self.root / f"resume-{directory}"
                    self.assertEqual(self.execute(resumed, self.output), 1)
                    summary = json.loads((resumed / ".arc/codex-recovery/summary.json").read_text())
                    self.assertEqual(summary["status"], "blocked")
                    self.assertEqual(summary["delivery"]["status"], "unavailable")

    def test_generated_runtime_install_metadata_does_not_change_identity(self):
        repo = Path(main.__file__).parent
        agent = self.root / "agent"
        agent.mkdir()
        for file in ("main.py", "requirements.txt"):
            shutil.copy2(repo / file, agent / file)
        for tree in ("template", "skills", "arcbench-agent-runtime"):
            shutil.copytree(repo / tree, agent / tree)
        original = main.input_identity(self.requirements, agent)
        metadata = agent / "arcbench-agent-runtime" / "src" / "arcbench_agent_runtime.egg-info"
        metadata.mkdir()
        (metadata / "PKG-INFO").write_text("generated at install time")
        self.assertEqual(main.input_identity(self.requirements, agent), original)

    def test_failure_log_redacts_url_userinfo_and_auth_query(self):
        FakeCodex.failure = "REQ-2"
        FakeCodex.failure_message = (
            "registry https://alice:p%40ss@example.test/npm?access_key=abc123&token=xyz987 failed"
        )
        with patch.object(main, "build_frontend", return_value={"status": "passed"}):
            self.assertEqual(self.execute(), 0)
        events = (self.output / ".arc" / "codex-recovery" / "events.jsonl").read_text()
        self.assertIn("example.test/npm", events)
        self.assertNotIn("alice", events)
        self.assertNotIn("p%40ss", events)
        self.assertNotIn("abc123", events)
        self.assertNotIn("xyz987", events)

    def test_process_exit_during_checkpoint_publish_preserves_previous_pointer(self):
        self.output.mkdir()
        (self.output / "source.txt").write_text("first")
        main.save_checkpoint(self.output, ["REQ-1"], {"test": "identity"}, {"status": "passed"})
        code = "\n".join([
            "import os, sys, types",
            "sdk=types.ModuleType('openai_codex')",
            "sdk.ApprovalMode=sdk.Codex=sdk.CodexConfig=sdk.Sandbox=object",
            "sys.modules['openai_codex']=sdk",
            "from pathlib import Path",
            "import main",
            "old=main.atomic_json",
            "def crash(path, payload):",
            "    if path.name == 'latest.json': os._exit(77)",
            "    return old(path, payload)",
            "main.atomic_json=crash",
            "main.save_checkpoint(Path(sys.argv[1]), ['REQ-1','REQ-2'], {'test':'identity'}, {'status':'passed'})",
        ])
        result = subprocess.run([sys.executable, "-c", code, str(self.output)], cwd=Path(main.__file__).parent)
        self.assertEqual(result.returncode, 77)
        self.assertEqual(self.latest()["completed_ids"], ["REQ-1"])

    @unittest.skipUnless(shutil.which("npm"), "npm is required for the real Vite build")
    def test_real_vite_build_failure_and_reinstall_from_snapshot(self):
        FakeCodex.break_build = True
        self.assertEqual(self.execute(), 0)
        first = self.latest()
        self.assertEqual(first["completed_ids"], ["REQ-1"])
        source = self.output / ".arc" / "codex-recovery" / "checkpoints" / first["checkpoint"] / "source"
        self.assertFalse((source / "frontend" / "node_modules").exists())
        self.assertFalse((source / "frontend" / "dist").exists())
        self.assertTrue((source / "frontend" / "package-lock.json").is_file())
        app = Path('frontend/src/App.tsx')
        self.assertEqual((self.output / app).read_bytes(), (source / app).read_bytes())
        drafts = (self.output / '.arc/codex-recovery/failed-candidates').glob('*/frontend/src/App.tsx')
        self.assertTrue(any(path.read_text() == '<bad' for path in drafts), 'failed source evidence was lost')
        FakeCodex.break_build = False
        resumed = self.root / "resumed"
        self.assertEqual(self.execute(resumed, self.output), 0)
        self.assertEqual(FakeCodex.thread.ran, ["REQ-2"])
        self.assertTrue((resumed / "frontend" / "dist" / "index.html").is_file())
        self.assertEqual(self.latest(resumed)["completed_ids"], ["REQ-1", "REQ-2"])


class RequirementOrganizationTests(unittest.TestCase):
    def test_atomic_index_preserves_order_and_ancestors(self):
        tree = {
            'id': 'M', 'name': 'Module', 'type': 'FOLDER',
            'children': [
                {'id': 'G', 'type': 'FOLDER', 'children': [
                    {'id': 'A', 'name': 'First', 'type': 'ATOMIC'},
                ]},
                {'id': 'B', 'name': 'Second', 'type': 'ATOMIC'},
            ],
        }
        self.assertEqual(main._atomic_requirement_index(tree), [
            {'id': 'A', 'name': 'First', 'ancestor_ids': ['M', 'G']},
            {'id': 'B', 'name': 'Second', 'ancestor_ids': ['M']},
        ])

    def test_empty_module_has_empty_index(self):
        self.assertEqual(main._atomic_requirement_index(
            {'id': 'M', 'type': 'FOLDER', 'children': []}), [])

    def test_prompt_preserves_source_and_adds_organization(self):
        subtree = {
            'id': 'M', 'name': 'Module', 'type': 'FOLDER',
            'description': 'Parent rule',
            'children': [{
                'id': 'A', 'name': 'Action', 'type': 'ATOMIC',
                'description': 'Use the exact link name.',
                'dependencies': ['OTHER'],
                'scenarios': [{'name': 'Keep this scenario'}],
            }],
        }
        module = main.RequirementModule(1, 1, 'M', 'Module', subtree)
        prompt = main._module_prompt(
            module, Path('/requirements'), [], Path('/skills'),
            {'id': 'ROOT', 'description': 'Global rule'}, [])
        for text in (
            'Global rule', 'Parent rule', 'Use the exact link name.',
            'OTHER', 'Keep this scenario', 'Atomic requirement index',
        ):
            self.assertIn(text, prompt)
        self.assertIn(json.dumps(subtree, ensure_ascii=False, indent=2), prompt)

    def test_coding_turn_can_check_main_flow_and_report_unverified_atoms(self):
        module = main.RequirementModule(1, 1, 'M', 'Module', {
            'id': 'M', 'children': [
                {'id': 'A', 'type': 'ATOMIC', 'description': 'Create and persist a record.'}]})
        developer = main._developer_instructions(Path('/skills'))
        prompt = main._module_prompt(module, Path('/requirements'), [], Path('/skills'),
                                     {'id': 'ROOT'}, [])
        self.assertIn('run bounded', developer)
        self.assertIn('timeout -k', developer)
        self.assertIn('same coding turn', prompt)
        self.assertIn('create, edit, save, and reload', prompt)
        self.assertIn('nonzero exit', prompt)
        self.assertIn('unverified', prompt)
        self.assertIn('implementation.json', prompt)
        self.assertIn('frozen', developer)
        self.assertIn('documented formal startup entry', developer)
        self.assertIn('leave it unchanged', developer)
        self.assertIn('observable state and data', developer)
        self.assertIn('HTTP 200', developer)
        self.assertIn('after the recheck', developer)
        self.assertIn('confirmed residual failure', prompt)


class SourceContractTests(unittest.TestCase):
    def setUp(self):
        self.module = main.RequirementModule(1, 1, "REQ-1", "One", {
            "id": "REQ-1", "children": [
                {"id": "REQ-1-1", "type": "ATOMIC", "description": "Create and persist an item."},
                {"id": "REQ-1-2", "type": "ATOMIC", "description": "Export a real file."}]})
        self.valid = main.observation_contract(self.module)

    def test_host_metadata_covers_each_source_atom_once(self):
        self.assertEqual([chain["title"] for chain in self.valid["chains"]], ["REQ-1-1", "REQ-1-2"])
        self.assertEqual([chain["requirement_ids"] for chain in self.valid["chains"]],
                         [["REQ-1-1"], ["REQ-1-2"]])

    def test_source_duplicate_or_empty_ids_are_rejected(self):
        for identifier in ("REQ-1-1", ""):
            self.module.subtree["children"][1]["id"] = identifier
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                main.observation_contract(self.module)

    def test_requirement_inventory_separates_files_and_executed_results(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "code.js").write_text("code")
            claims = {"requirements": [{"id": "REQ-1-1", "files": ["code.js"], "unfinished": ""}]}
            observed = {"status": "failed", "tests": [{"title": "REQ-1-1", "status": "expected",
                                                       "executed": True, "result_status": "passed"}]}
            inventory = main.requirement_inventory(self.module, self.valid, claims, observed, output)
            self.assertEqual(inventory["REQ-1-1"]["observation_status"], "observed_passed")
            self.assertEqual(inventory["REQ-1-2"]["observation_status"], "unverified")
            self.assertEqual(inventory["REQ-1-2"]["model_status"], "not_reported")
            observed["tests"][0]["executed"] = False
            self.assertEqual(main.requirement_inventory(self.module, self.valid, claims, observed, output)
                             ["REQ-1-1"]["observation_status"], "unverified")
            (output / "code.js").unlink()
            self.assertEqual(main.requirement_inventory(self.module, self.valid, claims, None, output)
                             ["REQ-1-1"]["model_status"], "missing_files")


class PlanningSessionTests(unittest.TestCase):
    def setUp(self):
        SourceContractTests.setUp(self)
    def test_planning_reads_copied_requirements_before_implementation_and_freezes_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requirements = root / "requirements"
            requirements.mkdir()
            (requirements / "requirements.yaml").write_text(json.dumps({
                'id': 'ROOT', 'description': 'Keep source readable', 'children': [self.module.subtree]}))
            (requirements / "reference.png").write_bytes(b"PNG evidence")
            (requirements / "app.py").write_text("product source", encoding="utf-8")
            output = root / "output"
            (output / "frontend").mkdir(parents=True)
            (output / "frontend" / "App.tsx").write_text("product code")
            events = []

            class Planner:
                def run(self, prompt):
                    events.append("plan")
                    self_outer.assertIn("source-required", prompt)
                    normalized_prompt = ' '.join(prompt.split())
                    self_outer.assertIn("Only assume startup-provisioned data when the source explicitly requires it", normalized_prompt)
                    self_outer.assertIn("Establish other prerequisite values through the visible UI in this test", normalized_prompt)
                    self_outer.assertIn("restore shared records changed by this test", normalized_prompt)
                    self_outer.assertNotIn(str(output), prompt)
                    self_outer.assertEqual((cwd / "requirements" / "reference.png").read_bytes(), b"PNG evidence")
                    self_outer.assertFalse((cwd / "requirements" / "app.py").exists())
                    self_outer.assertFalse((cwd / "frontend").exists())
                    (cwd / "checks.spec.js").write_bytes(b"test('REQ-1-1', async()=>{});")
                    return types.SimpleNamespace(error=None)

            class Codex:
                def thread_start(self, **kwargs):
                    nonlocal cwd
                    cwd = Path(kwargs["cwd"])
                    self_outer.assertNotEqual(cwd, output)
                    self_outer.assertEqual(kwargs["model"], "configured-model")
                    self_outer.assertNotIn(str(output), kwargs["developer_instructions"])
                    return Planner()

            self_outer = self
            cwd = None
            contract, script = main.plan_module_observation(
                Codex(), self.module, requirements, output, {"id": "ROOT"}, "configured-model")
            self.assertEqual(events, ["plan"])
            self.assertEqual(contract, self.valid)
            self.assertEqual((output / "module-observations" / "REQ-1" / "frozen-v1.spec.js").read_bytes(), script)


class PlanningFailureBoundaryTests(unittest.TestCase):
    def setUp(self):
        SourceContractTests.setUp(self)
        self.module = main.RequirementModule(1, 2, "REQ-1", "One", self.module.subtree)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.requirements = root / "requirements"
        self.requirements.mkdir()
        (self.requirements / "requirements.yaml").write_text("id: ROOT\ndescription: Open the home page.\n")
        self.output = root / "output"
        self.modules = [self.module, main.RequirementModule(2, 2, "REQ-2", "Two", {
            "id": "REQ-2", "description": "Second module.", "children": [
                {"id": "REQ-2-1", "type": "ATOMIC", "description": "Second item."}]})]
        (self.requirements / 'requirements.yaml').write_text(json.dumps({
            'id': 'ROOT', 'description': 'Open the home page.',
            'children': [module.subtree for module in self.modules]}))

    def execute(self, script_bytes, model_error=None):
        starts = []
        class Planner:
            def __init__(self, cwd): self.cwd = Path(cwd)
            def run(self, _prompt):
                if script_bytes is not None:
                    (self.cwd / "checks.spec.js").write_bytes(script_bytes)
                return types.SimpleNamespace(error=model_error, status="completed")
        class Codex:
            def thread_start(self, **kwargs):
                starts.append(kwargs["cwd"])
                return Planner(kwargs["cwd"])
        with self.assertRaises(Exception) as failure:
            main.plan_module_observation(Codex(), self.module, self.requirements, self.output,
                                         main.load_root_context(self.requirements), None)
        return main.error_evidence(failure.exception), starts

    def test_missing_and_empty_script_retained_without_retry(self):
        for script, reason in ((None, "planner omitted checks.spec.js"), (b"", "empty script")):
            with self.subTest(script=script):
                failure, starts = self.execute(script)
                folder = self.output / "module-observations" / "REQ-1"
                if script is not None:
                    self.assertEqual((folder / "checks.spec.js").read_bytes(), script)
                self.assertIn(reason, failure["message"])
                self.assertEqual(len(starts), 1)

    def test_planning_error_preserves_script_and_redacts_environment_credentials(self):
        secrets = {"APP_PASSWORD": "pw-secret-12345", "SESSION_AUTH": "auth-secret-67890"}
        script = b'const password="demo-login-123"; // ' + b' '.join(value.encode() for value in secrets.values())
        with patch.dict(os.environ, secrets):
            failure, starts = self.execute(script, RuntimeError("planner transport stopped: " + " ".join(secrets.values())))
        saved = (self.output / "module-observations/REQ-1/checks.spec.js").read_bytes()
        self.assertIn(b"demo-login-123", saved)
        self.assertIn("planner transport stopped", failure["message"])
        self.assertEqual(len(starts), 1)
        for secret in secrets.values():
            self.assertNotIn(secret.encode(), saved)
            self.assertNotIn(secret, json.dumps(failure))


class ModuleObservationTests(unittest.TestCase):
    def test_discovery_rejects_extra_or_wrong_title_before_execution(self):
        report = self.report(["expected"] * 3)
        titles = [spec["title"] for spec in report["suites"][0]["specs"]]
        with self.assertRaisesRegex(ValueError, "chain"):
            main.validate_discovered_chains(report, titles[:2])
        self.assertEqual(main.validate_discovered_chains(report, titles), titles)
        with self.assertRaisesRegex(ValueError, "chain"):
            main.validate_discovered_chains(report, ["changed", *titles[1:]])

    def report(self, statuses):
        return {"errors": [], "suites": [{"specs": [
            {"title": f"REQ-1-1 | chain-{index}", "tests": [{
                "expectedStatus": "passed", "status": status,
                "results": [{"status": "passed" if status == "expected" else
                             ("skipped" if status == "skipped" else "failed"),
                             "duration": 20, "errors": [] if status == "expected" else
                             [{"message": "Account menu was not found"}]}],
            }]} for index, status in enumerate(statuses, 1)
        ]}]}

    def test_empty_report_is_not_passed(self):
        self.assertEqual(main.parse_observation_report({"suites": [], "errors": []}, 0)["status"], "error")

    def test_unexpected_failure_is_failed(self):
        result = main.parse_observation_report(self.report(["unexpected"]), 1)
        self.assertEqual(result["status"], "failed")
        self.assertIn("Account menu was not found", json.dumps(result))

    def test_skipped_observation_is_not_passed(self):
        self.assertNotEqual(main.parse_observation_report(self.report(["skipped"]), 0)["status"], "passed")

    def test_all_independent_executed_passes_are_required(self):
        for statuses in (["expected"], ["expected", "expected"], ["expected"] * 3):
            with self.subTest(statuses=statuses):
                self.assertEqual(main.parse_observation_report(self.report(statuses), 0)["status"], "passed")
        self.assertNotEqual(main.parse_observation_report(self.report(["expected"]), 1)["status"], "passed")

    def test_expected_status_without_executed_result_is_error(self):
        report = self.report(["expected"])
        report["suites"][0]["specs"][0]["tests"][0]["results"] = []
        self.assertEqual(main.parse_observation_report(report, 0)["status"], "error")

    def test_natural_chain_title_is_accepted_in_observation_report(self):
        report = self.report(["expected"])
        report["suites"][0]["specs"][0]["title"] = "Create and confirm an item"
        self.assertEqual(main.parse_observation_report(report, 0)["status"], "passed")

    def test_real_element_timeout_counts_as_executed_failure(self):
        report = self.report(["unexpected"])
        attempt = report["suites"][0]["specs"][0]["tests"][0]["results"][0]
        attempt["status"] = "timedOut"
        attempt["duration"] = 30057
        attempt["errors"] = [{"message": "locator.click: Test timeout of 30000ms exceeded"}]
        result = main.parse_observation_report(report, 1)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["tests"][0]["executed"])
        self.assertEqual(result["tests"][0]["duration_ms"], 30057)

    def test_process_timeout_overrides_partial_unexpected_report(self):
        report = self.report(["unexpected"])
        result = main.parse_observation_report(report, -1, process_timed_out=True)
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["process_timed_out"])
        self.assertEqual(result["tests"][0]["status"], "unexpected")

    def test_missing_script_retains_error_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            module = main.RequirementModule(1, 1, "REQ-1", "One", {"id": "REQ-1"})
            result = main.run_module_observation(Path(directory), module, "before")
            self.assertEqual(result["status"], "error")
            evidence = Path(directory) / result["evidence_path"]
            self.assertTrue((evidence / "result.json").is_file())
            self.assertTrue((evidence / "stderr.txt").is_file())


class ModuleObservationFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.requirements = self.root / "requirements"
        self.requirements.mkdir()
        (self.requirements / "requirements.yaml").write_text(
            "id: ROOT\nchildren:\n  - id: REQ-1\n    name: First\n"
            "  - id: REQ-2\n    name: Second\n", encoding="utf-8")
        (self.requirements / 'requirements.yaml').write_text(json.dumps({'id': 'ROOT', 'children': [
            {'id': identifier, 'name': identifier, 'description': 'name', 'children': [
                {'id': identifier + '-' + str(index), 'type': 'ATOMIC', 'description': 'Neutral rule'}
                for index in (1, 2)]} for identifier in ('REQ-1', 'REQ-2')]}))
        self.output = self.root / "output"
        FakeCodex.failure = None
        FakeCodex.break_build = False
        FakeCodex.return_error = None
        FakeCodex.failure_message = "429 Too Many Requests"
        FakeCodex.actions = {}
        FakeCodex.repair_action = None
        FakeCodex.threads = []
        self.patchers = [patch.object(main, "Codex", FakeCodex),
                         patch.object(main, "build_frontend", return_value={"status": "passed"}),
                         patch.object(main, "verify_backend_startup", return_value={"status": "passed"}),
                         patch.object(main, "plan_module_observation", side_effect=fake_plan)]
        for item in self.patchers:
            item.start()
            self.addCleanup(item.stop)

    def execute(self, output=None, resume=None):
        args = ["main.py", str(self.requirements), "--output-dir", str(output or self.output)]
        if resume:
            args += ["--resume-from", str(resume)]
        with patch.object(sys, "argv", args), patch.dict(os.environ, {"MODEL": "fake", "OPENAI_API_KEY": ""}):
            return main.main()

    def observe(self, sequence):
        calls = []
        latest = {}

        def fake(output, module, phase):
            calls.append((module.node_id, phase))
            status = sequence.pop(0) if sequence else latest[module.node_id]
            latest[module.node_id] = status
            evidence = output / "module-observations" / module.node_id / phase
            evidence.mkdir(parents=True, exist_ok=True)
            (evidence / "checks.spec.js").write_text("// original two chains", encoding="utf-8")
            (evidence / "result.json").write_text("{}", encoding="utf-8")
            return {"status": status, "tests": [
                {"title": module.node_id + "-1", "status": "expected", "executed": True, "result_status": "passed"},
                {"title": module.node_id + "-2", "status": "expected" if status == "passed" else "unexpected", "executed": True, "result_status": "passed" if status == "passed" else "failed"}],
                "duration_seconds": 0.1, "evidence_path": evidence.relative_to(output).as_posix()}

        patcher = patch.object(main, "run_module_observation", side_effect=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def summary(self):
        return json.loads((self.output / ".arc" / "codex-recovery" / "summary.json").read_text())

    def test_first_pass_needs_no_repair(self):
        calls = self.observe(["passed", "passed"])
        self.assertEqual(self.execute(), 0)
        self.assertEqual(calls, [("REQ-1", "before"), ("REQ-2", "before")])
        self.assertEqual(len(FakeCodex.threads), 2)
        self.assertIsNone(self.summary()["module_results"][0]["observation"]["after"])

    def test_failed_observation_repairs_application_without_reviewing_or_replacing_script(self):
        calls = self.observe(["failed", "passed", "passed"])
        self.assertEqual(self.execute(), 0)
        folder = self.output / "module-observations" / "REQ-1"
        self.assertEqual(calls, [("REQ-1", "before"), ("REQ-2", "before"),
                                 ("REQ-1", "after"), ("REQ-2", "after")])
        self.assertEqual(len(FakeCodex.threads), 3)
        self.assertFalse((folder / "correction.json").exists())
        self.assertFalse((folder / "frozen-v2.spec.js").exists())
        self.assertEqual((folder / "frozen-v1.spec.js").read_bytes(),
                         (folder / "checks.spec.js").read_bytes())
        self.assertEqual(self.summary()["module_results"][0]["observation"]["status"], "passed")

    def test_configuration_error_is_recorded_without_application_repair(self):
        calls = self.observe(["error", "passed"])
        self.assertEqual(self.execute(), 0)
        self.assertEqual(calls, [("REQ-1", "before"), ("REQ-2", "before")])
        self.assertEqual(len(FakeCodex.threads), 2)
        record = self.summary()["module_results"][0]["observation"]
        self.assertEqual(record["status"], "error")
        self.assertIsNone(record["after"])
        self.assertEqual(self.summary()["pending_issues"], [])

    def test_planning_quota_stops_coding_without_completed_source(self):
        with patch.object(main, "plan_module_observation", side_effect=RuntimeError("Quota exceeded")) as planner:
            self.assertEqual(self.execute(), 1)
        self.assertEqual(self.summary()["status"], "blocked")
        self.assertEqual(self.summary()['unverified_count'], 4)
        self.assertEqual(planner.call_count, 1)
        self.assertFalse((self.output / 'REQ-1.txt').is_file() or (self.output / 'REQ-2.txt').is_file())
        self.assertEqual(FakeCodex.threads, [])

    def test_failure_repairs_once_and_rechecks_both_chains(self):
        calls = self.observe(["failed", "passed", "passed"])
        self.assertEqual(self.execute(), 0)
        self.assertEqual(calls, [("REQ-1", "before"), ("REQ-2", "before"),
                                 ("REQ-1", "after"), ("REQ-2", "after")])
        self.assertEqual(len(FakeCodex.threads), 3)
        self.assertEqual(self.summary()["module_results"][0]["observation"]["status"], "passed")
        self.assertEqual((self.output / "REQ-1.txt").read_text(), "repaired")
        self.assertIn("naming each changed file", FakeCodex.threads[2].prompts[0])
        self.assertIn("concrete fix", FakeCodex.threads[2].prompts[0])
        self.assertIn("repair-note.txt", FakeCodex.threads[2].prompts[0])
        self.assertIn("bounded targeted checks", FakeCodex.threads[2].prompts[0])
        self.assertIn("nonzero exit", FakeCodex.threads[2].prompts[0])
        self.assertIn("unverified", FakeCodex.threads[2].prompts[0])
        self.assertIn("documented formal startup entry", FakeCodex.threads[2].prompts[0])
        self.assertIn("observable state and data", FakeCodex.threads[2].prompts[0])
        self.assertIn("Leave already-correct source", FakeCodex.threads[2].prompts[0])
        self.assertIn("confirmed residual failure", FakeCodex.threads[2].prompts[0])

    def test_unfixed_failure_continues_without_technical_issue(self):
        calls = self.observe(["failed", "failed", "failed", "failed"])
        self.assertEqual(self.execute(), 0)
        self.assertEqual(calls, [("REQ-1", "before"), ("REQ-2", "before"),
                                 ("REQ-1", "after"), ("REQ-2", "after")])
        self.assertEqual(self.summary()["pending_issues"], [])
        self.assertEqual([issue["module_id"] for issue in self.summary()["observation_issues"]],
                         ["REQ-1", "REQ-2"])
        self.assertEqual(self.summary()["status"], "complete")
        self.assertIn('"module_id": "REQ-1"', FakeCodex.threads[-1].prompts[0])
        self.assertIn('"module_id": "REQ-2"', FakeCodex.threads[-1].prompts[0])

    def test_no_valid_result_stays_error_without_application_repair(self):
        self.observe(["error", "passed"])
        self.assertEqual(self.execute(), 0)
        self.assertEqual(self.summary()["module_results"][0]["observation"]["status"], "error")
        self.assertIsNone(self.summary()["module_results"][0]["observation"]["after"])
        self.assertEqual(len(FakeCodex.threads), 2)

    def test_repair_build_failure_restores_pre_repair_source_and_delivers(self):
        self.observe(["passed", "failed"])

        def build(_output):
            if 'Observation repair' in FakeCodex.thread.prompts[-1]:
                raise RuntimeError("repair broke build")
            return {"status": "passed"}

        with patch.object(main, "build_frontend", side_effect=build):
            self.assertEqual(self.execute(), 0)
        latest = json.loads((self.output / ".arc" / "codex-recovery" / "latest.json").read_text())
        self.assertEqual(latest["completed_ids"], ["REQ-1", "REQ-2"])
        self.assertEqual(len(FakeCodex.threads), 3)
        # The rejected repair is undone; the pre-repair candidate is what gets delivered.
        self.assertEqual((self.output / "REQ-2.txt").read_text(), "REQ-2")
        self.assertEqual(self.summary()["module_results"][1]["observation"]["status"], "failed")
        self.assertTrue(self.summary()["acceptance"]["repair"]["restored"])
        self.assertEqual(self.summary()["delivery"]["exit_code"], 0)

    def test_functional_failure_exits_zero_in_real_subprocess(self):
        code = "\n".join([
            "import os, sys, types",
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
            "main.run_module_observation=lambda output,module,phase: {'status':'failed' if module.node_id=='REQ-1' else 'passed', 'tests':[{'title':module.node_id+'-1','status':'unexpected' if module.node_id=='REQ-1' else 'expected','executed':True,'result_status':'failed' if module.node_id=='REQ-1' else 'passed'}, {'title':module.node_id+'-2','status':'unexpected' if module.node_id=='REQ-1' else 'expected','executed':True,'result_status':'failed' if module.node_id=='REQ-1' else 'passed'}], 'duration_seconds':0.1, 'evidence_path':'module-observations/'+module.node_id+'/'+phase}",
            "sys.argv=['main.py',sys.argv[1],'--output-dir',sys.argv[2]]",
            "raise SystemExit(main.main())",
        ])
        result = subprocess.run([sys.executable, "-c", code, str(self.requirements), str(self.output)],
                                cwd=Path(main.__file__).parent, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.summary()["status"], "complete")
        self.assertEqual(len(self.summary()["observation_issues"]), 1)

    def test_repaired_checkpoint_restores_final_source_and_evidence(self):
        self.observe(["failed", "passed", "passed"])
        self.assertEqual(self.execute(), 0)
        latest = json.loads((self.output / ".arc" / "codex-recovery" / "latest.json").read_text())
        source = self.output / ".arc" / "codex-recovery" / "checkpoints" / latest["checkpoint"] / "source"
        self.assertEqual((source / "REQ-1.txt").read_text(), "repaired")
        self.assertTrue((source / "module-observations" / "REQ-1" / "after" / "result.json").is_file())
        resumed = self.root / "resumed"
        self.assertEqual(self.execute(resumed, self.output), 0)
        self.assertEqual((resumed / "REQ-1.txt").read_text(), "repaired")
        self.assertTrue((resumed / "module-observations" / "summary.json").is_file())
        resumed_summary = json.loads((resumed / ".arc" / "codex-recovery" / "summary.json").read_text())
        self.assertEqual(resumed_summary["module_results"][0]["observation"]["status"], "passed")

    def test_repair_quota_delivers_candidate_without_nested_retry(self):
        self.observe(["failed"])
        FakeCodex.repair_action = "quota"
        self.assertEqual(self.execute(), 0)
        self.assertEqual(len(FakeCodex.threads), 3, "quota denial must not add a nested repair call")
        self.assertEqual(self.summary()["status"], "complete", "acceptance must not rewrite generation")
        self.assertEqual(self.summary()["delivery"]["exit_code"], 0)
        self.assertTrue((self.output / "REQ-1.txt").is_file() and (self.output / "REQ-2.txt").is_file())
        self.assertEqual(self.summary()["acceptance"]["technical_failure"]["stage"], "observation_repair")
        self.assertTrue(self.summary()["acceptance"]["repair"]["restored"])

    def test_repair_connection_failure_delivers_candidate_without_nested_retry(self):
        self.observe(["failed"])
        FakeCodex.repair_action = "connection"
        self.assertEqual(self.execute(), 0)
        self.assertEqual(len(FakeCodex.threads), 3)
        self.assertEqual(self.summary()["status"], "complete")
        self.assertEqual(self.summary()["delivery"]["status"], "ready")

    def test_interrupted_repair_restores_pre_repair_source_and_delivers(self):
        self.observe(["failed", "passed", "passed"])
        FakeCodex.repair_action = "corrupt_connection"
        self.assertEqual(self.execute(), 0)
        latest = json.loads((self.output / ".arc" / "codex-recovery" / "latest.json").read_text())
        self.assertEqual(latest["completed_ids"], ["REQ-1", "REQ-2"])
        # A repair that damaged the tree is undone: the file it added is gone and R0 bytes are back.
        self.assertFalse((self.output / "damaged.txt").exists())
        self.assertEqual((self.output / "REQ-1.txt").read_text(), "REQ-1")
        self.assertEqual((self.output / "REQ-2.txt").read_text(), "REQ-2")
        self.assertTrue((self.output / "module-observations" / "REQ-1" / "before" / "result.json").is_file())
        self.assertTrue(self.summary()["acceptance"]["repair"]["restored"])
        self.assertEqual(self.summary()["delivery"]["exit_code"], 0)
        FakeCodex.repair_action = None
        previous_threads = len(FakeCodex.threads)
        resumed = self.root / "resumed"
        self.assertEqual(self.execute(resumed, self.output), 0)
        self.assertEqual((resumed / "REQ-1.txt").read_text(), "REQ-1")
        self.assertEqual(len(FakeCodex.threads), previous_threads)
        self.assertEqual((resumed / 'REQ-2.txt').read_text(), 'REQ-2')

    def test_repair_cannot_drop_the_initial_second_chain(self):
        calls = self.observe(["failed", "passed", "passed"])
        original = main.run_module_observation

        def missing_second(output, module, phase):
            result = original(output, module, phase)
            if module.node_id == "REQ-1" and phase == "after":
                result["tests"] = result["tests"][:1]
            return result

        with patch.object(main, "run_module_observation", side_effect=missing_second):
            self.assertEqual(self.execute(), 0)
        self.assertEqual(len(calls), 4)
        self.assertEqual(self.summary()["module_results"][0]["observation"]["status"], "error")

    def test_repair_that_replaces_a_frozen_script_is_recorded_and_still_delivered(self):
        self.observe(["failed", "passed", "passed"])
        FakeCodex.repair_action = "create_script"
        self.assertEqual(self.execute(), 0)
        first = self.summary()["module_results"][0]["observation"]
        self.assertEqual(first["initial_status"], "failed")
        self.assertEqual(first["status"], "error")
        self.assertIn("script_change_error", first["after"])
        self.assertEqual(first["requirement_ids"], ["REQ-1-1", "REQ-1-2"])
        self.assertEqual(first["chain_titles"], ["REQ-1-1", "REQ-1-2"])
        self.assertEqual(self.summary()["delivery"]["exit_code"], 0)

    def test_new_script_without_source_note_is_error_and_continues(self):
        self.observe(["failed", "passed", "passed"])
        FakeCodex.repair_action = "create_script_without_note"
        self.assertEqual(self.execute(), 0)
        first = self.summary()["module_results"][0]["observation"]
        self.assertEqual(first["status"], "error")
        self.assertIn("script_change_error", first["after"])
        self.assertEqual(self.summary()["pending_issues"], [])
        self.assertEqual(len(FakeCodex.threads), 3)
        self.assertIn("repair-note.txt", FakeCodex.threads[2].prompts[0])
        # The host never fabricates repair evidence for the model.
        self.assertFalse((self.output / "module-observations" / "repair-note.txt").exists())


@unittest.skipUnless(shutil.which("npm"), "npm is required for backend startup checks")
class BackendStartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "app"
        main.copy_template_contents(Path(main.__file__).parent / "template", self.output)

    def free_port(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    def assert_port_closed(self, port):
        with socket.socket() as probe:
            probe.settimeout(1)
            self.assertNotEqual(probe.connect_ex(("127.0.0.1", port)), 0)

    def test_real_frontend_build_and_backend_startup_detects_bad_entry(self):
        self.assertEqual(main.build_frontend(self.output)["status"], "passed")
        port = self.free_port()
        self.assertEqual(main.verify_backend_startup(self.output, port=port)["status"], "passed")
        self.assert_port_closed(port)
        (self.output / "backend" / "src" / "index.js").write_text(
            "throw new Error('broken-entry-marker');\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(RuntimeError, "broken-entry-marker"):
            main.verify_backend_startup(self.output, port=port)
        self.assert_port_closed(port)

    def test_startup_uses_fresh_database_and_preserves_working_database(self):
        backend = self.output / "backend"
        working_db = backend / "database.db"
        database = sqlite3.connect(working_db)
        try:
            database.execute("CREATE TABLE items (name TEXT NOT NULL)")
            database.execute("INSERT INTO items (name) VALUES ('existing-item')")
            database.commit()
        finally:
            database.close()
        original_bytes = working_db.read_bytes()
        (backend / "probe.py").write_text(
            "import os, sqlite3, sys\n"
            "db_path = os.environ['ARC_DB_FILE']\n"
            "assert db_path == os.environ['DATABASE_FILE']\n"
            "open('observed-db-path.txt', 'w').write(db_path)\n"
            "with sqlite3.connect(db_path) as db:\n"
            "    db.execute('CREATE TABLE IF NOT EXISTS items (name TEXT NOT NULL)')\n"
            "    if not db.execute('SELECT COUNT(*) FROM items').fetchone()[0]:\n"
            "        try:\n"
            "            db.execute(\"INSERT INTO items (name, origin_id) "
            "VALUES ('new-item', 1)\")\n"
            "        except sqlite3.OperationalError as error:\n"
            "            print(f'SQLITE_ERROR: {error}', file=sys.stderr)\n"
            "            sys.exit(1)\n",
            encoding="utf-8",
        )
        (backend / "src" / "index.js").write_text(
            "const { spawnSync } = require('node:child_process');\n"
            "const http = require('node:http');\n"
            f"const result = spawnSync({json.dumps(sys.executable)}, ['probe.py'], "
            "{ cwd: process.cwd(), env: process.env, encoding: 'utf8' });\n"
            "if (result.status !== 0) { process.stderr.write(result.stderr); process.exit(1); }\n"
            "http.createServer((req, res) => { res.writeHead(200); res.end('ready'); })"
            ".listen(Number(process.env.PORT), '127.0.0.1');\n",
            encoding="utf-8",
        )
        (backend / "package.json").write_text('{"scripts":{"start":"node src/index.js"}}', encoding="utf-8")
        port = self.free_port()
        env = {**os.environ, "PORT": str(port), "ARC_DB_FILE": str(working_db),
               "DATABASE_FILE": str(working_db)}
        control = subprocess.Popen(["node", "src/index.js"], cwd=backend, env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            for _ in range(50):
                if control.poll() is not None:
                    self.fail(f"populated database control exited: {control.communicate()[1]!r}")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2) as client:
                        client.sendall(b"GET / HTTP/1.0\r\nHost: localhost\r\n\r\n")
                        self.assertIn(b"200", client.recv(1024))
                        break
                except OSError:
                    time.sleep(0.05)
            else:
                self.fail("populated database control did not serve HTTP")
        finally:
            control.terminate()
            control.communicate(timeout=2)
        self.assertEqual(working_db.read_bytes(), original_bytes)
        with patch.object(main, "ensure_project_dependencies"):
            with patch.dict(os.environ, {"ARC_DB_FILE": str(working_db),
                                             "DATABASE_FILE": str(working_db)}):
                with self.assertRaisesRegex(RuntimeError, "SQLITE_ERROR.*origin_id"):
                    main.verify_backend_startup(self.output, port=port)
        observed = Path((backend / "observed-db-path.txt").read_text(encoding="utf-8"))
        self.assertNotEqual(observed, working_db)
        self.assertFalse(observed.exists())
        self.assertFalse(observed.parent.exists())
        self.assertEqual(working_db.read_bytes(), original_bytes)
        self.assert_port_closed(port)

    def test_http_200_does_not_hide_logged_sqlite_schema_failure(self):
        backend = self.output / "backend"
        (backend / "package.json").write_text('{"scripts":{"start":"node src/index.js"}}', encoding="utf-8")
        (backend / "probe.py").write_text(
            "import sqlite3\n"
            "db = sqlite3.connect(':memory:')\n"
            "db.execute('CREATE TABLE items (name TEXT NOT NULL)')\n"
            "try:\n"
            "    db.execute(\"INSERT INTO items (name, origin_id) VALUES ('new-item', 1)\")\n"
            "except sqlite3.OperationalError as error:\n"
            "    print(f'SQLITE_ERROR: {error} token=secret-example')\n"
            "else:\n"
            "    raise AssertionError('fixture did not violate schema')\n",
            encoding="utf-8",
        )
        (backend / "src" / "index.js").write_text(
            "const http = require('node:http');\n"
            "const { spawnSync } = require('node:child_process');\n"
            f"const result = spawnSync({json.dumps(sys.executable)}, ['probe.py'], "
            "{ cwd: process.cwd(), encoding: 'utf8' });\n"
            "if (result.status !== 0) throw new Error(result.stderr);\n"
            "console.error(result.stdout.trim());\n"
            "http.createServer((req, res) => {res.writeHead(200); res.end('ready');})"
            ".listen(Number(process.env.PORT), '127.0.0.1');\n",
            encoding="utf-8",
        )
        port = self.free_port()
        with patch.object(main, "ensure_project_dependencies"):
            with self.assertRaisesRegex(RuntimeError, "SQLITE_ERROR.*origin_id") as failure:
                main.verify_backend_startup(self.output, port=port)
        self.assertNotIn("secret-example", str(failure.exception))
        self.assert_port_closed(port)

    def test_http_200_does_not_hide_caught_sqlite_not_null_failure(self):
        backend = self.output / "backend"
        (backend / "node_modules").mkdir()
        (backend / "probe.py").write_text(
            "import sqlite3, sys\n"
            "db = sqlite3.connect(sys.argv[1])\n"
            "db.execute('CREATE TABLE IF NOT EXISTS probe (value TEXT NOT NULL)')\n"
            "try:\n"
            "    db.execute('INSERT INTO probe DEFAULT VALUES')\n"
            "except sqlite3.IntegrityError as error:\n"
            "    print(f'{error.sqlite_errorname}: {error} token=secret-example')\n"
            "else:\n"
            "    raise AssertionError('fixture did not violate NOT NULL')\n",
            encoding="utf-8",
        )
        (backend / "src" / "index.js").write_text(
            "const http = require('node:http');\n"
            "const fs = require('node:fs');\n"
            "const path = require('node:path');\n"
            "const {spawnSync} = require('node:child_process');\n"
            f"const python = {json.dumps(sys.executable)};\n"
            "const db = path.join(__dirname, '..', 'probe.sqlite');\n"
            "const result = spawnSync(python, ['probe.py', db], {cwd: path.join(__dirname, '..'), encoding: 'utf8'});\n"
            "if (result.status !== 0) throw new Error(result.stderr);\n"
            "console.error('ordinary startup output '.repeat(300));\n"
            "console.error(result.stdout.trim());\n"
            "http.createServer((req, res) => {\n"
            "  fs.writeFileSync(path.join(__dirname, '..', 'http-200-observed'), 'yes');\n"
            "  res.writeHead(200); res.end('ready');\n"
            "}).listen(Number(process.env.PORT), '127.0.0.1');\n",
            encoding="utf-8",
        )
        port = self.free_port()
        with self.assertRaisesRegex(RuntimeError, "SQLITE_CONSTRAINT_NOTNULL") as failure:
            main.verify_backend_startup(self.output, port=port)
        self.assertTrue((backend / "http-200-observed").is_file())
        self.assertTrue((backend / "probe.sqlite").is_file())
        database = sqlite3.connect(backend / "probe.sqlite")
        try:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM probe").fetchone()[0], 0)
        finally:
            database.close()
        self.assertIn("NOT NULL constraint failed", str(failure.exception))
        self.assertNotIn("secret-example", str(failure.exception))
        self.assert_port_closed(port)
        with self.assertRaisesRegex(RuntimeError, "SQLITE_CONSTRAINT_NOTNULL"):
            main.verify_backend_startup(self.output, port=port)
        self.assert_port_closed(port)

    def test_http_200_with_nonfatal_warning_passes(self):
        backend = self.output / "backend"
        (backend / "node_modules").mkdir()
        (backend / "src" / "index.js").write_text(
            "const http = require('node:http');\n"
            "console.error('Warning: optional cache unavailable');\n"
            "http.createServer((req, res) => {res.writeHead(200); res.end('ready');})"
            ".listen(Number(process.env.PORT), '127.0.0.1');\n",
            encoding="utf-8",
        )
        port = self.free_port()
        self.assertEqual(main.verify_backend_startup(self.output, port=port)["status"], "passed")
        self.assert_port_closed(port)

    def test_backend_startup_timeout_cleans_its_own_service(self):
        (self.output / "frontend" / "dist").mkdir()
        (self.output / "frontend" / "dist" / "index.html").write_text("ready", encoding="utf-8")
        (self.output / "backend" / "node_modules").mkdir()
        (self.output / "backend" / "src" / "index.js").write_text(
            "const net = require('node:net');\n"
            "net.createServer(() => {}).listen(Number(process.env.PORT), '127.0.0.1');\n",
            encoding="utf-8",
        )
        port = self.free_port()
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            main.verify_backend_startup(self.output, port=port, timeout_seconds=2)
        self.assert_port_closed(port)


class SummarySemanticsTests(unittest.TestCase):
    setUp = ModuleObservationFlowTests.setUp
    execute = ModuleObservationFlowTests.execute
    summary = ModuleObservationFlowTests.summary

    def test_reporting_callback_failure_keeps_business_error_and_summary(self):
        from arcbench_agent_runtime.events import EventClient
        FakeCodex.failure = 'REQ-1'
        FakeCodex.failure_message = 'HTTP 401 original coding denial'
        with patch.object(EventClient, 'mark_implementation_failed', side_effect=RuntimeError('report sink failed')):
            self.assertEqual(self.execute(), 1)
        self.assertIn('original coding denial', self.summary()['pending_issues'][0]['error']['message'])
        self.assertTrue(self.summary()['reporting_issues'])


class RoundBFaultTests(unittest.TestCase):
    def test_snapshot_pointer_is_unchanged_after_publication_interruption(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / 'output'
            output.mkdir()
            (output / 'value.txt').write_text('first')
            main.save_checkpoint(output, ['REQ-1'], {}, {'status':'passed'})
            pointer = output / '.arc/codex-recovery/latest.json'
            before = pointer.read_bytes()
            (output / 'value.txt').write_text('second')
            original = main.atomic_json
            def interrupted(path, payload):
                if path.name == 'latest.json': raise OSError('injected pointer write interruption')
                return original(path, payload)
            with patch.object(main, 'atomic_json', side_effect=interrupted), self.assertRaises(OSError):
                main.save_checkpoint(output, ['REQ-1','REQ-2'], {}, {'status':'passed'})
            self.assertEqual(pointer.read_bytes(), before)
            restored = root / 'restored'
            restored.mkdir()
            self.assertEqual(main.restore_checkpoint(output, restored, {}, root), ['REQ-1'])
            self.assertEqual((restored / 'value.txt').read_text(), 'first')


class EvidenceIntegrityTests(unittest.TestCase):
    def test_port_is_not_an_http_status_and_explicit_statuses_still_work(self):
        self.assertEqual(main.error_evidence(RuntimeError('read tcp 10.0.0.1:52545->119.3.253.96:443: connection reset by peer'))['status_code'], 'unavailable')
        for text, code in [('HTTP 503','503'), ('status: 429','429')]:
            self.assertEqual(main.error_evidence(RuntimeError(text))['status_code'], code)

    def test_reporting_failure_does_not_replace_original_error(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            callback = lambda *_args: (_ for _ in ()).throw(RuntimeError('reporting failed token=secret-example'))
            helper = getattr(main, 'emit_runtime_event', None)
            self.assertTrue(callable(helper), 'isolated reporting helper is missing')
            helper(callback, output, 'REQ-1')
            self.assertIn('reporting', (output / '.arc/codex-recovery/events.jsonl').read_text())

    def test_nested_same_name_screenshots_keep_full_path_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            results = output / 'test-results'
            first, second = results / 'one/test.png', results / 'two/test.png'
            for path, value in ((first,b'one'), (second,b'two')):
                path.parent.mkdir(parents=True)
                path.write_bytes(value)
            def spec(path):
                return {'tests':[{'results':[{'attachments':[{'path':str(path)}]}]}]}
            report = {'suites':[{'specs':[spec(first)],'suites':[{'specs':[spec(second)]}]}]}
            evidence = output / 'evidence'
            evidence.mkdir()
            main.copy_observation_attachments(report, results, evidence, output)
            left = report['suites'][0]['specs'][0]['tests'][0]['results'][0]['attachments'][0]['path']
            right = report['suites'][0]['suites'][0]['specs'][0]['tests'][0]['results'][0]['attachments'][0]['path']
            self.assertNotEqual(left, right)
            self.assertEqual((output / left).read_bytes(), b'one')
            self.assertEqual((output / right).read_bytes(), b'two')


class FinalAcceptanceVersionTests(unittest.TestCase):
    execute = ModuleObservationFlowTests.execute
    summary = ModuleObservationFlowTests.summary

    def setUp(self):
        ModuleObservationFlowTests.setUp(self)
        (self.requirements / 'requirements.yaml').write_text(json.dumps({'id': 'ROOT', 'children': [
            {'id': identifier, 'name': identifier, 'description': 'name', 'children': [
                {'id': identifier + '-' + str(index), 'type': 'ATOMIC', 'description': 'Neutral source rule'}
                for index in (1, 2)]} for identifier in ('REQ-1', 'REQ-2')]}))
        self.calls = []

    def acceptance(self):
        summary = self.summary()
        self.assertIn('acceptance', summary, 'final acceptance is not connected')
        return summary['acceptance']

    def mixed_atomic_result(self, output, module, phase, atomic_id):
        evidence = output / 'module-observations' / module.node_id / (phase + '-' + atomic_id)
        evidence.mkdir(parents=True, exist_ok=True)
        script_error = atomic_id == 'REQ-1-2'
        business_failure = atomic_id == 'REQ-1-1' and phase == 'before'
        error = ('TypeError: broken observation helper' if script_error else
                 'Error: Source-required committed state is wrong' if business_failure else '')
        passed = not error
        report = {'errors': [], 'suites': [{'specs': [{
            'title': atomic_id, 'tests': [{'status': 'expected' if passed else 'unexpected',
                                         'results': [{'status': 'passed' if passed else 'failed',
                                                      'duration': 1,
                                                      'errors': [{'message': error}] if error else []}]}]}]}]}
        (evidence / 'result.json').write_text(json.dumps(report), encoding='utf-8')
        (evidence / 'stdout.txt').write_text('one selected atomic test', encoding='utf-8')
        (evidence / 'stderr.txt').write_text(error, encoding='utf-8')
        return {**main.parse_observation_report(report, 0 if passed else 1),
                'exit_code': 0 if passed else 1, 'duration_seconds': 0.01,
                'evidence_path': evidence.relative_to(output).as_posix(),
                'discovered_titles': list(module.chain_titles)}

    def test_mixed_atomic_failure_and_script_error_repairs_only_business_failure_once(self):
        with patch.object(main, '_run_atomic_observation', side_effect=self.mixed_atomic_result), patch.object(
                main, 'repair_final_candidate', wraps=main.repair_final_candidate) as repair:
            self.assertEqual(self.execute(), 0)
        acceptance = self.acceptance()
        first = acceptance['records'][0]
        self.assertEqual(first['before']['status'], 'error')
        self.assertEqual(first['status'], 'error')
        self.assertEqual(acceptance['repair']['failed_atoms'], ['REQ-1-1'])
        self.assertTrue(acceptance['repair']['attempted'])
        self.assertEqual(repair.call_count, 1)
        self.assertEqual(len(FakeCodex.threads), 3)
        failures = repair.call_args.args[-1]
        self.assertEqual([item['atomic_id'] for item in failures], ['REQ-1-1'])
        self.assertEqual(failures[0]['module_id'], 'REQ-1')
        self.assertEqual(failures[0]['requirement_file'], str((self.requirements / 'requirements.yaml').resolve()))
        self.assertEqual(first['requirements']['REQ-1-2']['observation_status'], 'unverified')
        self.assertEqual(first['requirements']['REQ-1-1']['observation_status'], 'observed_passed')

    def test_mixed_atomic_failure_then_planning_quota_keeps_failure_without_repair_call(self):
        def plan(codex, module, requirements, output, root, model):
            if module.node_id == 'REQ-2':
                raise RuntimeError('Quota exceeded')
            return fake_plan(codex, module, requirements, output, root, model)

        with patch.object(main, '_run_atomic_observation', side_effect=self.mixed_atomic_result), patch.object(
                main, 'plan_module_observation', side_effect=plan), patch.object(
                main, 'repair_final_candidate', wraps=main.repair_final_candidate) as repair:
            self.assertEqual(self.execute(), 0)
        acceptance = self.acceptance()
        self.assertEqual(acceptance['records'][0]['before']['status'], 'error')
        self.assertEqual(acceptance['records'][0]['status'], 'error')
        self.assertEqual(acceptance['chain_counts']['failed'], 1)
        self.assertEqual(acceptance['repair']['failed_atoms'], ['REQ-1-1'])
        self.assertFalse(acceptance['repair']['attempted'])
        self.assertIn('Quota exceeded', acceptance['repair']['blocked_reason']['error']['message'])
        self.assertEqual(repair.call_count, 0)
        self.assertEqual(len(FakeCodex.threads), 2)

    def test_local_script_error_preserves_independent_pass_inventory_and_counts(self):
        from arcbench_agent_runtime.events import EventClient
        payload = json.loads((self.requirements / 'requirements.yaml').read_text())
        payload['children'][0]['children'].append({
            'id': 'REQ-1-3', 'type': 'ATOMIC', 'description': 'Another independent behavior'})
        (self.requirements / 'requirements.yaml').write_text(json.dumps(payload))

        def observe(output, module, phase):
            evidence = output / 'module-observations' / module.node_id / phase
            evidence.mkdir(parents=True, exist_ok=True)
            report = {'errors': [], 'suites': [{'specs': [
                {'title': title, 'tests': [{'status': 'unexpected' if index == 0 and module.node_id == 'REQ-1'
                                           else 'expected', 'results': [{
                    'status': 'failed' if index == 0 and module.node_id == 'REQ-1' else 'passed',
                    'errors': [{'message': 'ReferenceError: missingHelper is not defined'}]
                              if index == 0 and module.node_id == 'REQ-1' else []}]}]}
                for index, title in enumerate(module.chain_titles)]}]}
            main.validate_discovered_chains(report, list(module.chain_titles))
            (evidence / 'result.json').write_text(json.dumps(report))
            return {**main.parse_observation_report(report, 1 if module.node_id == 'REQ-1' else 0),
                    'duration_seconds': 0, 'evidence_path': evidence.relative_to(output).as_posix()}

        with patch.object(main, 'run_module_observation', side_effect=observe), patch.object(
                EventClient, 'mark_test_passed', autospec=True) as passed_events:
            self.assertEqual(self.execute(), 0)
        acceptance = self.acceptance()
        first = acceptance['records'][0]
        self.assertEqual(first['status'], 'error')
        self.assertFalse(first['repair_attempted'])
        self.assertEqual([item['observation_status'] for item in first['requirements'].values()],
                         ['unverified', 'observed_passed', 'observed_passed'])
        self.assertEqual(acceptance['status'], 'unverified')
        self.assertEqual(acceptance['chain_counts'], {'passed': 4, 'failed': 0, 'unverified': 1})
        self.assertEqual(acceptance['unverified_ids'], ['REQ-1-1'])
        self.assertEqual(len(FakeCodex.threads), 2, 'script error caused an application repair')
        passed_ids = [call.args[1] for call in passed_events.call_args_list]
        self.assertIn('REQ-1-2', passed_ids)
        self.assertIn('REQ-1-3', passed_ids)
        self.assertNotIn('REQ-1-1', passed_ids)

    def result(self, output, module, phase, status):
        self.calls.append((module.node_id, phase))
        evidence = output / 'module-observations' / module.node_id / phase
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / 'result.json').write_text(json.dumps({'phase': phase, 'status': status}))
        return {'status': status, 'tests': [
            {'title': title, 'status': 'expected' if status == 'passed' else 'unexpected', 'executed': True, 'result_status': 'passed' if status == 'passed' else 'failed'}
            for title in module.chain_titles], 'duration_seconds': 0, 'evidence_path': evidence.relative_to(output).as_posix()}

    def test_later_generation_breakage_is_seen_in_final_acceptance(self):
        original_run = FakeThread.run
        def implement(thread, prompt):
            result = original_run(thread, prompt)
            if 'Observation repair' not in prompt:
                (thread.output / 'first-feature.txt').write_text('ok' if thread.ran[-1] == 'REQ-1' else 'broken')
            return result
        def observe(output, module, phase):
            status = 'failed' if module.node_id == 'REQ-1' and (output / 'first-feature.txt').read_text() != 'ok' else 'passed'
            return self.result(output, module, phase, status)
        with patch.object(FakeThread, 'run', implement), patch.object(main, 'run_module_observation', side_effect=observe):
            self.assertEqual(self.execute(), 0)
        acceptance = self.acceptance()
        self.assertEqual(acceptance['status'], 'failed')
        self.assertEqual(acceptance['records'][0]['before']['status'], 'failed')
        self.assertEqual(acceptance['application_sha256'], main.application_digest(self.output))

    def test_later_repair_invalidates_earlier_pass_and_preserves_failure_material(self):
        original_run = FakeThread.run
        def implement(thread, prompt):
            result = original_run(thread, prompt)
            if 'Observation repair' in prompt:
                (thread.output / 'first-feature.txt').write_text('broken')
                (thread.output / 'second-feature.txt').write_text('fixed')
            else:
                (thread.output / 'first-feature.txt').write_text('ok')
                (thread.output / 'second-feature.txt').write_text('needs repair')
            return result
        def observe(output, module, phase):
            file = output / ('first-feature.txt' if module.node_id == 'REQ-1' else 'second-feature.txt')
            wanted = 'ok' if module.node_id == 'REQ-1' else 'fixed'
            return self.result(output, module, phase, 'passed' if file.read_text() == wanted else 'failed')
        with patch.object(FakeThread, 'run', implement), patch.object(main, 'run_module_observation', side_effect=observe):
            self.assertEqual(self.execute(), 0)
        acceptance = self.acceptance()
        self.assertEqual(acceptance['records'][0]['initial_status'], 'passed')
        self.assertEqual(acceptance['records'][0]['status'], 'failed')
        self.assertEqual(self.calls[:2], [('REQ-1', 'before'), ('REQ-2', 'before')])
        self.assertEqual(self.calls[-2:], [('REQ-1', 'after'), ('REQ-2', 'after')])
        self.assertEqual(len(FakeCodex.threads), 3, 'the single repair triggered another model repair')
        self.assertEqual(json.loads((self.output / 'module-observations/REQ-2/before/result.json').read_text())['status'], 'failed')
        self.assertTrue((self.output / 'module-observations/REQ-2/after/result.json').is_file())
        self.assertTrue(all(record['application_sha256'] == main.application_digest(self.output)
                            for record in acceptance['records']))

    def test_unchanged_code_has_no_final_replay_and_metadata_does_not_change_digest(self):
        with patch.object(main, 'run_module_observation', side_effect=lambda output, module, phase:
                          self.result(output, module, phase, 'passed')):
            self.assertEqual(self.execute(), 0)
        self.assertEqual(self.calls, [('REQ-1', 'before'), ('REQ-2', 'before')])
        acceptance = self.acceptance()
        self.assertEqual(acceptance['status'], 'passed')
        digest = main.application_digest(self.output)
        (self.output / 'module-observations/extra-report.json').write_text('new metadata')
        self.assertEqual(main.application_digest(self.output), digest)
        runtime = main.AgentRuntime.from_env(project_dir=str(self.output))
        self.assertEqual(self.summary()['final_git_head'], runtime.git.current_head())

    def test_early_planning_error_does_not_withhold_later_generated_modules(self):
        plans = []
        def plan(_codex, module, _requirements, output, _root, _model):
            plans.append(module.node_id)
            self.assertFalse((output / 'REQ-1.txt').is_file())
            self.assertFalse((output / 'REQ-2.txt').is_file())
            folder = output / 'module-observations' / module.node_id
            folder.mkdir(parents=True, exist_ok=True)
            (folder / 'contract.json').write_text('{partial')
            raise ValueError('Invalid observation contract: neutral bad format')
        with patch.object(main, 'plan_module_observation', side_effect=plan):
            self.assertEqual(self.execute(), 0)
        self.assertEqual(plans, ['REQ-1', 'REQ-2'])
        self.assertTrue((self.output / 'REQ-1.txt').is_file())
        self.assertTrue((self.output / 'REQ-2.txt').is_file())
        self.assertEqual(self.summary()['status'], 'complete')
        self.assertEqual(self.acceptance()['status'], 'unverified')
        self.assertGreater(self.acceptance()['unverified_count'], 0)
        self.assertTrue((self.output / 'module-observations/REQ-1/planning-failure.json').is_file())

    def test_planning_quota_stops_acceptance_calls_without_losing_generated_code(self):
        with patch.object(main, 'plan_module_observation', side_effect=RuntimeError('Quota exceeded')) as planner:
            self.assertEqual(self.execute(), 0)
        self.assertEqual(planner.call_count, 1)
        self.assertTrue((self.output / 'REQ-1.txt').is_file() and (self.output / 'REQ-2.txt').is_file())
        self.assertEqual(self.acceptance()['status'], 'unverified')
        self.assertEqual(self.summary()['status'], 'complete')
        self.assertIsNone(self.acceptance()['technical_failure'])
        self.assertGreater(self.acceptance()['unverified_count'], 0)

    def test_later_planning_quota_preserves_prior_failure_diagnostics_without_repair_call(self):
        def plan(codex, module, requirements, output, root, model):
            if module.node_id == 'REQ-2':
                raise RuntimeError('Quota exceeded. Check your plan and billing details.')
            return fake_plan(codex, module, requirements, output, root, model)

        def observe(output, module, phase):
            result = fake_observe_pass(output, module, phase)
            result['tests'][0].update(status='unexpected', result_status='failed',
                                      error='Source-required state after a committed edit is wrong')
            result['status'] = 'failed'
            return result

        with patch.object(main, 'plan_module_observation', side_effect=plan) as planner, patch.object(
                main, 'run_module_observation', side_effect=observe):
            self.assertEqual(self.execute(), 0)
        acceptance = self.acceptance()
        self.assertEqual(planner.call_count, 2)
        self.assertEqual(len(FakeCodex.threads), 2, 'blocked planning must not trigger a repair model call')
        self.assertEqual(acceptance['chain_counts']['failed'], 1)
        self.assertEqual(acceptance['repair']['failed_atoms'], ['REQ-1-1'])
        self.assertFalse(acceptance['repair']['attempted'])
        self.assertIn('Quota exceeded', acceptance['repair']['blocked_reason']['error']['message'])
        self.assertTrue((self.output / 'REQ-1.txt').is_file())
        self.assertTrue((self.output / 'module-observations/REQ-1/before/checks.spec.js').is_file())

    def test_completed_resume_rechecks_changed_application_instead_of_old_pass(self):
        with patch.object(main, 'run_module_observation', side_effect=lambda output, module, phase:
                          self.result(output, module, phase, 'passed')):
            self.assertEqual(self.execute(), 0)
        resumed = self.root / 'resumed'
        with patch.object(main, 'restore_checkpoint', wraps=main.restore_checkpoint) as restore:
            def changed_restore(*args):
                completed = restore._mock_wraps(*args)
                (args[1] / 'changed.txt').write_text('changed restored application')
                return completed
            restore.side_effect = changed_restore
            with patch.object(main, 'run_module_observation', side_effect=lambda output, module, phase:
                              self.result(output, module, phase, 'passed')):
                self.assertEqual(self.execute(resumed, self.output), 0)
        self.assertEqual(len(self.calls), 4)

    def test_changed_frozen_inputs_do_not_reuse_cached_pass_for_same_application(self):
        with patch.object(main, 'run_module_observation', side_effect=lambda output, module, phase:
                          self.result(output, module, phase, 'passed')):
            self.assertEqual(self.execute(), 0)
        resumed = self.root / 'changed-evidence'
        original_restore = main.restore_checkpoint
        def changed_restore(*args):
            completed = original_restore(*args)
            (args[1] / 'module-observations/REQ-1/frozen-v1.spec.js').write_text('changed frozen input')
            return completed
        with patch.object(main, 'restore_checkpoint', side_effect=changed_restore), patch.object(
                main, 'run_module_observation', side_effect=lambda output, module, phase:
                    self.result(output, module, phase, 'passed')):
            self.assertEqual(self.execute(resumed, self.output), 0)
        self.assertEqual(len(self.calls), 3, 'changed frozen script must not be silently replanned or executed')
        acceptance = json.loads((resumed / '.arc/codex-recovery/summary.json').read_text())['acceptance']
        self.assertEqual(acceptance['status'], 'unverified')
        self.assertEqual(acceptance['records'][0]['requirements']['REQ-1-1']['observation_status'], 'unverified')

    def test_round_a_planning_401_stops_models_without_discarding_runnable_code(self):
        with patch.object(main, 'plan_module_observation', side_effect=RuntimeError('HTTP 401 Unauthorized')) as planner:
            self.assertEqual(self.execute(), 0)
        self.assertEqual(planner.call_count, 1)
        self.assertEqual(self.acceptance()['status'], 'unverified')
        self.assertTrue((self.output / 'REQ-1.txt').is_file() and (self.output / 'REQ-2.txt').is_file())

    def test_round_c_final_build_mutation_cannot_keep_an_old_pass(self):
        builds = []
        def build(output):
            builds.append(output)
            if len(builds) == 3:
                (output / 'late-build-change.txt').write_text('new source after acceptance')
            return {'status':'passed'}
        with patch.object(main, 'build_frontend', side_effect=build), patch.object(
                main, 'run_module_observation', side_effect=lambda output,module,phase:
                    self.result(output,module,phase,'passed')):
            self.assertEqual(self.execute(), 0)
        # The stale pass is invalidated even though the run still delivers the current candidate.
        self.assertEqual(self.acceptance()['status'], 'unverified')
        self.assertEqual(self.summary()['delivery']['exit_code'], 0)
        self.assertTrue(self.summary()['final_git_head'])


class ModuleCommitTests(unittest.TestCase):
    setUp = RecoveryTests.setUp
    execute = RecoveryTests.execute
    latest = RecoveryTests.latest
    summary = RecoveryTests.summary
    runtime_events = RecoveryTests.runtime_events

    def commit(self, runtime, identifier='REQ-1'):
        commit = getattr(main, 'commit_module', None)
        self.assertTrue(callable(commit), 'module commit wrapper is missing')
        return commit(runtime, identifier, self.output, 'runnable; acceptance pending')

    def test_real_module_commits_preserve_two_versions(self):
        original_run = FakeThread.run
        def implement(thread, prompt):
            result = original_run(thread, prompt)
            (thread.output / 'shared.txt').write_text(thread.ran[-1])
            return result
        with patch.object(FakeThread, 'run', implement), patch.object(
                main, 'build_frontend', return_value={'status': 'passed'}):
            self.assertEqual(self.execute(), 0)
        modules = self.summary()['module_results']
        self.assertTrue(all(item.get('git_head') for item in modules), 'missing real module HEADs')
        first, second = [item['git_head'] for item in modules]
        self.assertNotEqual(first, second)
        runtime = main.AgentRuntime.from_env(project_dir=str(self.output))
        self.assertEqual(runtime.git.run(['show', first + ':shared.txt']).stdout, 'REQ-1')
        self.assertEqual(runtime.git.run(['show', second + ':shared.txt']).stdout, 'REQ-2')
        self.assertEqual(runtime.git.current_head(), self.summary()['final_git_head'])
        self.assertEqual(runtime.git.run(['rev-list', '--count', 'HEAD']).stdout.strip(), '3')
        subjects = runtime.git.run(['log', '--format=%s']).stdout
        self.assertIn('REQ-1 (implement): runnable; acceptance pending', subjects)
        self.assertIn('REQ-2 (implement): runnable; acceptance pending', subjects)
        print('MODULE_HEADS', first, second)

    def test_no_difference_reuses_real_head_without_empty_commit(self):
        self.output.mkdir()
        (self.output / 'neutral.txt').write_text('unchanged source')
        runtime = main.AgentRuntime.from_env(project_dir=str(self.output))
        first = self.commit(runtime)
        second = self.commit(runtime, 'REQ-2')
        self.assertEqual(first, second)
        self.assertEqual(runtime.git.run(['rev-list', '--count', 'HEAD']).stdout.strip(), '1')

    def test_commit_failure_keeps_snapshot_and_stops_without_model_git_repair(self):
        from arcbench_agent_runtime.gitops import GitClient
        with patch.object(main, 'build_frontend', return_value={'status': 'passed'}), patch.object(
                GitClient, 'commit', side_effect=RuntimeError('neutral Git permission failure')):
            self.assertEqual(self.execute(), 0)
        self.assertEqual(self.latest()['completed_ids'], ['REQ-1'])
        self.assertEqual(self.summary()['completed_ids'], [])
        self.assertEqual(self.summary()['status'], 'blocked')
        self.assertEqual(self.summary()['module_results'][0]['status'], 'commit_failed')
        self.assertEqual(self.summary()['pending_issues'][0]['stage'], 'commit')
        self.assertEqual(self.summary()['unattempted_ids'], ['REQ-2'])
        self.assertEqual([thread.ran for thread in FakeCodex.threads], [['REQ-1']])
        snapshot = self.output / '.arc/codex-recovery/checkpoints' / self.latest()['checkpoint'] / 'source'
        self.assertEqual((snapshot / 'REQ-1.txt').read_text(), 'REQ-1')

    def test_snapshot_failure_cannot_publish_module_completion_or_commit(self):
        from arcbench_agent_runtime.gitops import GitClient
        with patch.object(main, 'build_frontend', return_value={'status': 'passed'}), patch.object(
                main, 'save_checkpoint', side_effect=RuntimeError('neutral snapshot failure')), patch.object(
                GitClient, 'commit', side_effect=AssertionError('must not commit before snapshot')):
            self.assertEqual(self.execute(), 1)
        self.assertEqual(self.summary()['completed_ids'], [])
        self.assertFalse(any(event['phase'] == 'implement' and event['status'] == 'completed'
                             for event in self.runtime_events()))
        self.assertFalse((self.output / '.git').exists())

    def test_new_output_resume_commits_real_baseline_before_next_implementation(self):
        FakeCodex.failure = 'REQ-2'
        with patch.object(main, 'build_frontend', return_value={'status': 'passed'}):
            self.assertEqual(self.execute(), 0)
        old_runtime = main.AgentRuntime.from_env(project_dir=str(self.output))
        old_head = old_runtime.git.current_head()
        self.assertIsNotNone(old_head, 'original module has no Git history')
        resumed = self.root / 'resumed'
        baselines = []
        original_run = FakeThread.run
        def implement(thread, prompt):
            runtime = main.AgentRuntime.from_env(project_dir=str(thread.output))
            head = runtime.git.current_head()
            self.assertIsNotNone(head, 'resume began implementation before a baseline commit')
            self.assertIn('recovery-baseline', runtime.git.run(['log', '-1', '--format=%s']).stdout)
            baselines.append(head)
            return original_run(thread, prompt)
        FakeCodex.failure = None
        with patch.object(FakeThread, 'run', implement), patch.object(
                main, 'build_frontend', return_value={'status': 'passed'}):
            self.assertEqual(self.execute(resumed, self.output), 0)
        runtime = main.AgentRuntime.from_env(project_dir=str(resumed))
        self.assertEqual(len(baselines), 1)
        self.assertNotEqual(runtime.git.current_head(), baselines[0])
        self.assertEqual(runtime.git.run(['rev-list', '--count', 'HEAD']).stdout.strip(), '3')
        self.assertEqual(old_runtime.git.current_head(), old_head)
        self.assertEqual(self.latest(resumed)['completed_ids'], ['REQ-1', 'REQ-2'])

    def test_cross_gap_snapshot_preserves_prefix_and_commits_later_source(self):
        (self.requirements / 'requirements.yaml').write_text(
            'id: ROOT\nchildren:\n  - id: REQ-1\n  - id: REQ-2\n  - id: REQ-3\n')
        def build(_output):
            if FakeCodex.thread.ran[-1] == 'REQ-2':
                raise RuntimeError('neutral shared build failure')
            return {'status': 'passed'}
        with patch.object(main, 'build_frontend', side_effect=build):
            self.assertEqual(self.execute(), 0)
        self.assertEqual(self.latest()['completed_ids'], ['REQ-1'])
        snapshot = self.output / '.arc/codex-recovery/checkpoints' / self.latest()['checkpoint'] / 'source'
        self.assertTrue((snapshot / 'REQ-3.txt').is_file(), 'later runnable source was not saved')
        later = self.summary()['module_results'][-1]
        self.assertEqual(later['status'], 'runtime_passed_uncheckpointed')
        self.assertTrue(later['git_head'])
        restored = self.root / 'restored-prefix'
        restored.mkdir()
        identity = main.input_identity(self.requirements, Path(main.__file__).parent)
        self.assertEqual(main.restore_checkpoint(self.output, restored, identity, Path(main.__file__).parent),
                         ['REQ-1'])
        self.assertEqual((restored / 'REQ-3.txt').read_text(), 'REQ-3')

    def test_resume_baseline_commit_failure_stops_before_any_new_model(self):
        from arcbench_agent_runtime.gitops import GitClient
        FakeCodex.failure = 'REQ-2'
        with patch.object(main, 'build_frontend', return_value={'status': 'passed'}):
            self.assertEqual(self.execute(), 0)
        FakeCodex.failure = None
        FakeCodex.threads = []
        resumed = self.root / 'failed-resume'
        with patch.object(main, 'build_frontend', return_value={'status': 'passed'}), patch.object(
                GitClient, 'commit', side_effect=RuntimeError('restored baseline Git denied')):
            self.assertEqual(self.execute(resumed, self.output), 0)
        self.assertEqual(FakeCodex.threads, [])
        self.assertEqual(self.summary(resumed)['module_results'][0]['status'], 'commit_failed')
        self.assertEqual(self.summary(resumed)['pending_issues'][0]['stage'], 'commit')
        self.assertEqual(self.latest()['completed_ids'], ['REQ-1'])

    def test_commit_without_real_head_is_rejected(self):
        from arcbench_agent_runtime.gitops import GitClient
        self.output.mkdir()
        runtime = main.AgentRuntime.from_env(project_dir=str(self.output))
        with patch.object(GitClient, 'commit', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'no HEAD'):
                self.commit(runtime)


class SnapshotRuntimeExclusionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.excluded = ['backend/runtime' + extension + suffix
            for extension in ('.db', '.sqlite', '.sqlite3')
            for suffix in ('', '-wal', '-shm', '-journal')]
        self.excluded += ['module-observations/REQ-1/before/failure.png',
                          'module-observations/REQ-1/after/video.webm',
                          'module-observations/REQ-1/after/trace.zip']
        self.kept = ['backend/src/app.js', 'reference/homepage.png',
                     'module-observations/REQ-1/contract.json',
                     'module-observations/REQ-1/checks.spec.js',
                     'module-observations/REQ-1/frozen-v1.spec.js',
                     'module-observations/REQ-1/implementation.json',
                     'module-observations/REQ-1/requirement-status.json']
        for name in self.excluded + self.kept:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(('neutral:' + name).encode())

    def test_snapshot_excludes_runtime_files_and_keeps_lightweight_acceptance_inputs(self):
        target = self.root / 'snapshot'
        main.copy_source_snapshot(self.source, target)
        for name in self.excluded:
            self.assertFalse((target / name).exists(), name)
        for name in self.kept:
            self.assertEqual((target / name).read_bytes(), (self.source / name).read_bytes())

    def test_initial_template_copy_uses_same_runtime_filter(self):
        target = self.root / 'output'
        main.copy_template_contents(self.source, target)
        for name in self.excluded:
            self.assertFalse((target / name).exists(), name)
        for name in self.kept:
            self.assertEqual((target / name).read_bytes(), (self.source / name).read_bytes())

    def test_real_git_ignores_runtime_files_but_tracks_reference_and_contract(self):
        runtime = main.AgentRuntime.from_env(project_dir=str(self.source))
        runtime.git.ensure_repo(create_initial_commit=False)
        self.assertTrue(runtime.git.commit('neutral exclusion contract'))
        tracked = set(runtime.git.run(['ls-files']).stdout.splitlines())
        self.assertFalse(set(self.excluded) & tracked, sorted(set(self.excluded) & tracked))
        self.assertTrue(set(self.kept) <= tracked)


class GenerationBeforeAcceptanceTests(unittest.TestCase):
    setUp = RecoveryTests.setUp
    latest = RecoveryTests.latest
    summary = RecoveryTests.summary
    runtime_events = RecoveryTests.runtime_events

    def execute(self):
        agent = Path(main.__file__).parent
        main.copy_template_contents(agent / 'template', self.output)
        skills = main.copy_skills_to_output(agent / 'skills', self.output)
        summary = main.implement_modules(self.requirements, self.output,
            main.load_root_modules(self.requirements), skills, main.input_identity(self.requirements, agent), [])
        main.atomic_json(self.output / '.arc/codex-recovery/summary.json', summary)
        return 0 if summary['status'] == 'complete' else 1

    def test_current_module_planning_precedes_only_current_generation(self):
        trace = []
        original_run = FakeThread.run
        original_save = main.save_checkpoint
        def implement(thread, prompt):
            identifier = re.search(r'Implement ROOT module \d+/\d+: (\S+)', prompt).group(1)
            trace.append('implement:' + identifier)
            self.assertNotIn('Read the independent, source-based observation contract', prompt)
            return original_run(thread, prompt)
        def build(_output):
            trace.append('build:' + FakeCodex.thread.ran[-1])
            return {'status': 'passed'}
        def startup(_output):
            trace.append('startup:' + FakeCodex.thread.ran[-1])
            return {'status': 'passed'}
        def checkpoint(output, completed, identity, result):
            trace.append('checkpoint:' + (completed[-1] if completed else 'none'))
            return original_save(output, completed, identity, result)
        def plan(*args):
            trace.append('plan:' + args[1].node_id)
            return fake_plan(*args)
        with patch.object(FakeThread, 'run', implement), patch.object(main, 'build_frontend', side_effect=build), \
                patch.object(main, 'verify_backend_startup', side_effect=startup), \
                patch.object(main, 'save_checkpoint', side_effect=checkpoint), \
                patch.object(main, 'plan_module_observation', side_effect=plan), \
                patch.object(main, 'run_module_observation', side_effect=AssertionError('observation in generation')):
            self.assertEqual(self.execute(), 0)
        self.assertEqual(trace, ['plan:REQ-1', 'implement:REQ-1', 'build:REQ-1', 'startup:REQ-1', 'checkpoint:REQ-1',
                                 'plan:REQ-2', 'implement:REQ-2', 'build:REQ-2', 'startup:REQ-2', 'checkpoint:REQ-2'])
        print('GENERATION_TRACE', json.dumps(trace))
        for identifier in ('REQ-1', 'REQ-2'):
            self.assertEqual((self.output / (identifier + '.txt')).read_text(), identifier)
        self.assertEqual(self.latest()['completed_ids'], ['REQ-1', 'REQ-2'])
        self.assertFalse(any(event['phase'] == 'test' for event in self.runtime_events()))

    def test_controlled_coding_turn_keeps_command_feedback_and_host_handoff(self):
        trace = []
        feedback = []
        original_run = FakeThread.run

        def replay(thread, prompt):
            if 'REQ-1' not in prompt.splitlines()[0]:
                return original_run(thread, prompt)
            self.assertIn('run bounded', main._developer_instructions(Path('/skills')))
            self.assertIn('nonzero exit', prompt)
            self.assertIn('documented formal startup entry', prompt)
            self.assertIn('observable state and data', prompt)
            self.assertIn('confirmed residual failure', prompt)
            source = thread.output / 'working-source.txt'
            source.write_text('draft', encoding='utf-8')
            failed = subprocess.run([sys.executable, '-c', 'import sys; sys.exit(3)'],
                                    capture_output=True, text=True, timeout=1)
            feedback.append({'kind': 'assertion', 'exit_code': failed.returncode,
                             'stdout': failed.stdout, 'stderr': failed.stderr})
            self.assertEqual(source.read_text(encoding='utf-8'), 'draft')
            source.write_text('fixed', encoding='utf-8')
            checked = subprocess.run([sys.executable, '-c',
                                      'from pathlib import Path; import sys; '
                                      'sys.exit(0 if Path(sys.argv[1]).read_text() == "fixed" else 3)',
                                      str(source)], capture_output=True, text=True, timeout=1)
            feedback.append({'kind': 'recheck', 'exit_code': checked.returncode,
                             'stdout': checked.stdout, 'stderr': checked.stderr})
            try:
                subprocess.run([sys.executable, '-c', 'import time; time.sleep(2)'],
                               capture_output=True, text=True, timeout=0.05)
            except subprocess.TimeoutExpired as exc:
                feedback.append({'kind': 'timeout', 'timeout_seconds': exc.timeout})
            try:
                subprocess.run(['definitely-missing-check-tool-oaic'], timeout=1)
            except FileNotFoundError:
                feedback.append({'kind': 'missing_tool'})
            folder = thread.output / 'module-observations' / 'REQ-1'
            folder.mkdir(parents=True, exist_ok=True)
            main.atomic_json(folder / 'implementation.json', {'requirements': [
                {'id': 'REQ-1-1', 'files': ['working-source.txt'], 'unfinished': ''},
                {'id': 'REQ-1-2', 'files': ['working-source.txt'],
                 'unfinished': 'Browser check tool unavailable; unverified'}]})
            trace.append('turn-completed')
            return types.SimpleNamespace(error=None, status='completed')

        def build(_output):
            trace.append('build')
            return {'status': 'passed'}

        def startup(_output):
            trace.append('startup')
            return {'status': 'passed'}

        with patch.object(FakeThread, 'run', replay), \
                patch.object(main, 'build_frontend', side_effect=build), \
                patch.object(main, 'verify_backend_startup', side_effect=startup), \
                patch.object(main, 'commit_module', return_value='controlled-head'), \
                patch.object(main, 'save_checkpoint', side_effect=lambda *_: trace.append('checkpoint')):
            result = main.implement_modules(self.requirements, self.output,
                main.load_root_modules(self.requirements), Path('/skills'), {}, [])

        self.assertEqual(result['status'], 'complete')
        self.assertEqual([item['kind'] for item in feedback],
                         ['assertion', 'recheck', 'timeout', 'missing_tool'])
        self.assertEqual([item['exit_code'] for item in feedback[:2]], [3, 0])
        self.assertEqual((self.output / 'working-source.txt').read_text(encoding='utf-8'), 'fixed')
        self.assertEqual(trace[:4], ['turn-completed', 'build', 'startup', 'checkpoint'])
        self.assertIn('unverified', (self.output / 'module-observations/REQ-1/implementation.json')
                      .read_text(encoding='utf-8'))
        self.assertEqual(len(FakeCodex.threads), 2)
        print('CONTROLLED_CODING_FEEDBACK', json.dumps(feedback))

    def test_coding_quota_remains_blocked_after_early_planning(self):
        FakeCodex.failure = 'REQ-1'
        FakeCodex.failure_message = 'Quota exceeded. Check billing details.'
        with patch.object(main, 'build_frontend', return_value={'status': 'passed'}), \
                patch.object(main, 'plan_module_observation', side_effect=fake_plan) as planner:
            self.assertEqual(self.execute(), 1)
        self.assertEqual(planner.call_count, 2)
        self.assertEqual([thread.ran for thread in FakeCodex.threads], [['REQ-1']])
        self.assertEqual(self.summary()['status'], 'blocked')
        self.assertEqual(self.summary()['pending_issues'][0]['stage'], 'model')

    def test_generation_keeps_the_existing_single_technical_retry(self):
        builds = []
        def build(_output):
            builds.append(FakeCodex.thread.ran[-1])
            if len(builds) == 1:
                raise RuntimeError('neutral build failure')
            return {'status': 'passed'}
        with patch.object(main, 'build_frontend', side_effect=build), \
                patch.object(main, 'plan_module_observation', side_effect=fake_plan):
            self.assertEqual(self.execute(), 0)
        self.assertEqual([thread.ran for thread in FakeCodex.threads], [['REQ-1'], ['REQ-1'], ['REQ-2']])
        self.assertEqual(self.latest()['completed_ids'], ['REQ-1', 'REQ-2'])


class DependencySyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        self.project = self.output / 'frontend'
        self.project.mkdir()
        self.package = self.project / 'package.json'
        self.lock = self.project / 'package-lock.json'
        self.package.write_text('{"name":"neutral","dependencies":{}}', encoding='utf-8')
        self.lock.write_text('{"lockfileVersion":3}', encoding='utf-8')
        self.commands = []

    def execute(self, command, **kwargs):
        self.commands.append((command, kwargs))
        if command[1] in {'ci', 'install'}:
            (Path(kwargs['cwd']) / 'node_modules').mkdir(exist_ok=True)
        return subprocess.CompletedProcess(command, 0, '', '')

    def ensure(self, project=None):
        installer = getattr(main, 'ensure_project_dependencies', None)
        self.assertTrue(callable(installer), 'shared dependency installer is missing')
        return installer(project or self.project, self.output)

    def test_existing_directory_and_changed_manifest_install(self):
        (self.project / 'node_modules').mkdir()
        with patch.object(main.subprocess, 'run', side_effect=self.execute):
            self.ensure()
            self.package.write_text('{"name":"neutral","dependencies":{"new-package":"1.0.0"}}')
            self.ensure()
        self.assertEqual(len(self.commands), 2)
        self.assertTrue(all('--include=dev' in command for command, _ in self.commands))

    def test_unchanged_content_reuses_success_even_if_timestamp_changes(self):
        with patch.object(main.subprocess, 'run', side_effect=self.execute):
            self.ensure()
            self.package.touch()
            self.lock.touch()
            result = self.ensure()
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(result['dependency_install'], 'existing')

    def test_failed_install_does_not_record_success(self):
        with patch.object(main.subprocess, 'run', return_value=subprocess.CompletedProcess(
                ['npm', 'ci'], 1, '', 'permission denied')):
            with self.assertRaisesRegex(RuntimeError, 'permission denied'):
                self.ensure()
        self.assertFalse((self.output / '.arc/codex-recovery/installed-dependencies.json').exists())

    def test_removed_dependency_directory_is_reinstalled(self):
        with patch.object(main.subprocess, 'run', side_effect=self.execute):
            self.ensure()
            shutil.rmtree(self.project / 'node_modules')
            self.ensure()
        self.assertEqual(len(self.commands), 2)

    def test_changed_lockfile_is_reinstalled(self):
        with patch.object(main.subprocess, 'run', side_effect=self.execute):
            self.ensure()
            self.lock.write_text('{"lockfileVersion":3,"packages":{}}')
            self.ensure()
        self.assertEqual(len(self.commands), 2)

    def test_lock_mismatch_fallback_records_updated_lock(self):
        def execute(command, **kwargs):
            if command[1] == 'ci':
                self.commands.append((command, kwargs))
                return subprocess.CompletedProcess(command, 1, '',
                    'npm ci can only install packages when your package.json and package-lock.json are in sync')
            self.lock.write_text('{"lockfileVersion":3,"updated":true}')
            return self.execute(command, **kwargs)
        with patch.object(main.subprocess, 'run', side_effect=execute):
            result = self.ensure()
            self.ensure()
        self.assertEqual(result['dependency_install'], 'npm install')
        self.assertEqual([command[1] for command, _ in self.commands], ['ci', 'install'])

    def test_missing_lock_uses_only_install(self):
        self.lock.unlink()
        with patch.object(main.subprocess, 'run', side_effect=self.execute):
            self.ensure()
        self.assertEqual([command[1] for command, _ in self.commands], ['install'])

    def test_network_failure_has_no_install_fallback(self):
        def execute(command, **kwargs):
            self.commands.append((command, kwargs))
            return subprocess.CompletedProcess(command, 1, '', 'socket ECONNRESET')
        with patch.object(main.subprocess, 'run', side_effect=execute):
            with self.assertRaisesRegex(RuntimeError, 'ECONNRESET'):
                self.ensure()
        self.assertEqual([command[1] for command, _ in self.commands], ['ci'])

    def test_install_timeout_keeps_command_evidence_without_success(self):
        def execute(command, **kwargs):
            self.commands.append((command, kwargs))
            raise subprocess.TimeoutExpired(command, kwargs['timeout'], stderr='offline timeout')
        with patch.object(main.subprocess, 'run', side_effect=execute):
            with self.assertRaisesRegex(RuntimeError, '600') as failure:
                self.ensure()
        self.assertIn('ci --include=dev', str(failure.exception))
        self.assertIn('offline timeout', str(failure.exception))
        self.assertEqual(self.commands[0][1]['timeout'], 600)
        self.assertFalse((self.output / '.arc/codex-recovery/installed-dependencies.json').exists())
        events = (self.output / '.arc/codex-recovery/events.jsonl').read_text()
        self.assertIn('npm', events)
        self.assertIn('timeout', events)

    def test_frontend_has_install_and_build_time_limits(self):
        (self.project / 'dist').mkdir()
        (self.project / 'dist/index.html').write_text('neutral')
        with patch.object(main.subprocess, 'run', side_effect=self.execute):
            self.assertEqual(main.build_frontend(self.output)['status'], 'passed')
        self.assertEqual([kwargs.get('timeout') for _, kwargs in self.commands], [600, 180])

    def test_build_timeout_keeps_command_evidence(self):
        def execute(command, **kwargs):
            if command[1:3] == ['run', 'build']:
                raise subprocess.TimeoutExpired(command, kwargs.get('timeout', 0), stderr='build stalled')
            return self.execute(command, **kwargs)
        with patch.object(main.subprocess, 'run', side_effect=execute):
            with self.assertRaisesRegex(RuntimeError, '180') as failure:
                main.build_frontend(self.output)
        self.assertIn('npm run build', str(failure.exception))
        self.assertIn('build stalled', str(failure.exception))
        self.assertIn('npm', (self.output / '.arc/codex-recovery/events.jsonl').read_text())

    def test_backend_startup_uses_shared_dependency_sync(self):
        backend = self.output / 'backend'
        shutil.copytree(self.project, backend)
        with patch.object(main.subprocess, 'run', side_effect=self.execute), patch.object(
                main.subprocess, 'Popen', side_effect=RuntimeError('startup boundary reached')):
            with self.assertRaisesRegex(RuntimeError, 'startup boundary reached'):
                main.verify_backend_startup(self.output)
        self.assertEqual(len(self.commands), 1)
        self.assertIn('--include=dev', self.commands[0][0])
        self.assertEqual(self.commands[0][1]['timeout'], 600)

    def test_observation_reuses_synced_backend_without_second_install(self):
        backend = self.output / 'backend'
        shutil.copytree(self.project, backend)
        observation = self.output / 'module-observations/REQ-1'
        observation.mkdir(parents=True)
        (observation / 'checks.spec.js').write_bytes(b'// neutral frozen script')
        module = main.RequirementModule(1, 1, 'REQ-1', 'neutral', {},
                                        frozen_script=b'// neutral frozen script')
        def execute(command, **kwargs):
            if command[1] == 'test':
                self.commands.append((command, kwargs))
                return subprocess.CompletedProcess(command, 1, '', 'neutral discovery boundary')
            return self.execute(command, **kwargs)
        with patch.object(main.subprocess, 'run', side_effect=execute):
            self.ensure(backend)
            self.assertEqual(main.run_module_observation(self.output, module, 'before')['status'], 'error')
        self.assertEqual([command[1] for command, _ in self.commands], ['ci', 'test'])


class HealthcheckRegressions(unittest.TestCase):
    def test_changed_frontend_dependencies_are_installed(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            frontend = output / "frontend"
            (frontend / "dist").mkdir(parents=True)
            (frontend / "dist/index.html").write_text("neutral build", encoding="utf-8")
            package = frontend / "package.json"
            package.write_text(json.dumps({"dependencies": {}}), encoding="utf-8")
            commands = []

            def execute(command, **_kwargs):
                commands.append(command)
                if command[1] in {"ci", "install"}:
                    (frontend / "node_modules").mkdir(exist_ok=True)
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch.object(main.subprocess, "run", side_effect=execute):
                main.build_frontend(output)
                commands.clear()
                package.write_text(json.dumps({"dependencies": {"neutral-new-package": "1.0.0"}}),
                                   encoding="utf-8")
                main.build_frontend(output)
            self.assertTrue(any(command[1] in {"ci", "install"} for command in commands),
                            f"Changed dependencies reused the old installation: {commands}")


if __name__ == "__main__":
    unittest.main()
