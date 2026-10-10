"""Current-module-only deterministic batch planning tests."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_recovery as support

main = support.main


def atom(identifier, dependencies=None, scenarios=1, text="create account and save state"):
    return {
        "id": identifier,
        "type": "ATOMIC",
        "dependencies": dependencies or [],
        "description": text,
        "scenarios": [{"name": f"{identifier}: scenario {i}"} for i in range(scenarios)],
    }


def module(children, identifier="REQ-4", description="module constraints"):
    return {
        "id": identifier,
        "type": "FOLDER",
        "name": identifier,
        "description": description,
        "dependencies": [],
        "children": children,
    }


class CurrentModuleBatchTests(unittest.TestCase):
    def test_low_risk_module_stays_one_batch(self):
        current = module([atom("REQ-3-1", text="read public repository")], "REQ-3")
        plan = main.plan_current_module_batches(current)
        self.assertEqual(plan["decision"], "keep_original")
        self.assertEqual([item["batch_id"] for item in plan["batches"]], ["REQ-3"])
        self.assertEqual(plan["planned_model_calls"], 2)

    def test_high_risk_closed_module_splits_by_existing_subtrees(self):
        current = module([
            {"id": "REQ-4-1", "type": "FOLDER", "dependencies": [],
             "children": [atom("REQ-4-1-1", text="browse files"), atom("REQ-4-1-2", ["REQ-4-1-1"], text="read commits")]},
            {"id": "REQ-4-2", "type": "FOLDER", "dependencies": [],
             "children": [atom("REQ-4-2-1", scenarios=5, text="create branch and save branch state")]},
            {"id": "REQ-4-3", "type": "FOLDER", "dependencies": [],
             "children": [atom("REQ-4-3-1", scenarios=5, text="publish release and update audit log")]},
        ])
        plan = main.plan_current_module_batches(current)
        self.assertEqual(plan["decision"], "split")
        self.assertLessEqual(plan["batch_count"], 3)
        self.assertEqual(plan["atomic_coverage_ok"], True)
        self.assertEqual(plan["duplicate_atomic_ids"], [])
        self.assertEqual(plan["missing_atomic_ids"], [])
        self.assertEqual([item["batch_id"] for item in plan["batches"]],
                         ["REQ-4.part-1", "REQ-4.part-2", "REQ-4.part-3"])
        self.assertEqual(plan["batches"][1]["dependency_closure_ids"], [])

    def test_unclosed_dependency_falls_back_to_original_module(self):
        current = module([
            {"id": "REQ-4-1", "type": "FOLDER", "dependencies": [],
             "children": [atom("REQ-4-1-1", ["REQ-4-9-9"], text="create state")]},
            {"id": "REQ-4-2", "type": "FOLDER", "dependencies": [],
             "children": [atom("REQ-4-2-1", text="write state")]},
        ])
        plan = main.plan_current_module_batches(current)
        self.assertEqual(plan["decision"], "keep_original")
        self.assertEqual(plan["stop_reason"], "dependency_graph_invalid")
        self.assertEqual(plan["planned_module_ids"], ["REQ-4"])

    def test_self_cycle_falls_back_without_silently_dropping_edge(self):
        current = module([atom("REQ-4-1", ["REQ-4-1"], text="update state")])
        plan = main.plan_current_module_batches(current)
        self.assertEqual(plan["decision"], "keep_original")
        self.assertIn("cycle", {item["kind"] for item in plan["issues"]})

    def test_short_hard_guard_falls_back_without_waiting(self):
        current = module([atom("REQ-4-1", text="create state") for _ in range(4)])
        plan = main.plan_current_module_batches(current, max_nodes=1)
        self.assertEqual(plan["decision"], "keep_original")
        self.assertEqual(plan["stop_reason"], "splitter_guard")

    def test_checkpoint_records_current_batch_scope_and_plan_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "frontend").mkdir()
            (output / "frontend" / "app.txt").write_text("valid", encoding="utf-8")
            (output / ".arc" / "engineering").mkdir(parents=True)
            (output / ".arc" / "engineering" / "preflight.json").write_text(
                json.dumps({"identity": {"fixture": "batch"}}), encoding="utf-8")
            plan = {"root_module_id": "REQ-4", "batch_id": "REQ-4.part-2",
                    "batch_ids": ["REQ-4.part-1", "REQ-4.part-2"]}
            plan["batch_plan_sha256"] = __import__("hashlib").sha256(
                json.dumps(plan, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            main.atomic_json(output / ".arc" / "engineering" / "current-batch-plan.json", plan)
            identity = {"fixture": "batch"}
            main.save_checkpoint(output, [], identity, {"status": "passed"},
                                 completed_batches=["REQ-4.part-1"],
                                 current_batch=plan)
            latest = main._checkpoint_record(output, identity, [])
            self.assertEqual(latest["manifest"]["completed_batches"], ["REQ-4.part-1"])
            self.assertEqual(latest["manifest"]["current_batch"]["batch_id"], "REQ-4.part-2")
            self.assertEqual(latest["manifest"]["batch_plan_sha256"], plan["batch_plan_sha256"])


if __name__ == "__main__":
    unittest.main()
