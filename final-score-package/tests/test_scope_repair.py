"""Host flow regressions; no real SDK, npm, browser or service execution."""
import json
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
import yaml

sys.path.insert(0, str(Path(__file__).parent))
import test_recovery as support
main = support.main


def atoms(identifier, label):
    return {"id": identifier, "type": "ATOMIC", "description": label,
            "dependencies": [], "scenarios": [
                {"name": f"{identifier} scenario {i}", "steps": [
                    {"keyword": "THEN", "content": label}]} for i in range(7)]}


def payload():
    return {"id": "ROOT", "description": "GLOBAL_CONSTRAINT", "children": [
        {"id": "REQ-1", "description": "FIRST", "children": [atoms("REQ-1-1", "read overview")]},
        {"id": "REQ-5", "description": "PARENT_CONSTRAINT", "children": [
            {"id": "REQ-5-1", "description": "LEFT_PARENT", "children": [atoms("REQ-5-1-1", "LEFT_ONLY " * 40)]},
            {"id": "REQ-5-2", "description": "RIGHT_PARENT", "children": [atoms("REQ-5-2-1", "RIGHT_ONLY " * 40)]}]},
        {"id": "REQ-6", "description": "LATER_ONLY", "children": [atoms("REQ-6-1", "future")]},
    ]}


class ScopeRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.requirements = self.root / "requirements"
        self.requirements.mkdir()
        (self.requirements / "requirements.yaml").write_text(yaml.safe_dump(payload()), encoding="utf-8")
        self.output = self.root / "output"
        self.output.mkdir()
        main.copy_template_contents(Path(main.__file__).parent / "template", self.output)
        self.modules = main.load_root_modules(self.requirements)
        self.identity = main.input_identity(self.requirements, Path(main.__file__).parent)
        self.ran = []
        self.failed_id = None

    def generation(self, output=None, completed=None, resume_state=None, observe=None):
        owner = self
        class Client:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def login_api_key(self, key): pass
            def thread_start(self, **kwargs):
                def run(prompt):
                    identifier = re.search(r"Implement ROOT module \d+/\d+: (\S+)", prompt).group(1)
                    owner.ran.append(identifier)
                    if identifier == owner.failed_id:
                        raise RuntimeError("unknown SDK fault")
                    (Path(kwargs["cwd"]) / f"{identifier}.txt").write_text("candidate")
                    return types.SimpleNamespace(status="completed", error=None)
                return types.SimpleNamespace(run=run)
        target = output or self.output
        with patch.object(main, "_sdk_session", return_value=Client()), \
             patch.object(main, "plan_module_observation", side_effect=support.fake_plan), \
             patch.object(main, "run_module_observation", side_effect=observe or support.fake_observe_pass), \
             patch.object(main, "build_frontend", return_value={"status": "passed"}), \
             patch.object(main, "verify_backend_startup", return_value={"status": "passed"}), \
             patch.object(main, "commit_module", return_value="offline-head"):
            return main.implement_modules(self.requirements, target, main.load_root_modules(self.requirements),
                                          Path("/skills"), self.identity, completed or [],
                                          resume_batch_state=resume_state)

    def test_unknown_fault_blocks_current_batch_and_continues_next_batch_and_root(self):
        self.failed_id = "REQ-5.part-1"
        result = self.generation()
        self.assertEqual(result["completed_ids"], ["REQ-1"])
        self.assertIn("REQ-5.part-2", self.ran)
        self.assertIn("REQ-6", self.ran)
        self.assertFalse(list((self.output / ".arc/codex-recovery/batch-plans").glob("REQ-5-*")))
        self.assertNotEqual(result["status"], "complete")

    def test_failed_unsplit_observation_does_not_complete_root_prefix(self):
        def fail_observe(output, module, phase):
            result = support.fake_observe_pass(output, module, phase)
            if module.node_id == "REQ-1":
                result["status"] = "error"
            return result

        result = self.generation(observe=fail_observe)
        self.assertEqual(result["completed_ids"], [])
        record = main._checkpoint_record(self.output, self.identity,
                                         ["REQ-1", "REQ-5", "REQ-6"])
        self.assertIsNotNone(record)
        self.assertEqual(record["latest"]["completed_ids"], [])
        self.assertEqual(record["latest"]["current_batch"]["root_module_id"], "REQ-1")

    def test_resume_skips_root_prefix_and_completed_subbatch(self):
        self.failed_id = "REQ-5.part-2"
        result = self.generation()
        record = main._checkpoint_record(self.output, self.identity, ["REQ-1", "REQ-5", "REQ-6"])
        self.assertIsNotNone(record)
        self.assertEqual(record["latest"]["completed_ids"], ["REQ-1"])
        self.assertIn("REQ-5.part-1", record["manifest"]["completed_batches"])
        restored = self.root / "restored"
        restored.mkdir()
        completed = main.restore_checkpoint(self.output, restored, self.identity, Path(main.__file__).parent,
                                             ["REQ-1", "REQ-5", "REQ-6"])
        self.failed_id = None
        self.ran.clear()
        with patch.object(main, "plan_current_module_batches", wraps=main.plan_current_module_batches) as planner:
            resumed = self.generation(restored, completed, record["latest"])
        self.assertEqual(self.ran, ["REQ-5.part-2", "REQ-6"])
        self.assertEqual(resumed["completed_ids"], ["REQ-1", "REQ-5", "REQ-6"])
        self.assertNotIn("REQ-5", [call.args[0]["id"] for call in planner.call_args_list])

    def test_final_acceptance_replays_every_atom_once_in_batch_scope(self):
        result = self.generation()
        seen = []
        def observe(output, module, phase):
            seen.extend(main._atomic_requirement_index(module.subtree))
            return support.fake_observe_pass(output, module, phase)
        with patch.object(main, "run_module_observation", side_effect=observe), \
             patch.object(main, "_sdk_session", side_effect=AssertionError("offline SDK")):
            acceptance = main.run_final_acceptance(self.requirements, self.output, self.modules,
                                                    Path("/skills"), self.identity,
                                                    completed_ids=result["completed_ids"])
        self.assertNotIn("integrity_stop", acceptance)
        self.assertEqual(acceptance["status"], "passed")
        identifiers = [item["id"] for item in seen]
        self.assertEqual(len(identifiers), len(set(identifiers)))
        self.assertEqual(set(identifiers), {"REQ-1-1", "REQ-5-1-1", "REQ-5-2-1", "REQ-6-1"})
        self.assertEqual(result["unattempted_ids"], [])
        self.assertNotIn("REQ-5.part-1", result.get("attempted_ids", []))

    def test_planning_workspace_contains_only_current_closure_and_references(self):
        module = self.modules[1]
        plan = main.plan_current_module_batches(module.subtree)
        batch = main.materialize_current_batches(module, plan)[0]
        workspace = self.root / "planning"
        workspace.mkdir()
        main._planning_workspace(self.requirements, workspace, batch)
        text = (workspace / "requirements/requirements.yaml").read_text(encoding="utf-8")
        self.assertIn("LEFT_ONLY", text)
        self.assertIn("PARENT_CONSTRAINT", text)
        self.assertIn("GLOBAL_CONSTRAINT", text)
        self.assertNotIn("RIGHT_ONLY", text)
        self.assertNotIn("LATER_ONLY", text)

    def test_observation_prompt_and_workspace_are_batch_scoped(self):
        module = self.modules[1]
        plan = main.plan_current_module_batches(module.subtree)
        batch = main.materialize_current_batches(module, plan)[0]
        captured = {}

        class PlanningClient:
            def thread_start(self, **kwargs):
                captured["workspace"] = Path(kwargs["cwd"])
                return object()

        def complete(_thread, prompt, *_args, **_kwargs):
            captured["prompt"] = prompt
            captured["requirements"] = (captured["workspace"] /
                                         "requirements/requirements.yaml").read_text(encoding="utf-8")
            (captured["workspace"] / "checks.spec.js").write_text("// scoped", encoding="utf-8")

        with patch.object(main, "_completed_turn", side_effect=complete):
            main.plan_module_observation(PlanningClient(), batch, self.requirements,
                                         self.output, {"id": "ROOT"}, None)
        self.assertNotIn("Read the full original requirements", captured["prompt"])
        scoped = captured["requirements"]
        self.assertIn("LEFT_ONLY", scoped)
        self.assertNotIn("RIGHT_ONLY", scoped)
        self.assertNotIn("LATER_ONLY", scoped)

    def test_live_plan_tamper_is_rejected_before_restore(self):
        self.failed_id = "REQ-5.part-2"
        self.generation()
        plan_path = self.output / ".arc/engineering/current-batch-plan.json"
        plan = json.loads(plan_path.read_text())
        plan["batches"][0]["atomic_ids"] = ["REQ-6-1"]
        main.atomic_json(plan_path, plan)
        restored = self.root / "tampered"
        restored.mkdir()
        with self.assertRaises(ValueError):
            main.restore_checkpoint(self.output, restored, self.identity, Path(main.__file__).parent,
                                    ["REQ-1", "REQ-5", "REQ-6"])

    def test_archive_selection_uses_checkpoint_plan_not_hash_sort(self):
        self.generation()
        archive = self.output / ".arc/codex-recovery/batch-plans"
        main.atomic_json(archive / ("REQ-5-" + "f" * 64 + ".json"),
                         {"root_module_id": "REQ-5", "decision": "split", "batches": []})
        selected = main.load_completed_batch_modules(self.output, self.modules)
        self.assertEqual([item.node_id for item in selected],
                         ["REQ-1", "REQ-5.part-1", "REQ-5.part-2", "REQ-6"])


