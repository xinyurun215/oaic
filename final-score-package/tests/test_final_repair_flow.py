"""Host order, call budget and file-level evidence for the single final repair.

Two layers: the host call trace through the real ``main()`` with only the model and browser
boundaries replaced, and real on-disk snapshot/restore regressions for product source.
"""

import json
from contextlib import ExitStack
import os
import sys
import tempfile
import types
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import test_recovery as support

main = support.main


def write_requirements(requirements: Path, identifiers) -> None:
    payload = {"id": "ROOT", "children": [
        {"id": identifier, "name": identifier, "description": "neutral",
         "children": [{"id": identifier + "-1", "type": "ATOMIC", "description": "neutral rule"},
                      {"id": identifier + "-2", "type": "ATOMIC", "description": "neutral rule"}]}
        for identifier in identifiers]}
    (requirements / "requirements.yaml").write_text(json.dumps(payload), encoding="utf-8")


class RepairFlowTests(unittest.TestCase):
    """Host trace: all observations first, then at most one repair, then one frozen replay."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.requirements = self.root / "requirements"
        self.requirements.mkdir()
        write_requirements(self.requirements, ("REQ-1", "REQ-2", "REQ-3"))
        self.output = self.root / "output"
        self.events = []
        self.observations = {}
        self.early_observations = {}
        self.errors = {}
        self.plan_failure = {}
        self.repair_failure = None
        self.failures_seen = []
        self.repair_delegate = main.repair_final_candidate
        support.FakeCodex.failure = None
        support.FakeCodex.break_build = False
        support.FakeCodex.return_error = None
        support.FakeCodex.failure_message = "429 Too Many Requests"
        support.FakeCodex.actions = {}
        support.FakeCodex.repair_action = None
        support.FakeCodex.threads = []
        for name, replacement in (("Codex", support.FakeCodex),
                                  ("build_frontend", lambda output: {"status": "passed"}),
                                  ("verify_backend_startup", lambda output: {"status": "passed"})):
            item = patch.object(main, name, new=replacement)
            item.start()
            self.addCleanup(item.stop)
        for name, replacement in (("plan_module_observation", self.plan),
                                  ("run_module_observation", self.observe),
                                  ("repair_final_candidate", self.repair)):
            item = patch.object(main, name, side_effect=replacement)
            item.start()
            self.addCleanup(item.stop)

    def plan(self, _codex, module, _requirements, output, _root, _model):
        self.events.append(("plan", module.node_id))
        if module.node_id in self.plan_failure:
            raise RuntimeError(self.plan_failure[module.node_id])
        folder = output / "module-observations" / module.node_id
        folder.mkdir(parents=True, exist_ok=True)
        script = b"// neutral frozen script"
        contract = main.observation_contract(module)
        main.atomic_json(folder / "contract.json", contract)
        (folder / "checks.spec.js").write_bytes(script)
        (folder / "frozen-v1.spec.js").write_bytes(script)
        return contract, script

    def observe(self, output, module, phase):
        statuses = (self.early_observations if phase == "early" else self.observations).setdefault(module.node_id, [])
        status = statuses.pop(0) if statuses else "passed"
        self.events.append(("observe-error" if status == "error" else "observe",
                            module.node_id, phase))
        evidence = output / "module-observations" / module.node_id / phase
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / "checks.spec.js").write_bytes(module.frozen_script or b"")
        (evidence / "result.json").write_text(json.dumps({"status": status}), encoding="utf-8")
        if status == "error":
            return {"status": "error", "tests": [], "duration_seconds": 0.0,
                    "evidence_path": evidence.relative_to(output).as_posix()}
        passed = status == "passed"
        tests = [{"title": title,
                  "status": "expected" if passed else "unexpected",
                  "executed": True,
                  "result_status": "passed" if passed else "failed",
                  "error": "" if passed else self.errors.get(module.node_id,
                                                             "expected value, received another value")}
                 for title in module.chain_titles]
        return {"status": status, "tests": tests, "duration_seconds": 0.0,
                "evidence_path": evidence.relative_to(output).as_posix()}

    def repair(self, codex, output_dir, requirements_dir, skills_dir, model, failures):
        self.events.append(("repair", "final-candidate"))
        self.failures_seen.append(list(failures))
        if self.repair_failure:
            raise RuntimeError(self.repair_failure)
        return self.repair_delegate(codex, output_dir, requirements_dir, skills_dir, model, failures)

    def execute(self, output=None, resume=None):
        args = ["main.py", str(self.requirements), "--output-dir", str(output or self.output)]
        if resume:
            args += ["--resume-from", str(resume)]
        with patch.object(sys, "argv", args), patch.dict(os.environ,
                                                         {"MODEL": "fake", "OPENAI_API_KEY": ""}):
            return main.main()

    def summary(self):
        path = self.output / ".arc" / "codex-recovery" / "summary.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def phases(self, kind):
        return [event for event in self.events if event[0] == kind]

    def test_each_module_plans_then_codes_then_observes_before_next_module(self):
        self.assertEqual(self.execute(), 0)
        self.assertEqual(self.events[:9], [
            ("plan", "REQ-1"), ("observe", "REQ-1", "early"),
            ("plan", "REQ-2"), ("observe", "REQ-2", "early"),
            ("plan", "REQ-3"), ("observe", "REQ-3", "early"),
            ("observe", "REQ-1", "before"), ("observe", "REQ-2", "before"),
            ("observe", "REQ-3", "before"),
        ])
        summary = self.summary()
        self.assertEqual([item["module_id"] for item in summary["early_observations"]],
                         ["REQ-1", "REQ-2", "REQ-3"])
        self.assertEqual(summary["acceptance"]["passed_chains"], 6)

    def test_provisional_failure_reaches_next_coding_prompt_but_final_rechecks(self):
        self.early_observations = {"REQ-1": ["failed"]}
        self.assertEqual(self.execute(), 0)
        repair_prompt = next(thread.prompts[0] for thread in support.FakeCodex.threads
                             if thread.prompts and "Observation repair" in thread.prompts[0])
        self.assertIn('"id": "REQ-1"', repair_prompt)
        self.assertIn('"atomic_id": "REQ-1-1"', repair_prompt)
        self.assertNotIn('"id": "REQ-2"', repair_prompt)
        next_prompt = next(thread.prompts[0] for thread in support.FakeCodex.threads
                           if thread.prompts and "Implement ROOT module 2/3" in thread.prompts[0])
        self.assertIn("REQ-1-1", next_prompt)
        self.assertIn("provisional", next_prompt)
        acceptance = self.summary()["acceptance"]
        self.assertEqual(acceptance["status"], "passed")
        self.assertEqual(self.summary()["status"], "complete")
        self.assertEqual(self.summary()["early_observations"][0]["repair"]["status"], "passed")

    def test_early_repair_failure_retries_once_and_preserves_prefix(self):
        self.early_observations = {"REQ-2": ["failed"]}
        support.FakeCodex.repair_actions = ["connection", "connection"]
        self.assertEqual(self.execute(), 0)
        summary = self.summary()
        self.assertEqual(summary["status"], "partial")
        self.assertTrue((self.output / "REQ-1.txt").is_file())
        self.assertTrue((self.output / "REQ-3.txt").exists())
        record = next(item for item in summary["early_observations"] if item["module_id"] == "REQ-2")
        self.assertEqual(record["failure_class"], "product_behavior")
        self.assertEqual(record["repair"]["attempts"], 2)
        checkpoints = [self.output / item for item in record["repair"]["working_checkpoints"]]
        self.assertEqual(len(checkpoints), 2)
        self.assertTrue(all((item / "failure.json").is_file() for item in checkpoints))

    def test_observation_failure_classes_keep_non_hard_failures_out_of_generation_block(self):
        classify = main.classify_observation_failure
        self.assertEqual(classify({"status": "error", "configuration_error": "ReferenceError: test is not defined"}),
                         "test_observation_error")
        self.assertEqual(classify({"status": "failed", "tests": [{"executed": True, "status": "unexpected"}]}),
                         "product_behavior")
        self.assertEqual(classify({"status": "error", "process_timed_out": True}),
                         "environment_connection_timeout")
        self.assertEqual(classify({"status": "error", "error": "dependency install failed"}),
                         "dependency_startup_hard_failure")

    def test_every_before_observation_precedes_the_single_repair(self):
        self.observations = {"REQ-1": ["failed"], "REQ-2": ["passed"], "REQ-3": ["error"]}
        self.assertEqual(self.execute(), 0)
        self.assertEqual(self.events[:10], [
            ("plan", "REQ-1"), ("observe", "REQ-1", "early"),
            ("plan", "REQ-2"), ("observe", "REQ-2", "early"),
            ("plan", "REQ-3"), ("observe", "REQ-3", "early"),
            ("observe", "REQ-1", "before"), ("observe", "REQ-2", "before"),
            ("observe-error", "REQ-3", "before"),
            ("repair", "final-candidate"),
        ])
        self.assertEqual(len(self.phases("repair")), 1)
        before = [event[1] for event in self.events
                  if event[0] in {"observe", "observe-error"} and event[2] == "before"]
        self.assertEqual(before, ["REQ-1", "REQ-2", "REQ-3"])
        self.assertEqual([event[1] for event in self.phases("observe")
                          if event[2] == "after"], ["REQ-1", "REQ-1", "REQ-2", "REQ-3"])

    def test_repair_context_keeps_atoms_without_executed_observations(self):
        self.observations = {"REQ-1": ["failed"]}
        self.plan_failure = {"REQ-3": "neutral planning format error"}
        self.assertEqual(self.execute(), 0)
        prompt = support.FakeCodex.threads[-1].prompts[0]
        self.assertNotIn('"id": "REQ-3-1"', prompt)
        self.assertNotIn('"module_id": "REQ-3"', prompt)

    def generate_with_preflight(self):
        self.output.mkdir()
        main.copy_template_contents(Path(main.__file__).parent / 'template', self.output)
        modules = main.load_root_modules(self.requirements)
        identity = main.input_identity(self.requirements, Path(main.__file__).parent)
        skills = main.copy_skills_to_output(Path(main.__file__).parent / 'skills', self.output)
        summary = main.implement_modules(self.requirements, self.output, modules, skills, identity, [])
        self.assertEqual(summary['status'], 'complete')
        return modules, identity, skills

    def test_deleted_receipt_cannot_replan_a_failed_memory_plan(self):
        self.plan_failure = {'REQ-2': 'neutral invalid planning output'}
        modules, identity, skills = self.generate_with_preflight()
        (self.output / '.arc/engineering/preflight.json').unlink()
        plans_before = len(self.phases('plan'))
        acceptance = main.run_final_acceptance(self.requirements, self.output, modules, skills, identity)
        self.assertEqual(len(self.phases('plan')), plans_before, 'deleted receipt triggered a late model planner')
        self.assertEqual(acceptance['records'][1]['status'], 'error')

    def test_completed_checkpoint_with_missing_preflight_stops_without_late_replan(self):
        modules, identity, skills = self.generate_with_preflight()
        receipt = self.output / '.arc/engineering/preflight.json'
        receipt.unlink()
        latest = json.loads((self.output / '.arc/codex-recovery/latest.json').read_text())
        checkpoint = self.output / '.arc/codex-recovery/checkpoints' / latest['checkpoint']
        (checkpoint / 'engineering/preflight.json').unlink()
        fresh_modules = main.load_root_modules(self.requirements)
        with patch.object(main, 'Codex', side_effect=AssertionError('integrity stop forbids model')) as client:
            acceptance = main.run_final_acceptance(self.requirements, self.output, fresh_modules,
                                                   skills, identity)
        client.assert_not_called()
        self.assertEqual(acceptance['repair']['model_turns'], 0)
        self.assertEqual(acceptance['status'], 'unverified')
        self.assertIn('integrity', json.dumps(acceptance).lower())

    def test_completed_checkpoint_with_corrupt_preflight_stops_without_late_replan(self):
        modules, identity, skills = self.generate_with_preflight()
        (self.output / '.arc/engineering/preflight.json').write_text('{broken', encoding='utf-8')
        fresh_modules = main.load_root_modules(self.requirements)
        with patch.object(main, 'Codex', side_effect=AssertionError('integrity stop forbids model')) as client:
            acceptance = main.run_final_acceptance(self.requirements, self.output, fresh_modules,
                                                   skills, identity)
        client.assert_not_called()
        self.assertEqual(acceptance['repair']['model_turns'], 0)
        self.assertIn('integrity', json.dumps(acceptance).lower())

    def test_same_identity_forged_preflight_is_not_checkpoint_authority(self):
        modules, identity, skills = self.generate_with_preflight()
        receipt = json.loads((self.output / '.arc/engineering/preflight.json').read_text())
        receipt['observations'] = {}
        main.atomic_json(self.output / '.arc/engineering/preflight.json', receipt)
        fresh_modules = main.load_root_modules(self.requirements)
        with patch.object(main, 'Codex', side_effect=AssertionError('forged authority forbids model')) as client:
            acceptance = main.run_final_acceptance(self.requirements, self.output, fresh_modules,
                                                   skills, identity)
        client.assert_not_called()
        self.assertIn('integrity', json.dumps(acceptance).lower())

    def test_integrity_stop_precedes_quota_reason_and_model_request(self):
        modules, identity, skills = self.generate_with_preflight()
        latest = json.loads((self.output / '.arc/codex-recovery/latest.json').read_text())
        checkpoint = self.output / '.arc/codex-recovery/checkpoints' / latest['checkpoint']
        (checkpoint / 'engineering/preflight.json').unlink()
        manifest_path = checkpoint / 'checkpoint.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['engineering_files'].pop('preflight.json', None)
        main.atomic_json(manifest_path, manifest)
        fresh_modules = main.load_root_modules(self.requirements)
        with patch.object(main, 'Codex', side_effect=AssertionError('integrity stop forbids model')) as client:
            acceptance = main.run_final_acceptance(self.requirements, self.output, fresh_modules,
                                                   skills, identity, completed_ids=['REQ-1'])
        client.assert_not_called()
        self.assertEqual(acceptance['status'], 'unverified')
        self.assertEqual(acceptance['repair']['model_turns'], 0)
        self.assertEqual(acceptance['integrity_stop']['stage'], 'checkpoint_integrity')
        self.assertNotIn('quota', json.dumps(acceptance['repair']['blocked_reason']).lower())

    def test_v2_preflight_hash_mismatch_stops_as_unverified_without_repair(self):
        modules, identity, skills = self.generate_with_preflight()
        receipt_path = self.output / '.arc/engineering/preflight.json'
        receipt = json.loads(receipt_path.read_text())
        receipt['observations'] = {}
        main.atomic_json(receipt_path, receipt)
        fresh_modules = main.load_root_modules(self.requirements)
        with patch.object(main, 'Codex', side_effect=AssertionError('hash mismatch forbids model')) as client:
            acceptance = main.run_final_acceptance(self.requirements, self.output, fresh_modules,
                                                   skills, identity)
        client.assert_not_called()
        self.assertEqual(acceptance['status'], 'unverified')
        self.assertEqual(acceptance['integrity_stop']['stage'], 'preflight_integrity')
        self.assertEqual(acceptance['repair']['model_turns'], 0)
        self.assertFalse(acceptance['repair']['attempted'])

    def test_final_acceptance_sdk_constructor_and_login_exit_failures_are_reported(self):
        modules, identity, skills = self.generate_with_preflight()
        fresh_modules = main.load_root_modules(self.requirements)

        class BrokenConstructor:
            def __init__(self, **_kwargs):
                raise RuntimeError('SDK constructor unavailable')

        with patch.object(main, 'Codex', BrokenConstructor):
            acceptance = main.run_final_acceptance(self.requirements, self.output, fresh_modules,
                                                   skills, identity)
        self.assertEqual(acceptance['status'], 'unverified')
        self.assertIn('SDK constructor unavailable', json.dumps(acceptance))

        class BrokenLoginExit(support.FakeCodex):
            def login_api_key(self, _key):
                raise RuntimeError('HTTP 403 login denied')
            def __exit__(self, *_args):
                raise RuntimeError('SDK exit also failed')

        with patch.object(main, 'Codex', BrokenLoginExit), \
                patch.dict(os.environ, {'OPENAI_API_KEY': 'offline-placeholder'}):
            acceptance = main.run_final_acceptance(self.requirements, self.output, fresh_modules,
                                                   skills, identity)
        report = json.dumps(acceptance)
        self.assertIn('HTTP 403 login denied', report)
        self.assertIn('SDK exit also failed', report)
        self.assertEqual(acceptance['status'], 'unverified')

    def test_rewritten_receipt_cannot_remove_memory_quota_block(self):
        self.plan_failure = {'REQ-2': 'Quota exceeded. Check billing details.'}
        self.observations = {'REQ-1': ['failed']}
        self.output.mkdir()
        main.copy_template_contents(Path(main.__file__).parent / 'template', self.output)
        modules = main.load_root_modules(self.requirements)
        identity = main.input_identity(self.requirements, Path(main.__file__).parent)
        skills = main.copy_skills_to_output(Path(main.__file__).parent / 'skills', self.output)
        summary = main.implement_modules(self.requirements, self.output, modules, skills, identity, [])
        self.assertEqual(summary['status'], 'blocked')
        receipt_path = self.output / '.arc/engineering/preflight.json'
        receipt = json.loads(receipt_path.read_text())
        receipt['observations'] = {identifier: entry for identifier, entry in receipt['observations'].items()
                                   if entry['status'] == 'prepared'}
        receipt_path.write_text(json.dumps(receipt))
        acceptance = main.run_final_acceptance(self.requirements, self.output, modules, skills, identity,
                                               completed_ids=summary['completed_ids'])
        self.assertEqual(self.phases('repair'), [], 'rewritten receipt authorized an extra repair model call')
        self.assertFalse(acceptance['repair']['attempted'])
        self.assertEqual(acceptance['integrity_stop']['stage'], 'preflight_integrity')
        self.assertIn('integrity stop', acceptance['repair']['blocked_reason']['error']['message'])
        self.assertEqual(acceptance['chain_counts']['failed'], 0)
        self.assertGreater(acceptance['chain_counts']['unverified'], 0)

    def test_invalid_frozen_replay_cannot_claim_improved_or_passed_atoms(self):
        self.observations = {"REQ-1": ["failed", "passed"]}
        support.FakeCodex.repair_action = "create_script"
        self.assertEqual(self.execute(), 0)
        changes = [item for item in self.summary()['acceptance']['atomic_changes']
                   if item['id'].startswith('REQ-1-')]
        self.assertTrue(changes)
        self.assertTrue(all(item['after'] == 'unverified' and item['change'] != 'improved' for item in changes))

    def test_repair_replay_regression_restores_baseline_source(self):
        self.observations = {"REQ-1": ["passed", "failed"],
                             "REQ-2": ["failed", "passed"]}
        self.assertEqual(self.execute(), 0)
        self.assertEqual((self.output / "REQ-2.txt").read_text(), "REQ-2")
        repair = self.summary()["acceptance"]["repair"]
        self.assertFalse(repair["kept"])
        self.assertTrue(repair["restored"])
        self.assertTrue(repair["replay_regressed"])

    def test_invalid_replay_is_unverified_without_automatic_keep_or_rollback(self):
        self.observations = {"REQ-1": ["failed", "error"]}
        self.assertEqual(self.execute(), 0)
        repair = self.summary()["acceptance"]["repair"]
        self.assertFalse(repair["kept"])
        self.assertTrue(repair["restored"])
        self.assertEqual(repair["selection"], "unchanged")
        self.assertEqual((self.output / "REQ-1.txt").read_text(), "REQ-1")

    def test_replay_regression_restore_failure_is_unverified_not_kept(self):
        self.observations = {"REQ-1": ["passed", "failed"],
                             "REQ-2": ["failed", "passed"]}
        real_restore = main.restore_repair_snapshot
        def fail_baseline(snapshot, output):
            if "repair-baselines" in str(snapshot):
                raise OSError("baseline restore unavailable")
            return real_restore(snapshot, output)
        with patch.object(main, "restore_repair_snapshot", side_effect=fail_baseline):
            self.assertEqual(self.execute(), 0)
        repair = self.summary()["acceptance"]["repair"]
        self.assertFalse(repair["kept"])
        self.assertEqual(repair["selection"], "unverified")
        self.assertIn("baseline restore unavailable", json.dumps(repair))

    def test_environment_error_in_failed_replay_is_unverified_without_rollback(self):
        self.observations = {"REQ-1": ["passed", "failed"],
                             "REQ-2": ["failed", "passed"]}
        observer = self.observe
        def environment_error(output, module, phase):
            result = observer(output, module, phase)
            if module.node_id == "REQ-1" and phase == "after":
                result["tests"][0]["error_origin"] = "environment"
            return result
        with patch.object(main, "run_module_observation", side_effect=environment_error):
            self.assertEqual(self.execute(), 0)
        repair = self.summary()["acceptance"]["repair"]
        self.assertFalse(repair["kept"])
        self.assertFalse(repair["restored"])
        self.assertEqual(repair["selection"], "unverified")
        self.assertEqual((self.output / "REQ-1.txt").read_text(), "REQ-1")

    def test_script_and_configuration_errors_alone_trigger_no_repair(self):
        self.observations = {"REQ-1": ["error"], "REQ-2": ["error"], "REQ-3": ["error"]}
        self.assertEqual(self.execute(), 0)
        self.assertEqual(self.phases("repair"), [])
        summary = self.summary()
        self.assertFalse(summary["acceptance"]["repair"]["attempted"])
        self.assertEqual(summary["acceptance"]["status"], "unverified")
        self.assertEqual(summary["acceptance"]["chain_counts"]["failed"], 0)
        self.assertGreater(summary["acceptance"]["unverified_count"], 0)
        self.assertEqual(summary["delivery"]["exit_code"], 0)

    def test_unfinished_step_correction_is_bounded_and_never_triggers_product_repair(self):
        def runtime_report(output, module, phase):
            result = self.observe(output, module, phase)
            if module.node_id == 'REQ-1':
                report = {'errors': [], 'suites': [{'specs': [{
                    'title': title, 'tests': [{'status': 'unexpected' if index == 0 else 'expected',
                    'results': [{'status': 'failed' if index == 0 else 'passed', 'errors': [
                        {'message': 'Error: expect(locator).toHaveText failed'},
                        {'message': 'Error: ARC_OBSERVATION_UNFINISHED_STEP: 1 test.step promise(s) still pending'}
                    ] if index == 0 else []}]}]}
                    for index, title in enumerate(module.chain_titles)]}]}
                result.update(main.parse_observation_report(report, 1))
            return result
        with patch.object(main, 'run_module_observation', side_effect=runtime_report):
            self.assertEqual(self.execute(), 0)
        result = self.summary()['acceptance']
        self.assertEqual(result['unverified_ids'], ['REQ-1-1'])
        self.assertEqual(result['chain_counts']['failed'], 0)
        self.assertEqual(result['repair']['model_turns'], 0)
        self.assertEqual(result['repair']['script_correction_turns'], 1)
        self.assertEqual(self.phases('repair'), [])

    def test_multiple_real_failures_use_module_batches_without_replanning(self):
        self.observations = {"REQ-1": ["failed", "failed"], "REQ-2": ["failed", "failed"],
                             "REQ-3": ["passed"]}
        self.assertEqual(self.execute(), 0)
        self.assertEqual(len(self.phases("repair")), 2)
        # One module per batch, preserving the original atomic observation order.
        self.assertEqual([[entry["module_id"] for entry in batch] for batch in self.failures_seen],
                         [["REQ-1", "REQ-1"], ["REQ-2", "REQ-2"]])
        self.assertEqual([entry["atomic_id"] for batch in self.failures_seen for entry in batch],
                         ["REQ-1-1", "REQ-1-2", "REQ-2-1", "REQ-2-2"])
        self.assertEqual(len(self.phases("plan")), 3, "the repair must not replan observations")
        self.assertEqual(self.summary()["acceptance"]["status"], "failed")
        self.assertEqual(self.summary()["delivery"]["exit_code"], 0)

    def test_planning_transient_failure_records_unverified_and_continues(self):
        self.plan_failure = {"REQ-2": "Connection reset by peer"}
        self.assertEqual(self.execute(), 0)
        self.assertEqual([event[1] for event in self.phases("plan")],
                         ["REQ-1", "REQ-2", "REQ-3"])
        self.assertEqual([event for event in self.phases("observe") if event[1] == "REQ-3"],
                         [("observe", "REQ-3", "early"), ("observe", "REQ-3", "before")])
        summary = self.summary()
        record = next(item for item in summary["acceptance"]["records"]
                      if item["module_id"] == "REQ-2")
        self.assertEqual(record["status"], "error")
        self.assertEqual(summary["delivery"]["exit_code"], 0)

    def test_planning_auth_denial_stops_later_model_calls_and_keeps_candidate(self):
        self.plan_failure = {"REQ-2": "HTTP 401 Unauthorized"}
        self.assertEqual(self.execute(), 0)
        self.assertEqual([event[1] for event in self.phases("plan")], ["REQ-1", "REQ-2"])
        self.assertTrue((self.output / "REQ-1.txt").is_file())
        self.assertFalse((self.output / "REQ-2.txt").is_file())
        summary = self.summary()
        self.assertEqual(summary["delivery"], {"status": "partial", "exit_code": 0})
        stopped = next(item for item in summary["acceptance"]["records"]
                       if item["module_id"] == "REQ-3")
        self.assertEqual(stopped["status"], "error")

    def test_repair_failure_after_all_observations_keeps_later_module_evidence(self):
        self.observations = {"REQ-1": ["failed"]}
        self.repair_failure = "Connection reset by peer"
        self.assertEqual(self.execute(), 0)
        self.assertEqual(len(self.failures_seen), 2)
        # Module 3 was observed before the repair, so its raw before-evidence must survive.
        self.assertTrue((self.output / "module-observations" / "REQ-3" / "before" /
                         "result.json").is_file())
        self.assertEqual([event[1] for event in self.phases("observe")
                          if event[2] == "before"], ["REQ-1", "REQ-2", "REQ-3"])
        self.assertEqual(self.summary()["delivery"]["exit_code"], 0)
        self.assertEqual(self.summary()["acceptance"]["repair"]["restored"], True)

    def test_repair_prompt_carries_bounded_failure_context_only(self):
        self.errors = {"REQ-1": "y" * 5000}
        self.observations = {"REQ-1": ["failed"]}
        self.assertEqual(self.execute(), 0)
        failure = self.failures_seen[0][0]
        self.assertEqual(sorted(failure), ["atomic_id", "batch_subtree", "error_excerpt", "evidence_path",
                                           "frozen_script", "host_state", "module_id", "requirement_file"])
        self.assertEqual(failure["module_id"], "REQ-1")
        self.assertEqual(failure["atomic_id"], "REQ-1-1")
        self.assertLessEqual(len(failure["error_excerpt"]), 800)
        self.assertTrue(Path(failure["frozen_script"]).is_absolute())
        self.assertTrue(Path(failure["frozen_script"]).as_posix()
                        .endswith("module-observations/REQ-1/checks.spec.js"))
        self.assertTrue(Path(failure["requirement_file"]).as_posix().endswith("requirements.yaml"))
        self.assertTrue(Path(failure["evidence_path"]).as_posix()
                        .endswith("module-observations/REQ-1/before"))
        prompt = support.FakeCodex.threads[-1].prompts[0]
        self.assertNotIn("y" * 2000, prompt)
        self.assertIn('"atomic_id": "REQ-1-1"', prompt)
        self.assertNotIn('"tests":', prompt)
        self.assertNotIn("Observation did not run", prompt)

    def test_script_correction_and_prior_repairs_leave_only_one_normal_turn(self):
        modules, identity, skills = self.generate_with_preflight()
        receipt = json.loads((self.output / '.arc/engineering/preflight.json').read_text())
        receipt['script_corrections_used'] = 1
        receipt['normal_repairs_used'] = 6
        main.atomic_json(self.output / '.arc/engineering/preflight.json', receipt)
        modules = [replace(module, planning_authority=receipt) for module in modules]
        self.observations = {identifier: ['failed'] for identifier in ('REQ-1', 'REQ-2', 'REQ-3')}
        acceptance = main.run_final_acceptance(self.requirements, self.output, modules, skills, identity)
        self.assertEqual(acceptance['repair']['script_correction_turns'], 1)
        self.assertEqual(acceptance['repair']['model_turns'], 0)
        self.assertFalse(acceptance['repair']['attempted'])
        self.assertEqual(acceptance['integrity_stop']['stage'], 'preflight_integrity')
        self.assertEqual(len(self.phases('repair')), 0)
        trusted = main.engineering.checkpoint_authority(self.output, identity)
        self.assertEqual(trusted.get('normal_repairs_used', 0), 0)
        self.assertEqual(trusted.get('script_corrections_used', 0), 0)
        resumed = [replace(module, planning_authority=trusted) for module in modules]
        (self.output / 'REQ-1.txt').write_text('changed after cached acceptance')
        self.observations = {identifier: ['failed'] for identifier in ('REQ-1', 'REQ-2', 'REQ-3')}
        second = main.run_final_acceptance(self.requirements, self.output, resumed, skills, identity)
        self.assertEqual(second['repair']['model_turns'], 0)
        self.assertFalse(second['repair']['attempted'])
        self.assertEqual(second['integrity_stop']['stage'], 'preflight_integrity')
        self.assertEqual(len(self.phases('repair')), 0)

    def test_correction_auth_denial_stops_next_planner_and_coder(self):
        self.early_observations = {'REQ-1': ['error']}
        with patch.object(main, 'correct_observation_script',
                          side_effect=main.ModuleResultError(RuntimeError('HTTP 403 Forbidden'))):
            self.assertEqual(self.execute(), 0)
        self.assertEqual([event[1] for event in self.phases('plan')], ['REQ-1'])
        self.assertFalse((self.output / 'REQ-2.txt').exists())
        self.assertEqual(self.summary()['status'], 'blocked')
        self.assertEqual(self.summary()['acceptance']['mode'], 'observation_only')

    def test_checkpoint_authority_prevents_disk_budget_reset_and_late_replanning(self):
        modules, identity, skills = self.generate_with_preflight()
        receipt = json.loads((self.output / '.arc/engineering/preflight.json').read_text())
        receipt.update(script_corrections_used=1, normal_repairs_used=7, compaction_recovery_used=1)
        main.atomic_json(self.output / '.arc/engineering/preflight.json', receipt)
        main.save_checkpoint(self.output, [module.node_id for module in modules], identity,
                             {'status': 'passed'})
        forged = dict(receipt, script_corrections_used=0, normal_repairs_used=0,
                      compaction_recovery_used=0, observations={})
        main.atomic_json(self.output / '.arc/engineering/preflight.json', forged)
        fresh_modules = main.load_root_modules(self.requirements)
        with patch.object(main, 'Codex',
                          side_effect=AssertionError('checkpoint must prevent late replanning')):
            trusted = main.prepare_engineering(self.requirements, self.output, fresh_modules,
                                               identity, support.FakeCodex())
        self.assertEqual(trusted, receipt)
        self.assertEqual(json.loads((self.output / '.arc/engineering/preflight.json').read_text()), receipt)
        self.observations = {identifier: ['failed'] for identifier in ('REQ-1', 'REQ-2', 'REQ-3')}
        result = main.run_final_acceptance(self.requirements, self.output, fresh_modules, skills, identity)
        self.assertEqual(result['repair']['model_turns'], 0)
        self.assertEqual(result['repair']['script_correction_turns'], 1)
        self.assertEqual(result['repair']['recovery_attempts'], 1)
        self.assertEqual(self.phases('repair'), [])

    def test_v2_selection_invalidates_pass_cache_and_corruption_never_falls_back_to_v1(self):
        modules, identity, skills = self.generate_with_preflight()
        first = main.run_final_acceptance(self.requirements, self.output, modules, skills, identity)
        self.assertEqual(first['status'], 'passed')
        observed_before = len(self.phases('observe'))
        receipt = json.loads((self.output / '.arc/engineering/preflight.json').read_text())
        folder = self.output / 'module-observations/REQ-1'
        original = (folder / 'frozen-v1.spec.js').read_bytes()
        selected = original + b'\n// corrected selection'
        (folder / 'frozen-v2.spec.js').write_bytes(selected)
        (folder / 'checks.spec.js').write_bytes(selected)
        receipt['observations']['REQ-1'].update(version=2,
            original_script_sha256=main.hashlib.sha256(original).hexdigest(),
            script_sha256=main.hashlib.sha256(selected).hexdigest())
        receipt['script_corrections_used'] = 1
        main.atomic_json(self.output / '.arc/engineering/preflight.json', receipt)
        modules = [replace(module, planning_authority=receipt) for module in modules]
        second = main.run_final_acceptance(self.requirements, self.output, modules, skills, identity)
        self.assertEqual(second['status'], 'unverified')
        self.assertEqual(second['integrity_stop']['stage'], 'preflight_integrity')
        self.assertEqual(second['repair']['model_turns'], 0)
        self.assertEqual(len(self.phases('observe')), observed_before)
        (folder / 'frozen-v2.spec.js').write_bytes(b'corrupt selected version')
        third = main.run_final_acceptance(self.requirements, self.output, modules, skills, identity)
        self.assertEqual(third['status'], 'unverified')
        self.assertEqual(third['integrity_stop']['stage'], 'preflight_integrity')
        self.assertEqual(third['repair']['model_turns'], 0)
        self.assertEqual((folder / 'frozen-v1.spec.js').read_bytes(), original)

    def correction_failure_run(self, forbidden=True, report_error=False, restore_error=False,
                               frozen_restore_error=False, stop_export_error=False):
        actual_corrector = main.correct_observation_script
        original_write = main.atomic_json
        self.early_observations = {'REQ-1': ['error']}
        app = self.output / 'frontend/src/App.tsx'
        preserved = []
        calls = []
        frozen_file = self.output / 'module-observations/REQ-1/frozen-v1.spec.js'
        class Client:
            def thread_start(self, **options):
                def run(prompt):
                    calls.append('correction')
                    if restore_error or frozen_restore_error or stop_export_error:
                        preserved.append(app.read_bytes())
                    if restore_error:
                        app.write_text('failed correction draft')
                    if frozen_restore_error:
                        frozen_file.write_text('failed frozen correction draft')
                    if forbidden:
                        return types.SimpleNamespace(error=types.SimpleNamespace(message='Forbidden', status_code=403))
                    raise RuntimeError('non-resource correction failure')
                return types.SimpleNamespace(run=run)
        def correct(codex, module, requirements, output, receipt, observed, model, modules):
            observed = {**observed, 'configuration_error': 'ReferenceError: test is not defined at checks.spec.js:1'}
            return actual_corrector(Client(), module, requirements, output, receipt, observed, model, modules)
        def write(path, data):
            if report_error and path.name == 'correction-failure.json':
                raise OSError('failure report unavailable')
            if stop_export_error and calls and path.name == 'latest.json':
                raise OSError('STOP checkpoint latest publication unavailable')
            return original_write(path, data)
        with ExitStack() as stack:
            stack.enter_context(patch.object(main, 'correct_observation_script', side_effect=correct))
            if report_error or stop_export_error:
                stack.enter_context(patch.object(main, 'atomic_json', side_effect=write))
            if restore_error:
                stack.enter_context(patch.object(main, 'restore_repair_snapshot', side_effect=OSError('restore unavailable')))
            if frozen_restore_error:
                original_bytes_write = Path.write_bytes
                def frozen_write(path, data):
                    if path == frozen_file and path.is_file() and path.read_bytes() == b'failed frozen correction draft':
                        raise OSError('frozen restoration unavailable')
                    return original_bytes_write(path, data)
                stack.enter_context(patch.object(Path, 'write_bytes', new=frozen_write))
            self.assertEqual(self.execute(), 0)
        self.assertEqual(calls, ['correction'])
        self.assertEqual([event[1] for event in self.phases('plan')], ['REQ-1'])
        self.assertFalse((self.output / 'REQ-2.txt').exists())
        summary = self.summary()
        self.assertEqual(summary['status'], 'blocked')
        self.assertEqual(summary['acceptance']['mode'], 'observation_only')
        reason = next(item for item in summary['pending_issues'] if item.get('stage') == 'early_observation')
        if forbidden:
            self.assertEqual(reason['error']['status_code'], '403')
        else:
            self.assertEqual(reason['error']['status_code'], 'unavailable')
            self.assertIn('non-resource correction failure', reason['error']['message'])
        if restore_error or frozen_restore_error or stop_export_error:
            detail = reason['host_integrity_stop']
            snapshot = Path(detail['snapshot_path'])
            self.assertTrue(snapshot.is_dir())
            self.assertEqual((snapshot / 'frontend/src/App.tsx').read_bytes(), preserved[0])
            self.assertEqual(summary['acceptance']['passed_chains'], 0)
            self.assertTrue(snapshot.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()))
            self.addCleanup(main.shutil.rmtree, snapshot)
        return summary

    def assert_correction_stop_survives_separate_resume(self):
        planned = len(self.phases('plan'))
        requested = len(support.FakeCodex.threads)
        resumed = self.root / 'resumed'
        self.assertEqual(self.execute(resumed, self.output), 0)
        self.assertEqual(len(self.phases('plan')), planned)
        self.assertEqual(len(support.FakeCodex.threads), requested)
        summary = json.loads((resumed / '.arc/codex-recovery/summary.json').read_text())
        self.assertEqual(summary['status'], 'blocked')
        self.assertEqual(summary['acceptance']['mode'], 'observation_only')

    def test_correction_forbidden_with_report_failure_stops_all_later_models(self):
        self.correction_failure_run(report_error=True)
        self.assert_correction_stop_survives_separate_resume()

    def test_correction_forbidden_with_failed_restore_keeps_snapshot_and_stops_models(self):
        self.correction_failure_run(report_error=True, restore_error=True)
        self.assert_correction_stop_survives_separate_resume()

    def test_nonresource_correction_failed_restore_is_host_stop_without_fake_403(self):
        self.correction_failure_run(forbidden=False, restore_error=True)
        self.assert_correction_stop_survives_separate_resume()

    def test_correction_forbidden_failed_frozen_restore_keeps_snapshot_and_stops_models(self):
        self.correction_failure_run(frozen_restore_error=True)
        self.assert_correction_stop_survives_separate_resume()

    def test_failed_stop_publication_trusted_pending_blocks_separate_resume(self):
        self.correction_failure_run(report_error=True, stop_export_error=True)
        self.assert_correction_stop_survives_separate_resume()


class RepairSnapshotTests(unittest.TestCase):
    """Real on-disk proof that restoring reverts changed, deleted and added product files."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "output"
        self.snapshot = self.root / "snapshot"

    def test_modified_deleted_and_added_files_are_reverted(self):
        source = self.output / "frontend" / "src"
        source.mkdir(parents=True)
        (source / "A.tsx").write_text("A original", encoding="utf-8")
        (source / "B.tsx").write_text("B original", encoding="utf-8")
        manifest = main.copy_repair_snapshot(self.output, self.snapshot)
        self.assertEqual(sorted(manifest), ["frontend/src/A.tsx", "frontend/src/B.tsx"])

        (source / "A.tsx").write_text("A damaged by repair", encoding="utf-8")
        (source / "B.tsx").unlink()
        (source / "C.tsx").write_text("C added by repair", encoding="utf-8")
        result = main.restore_repair_snapshot(self.snapshot, self.output)

        self.assertEqual((source / "A.tsx").read_text(encoding="utf-8"), "A original")
        self.assertEqual((source / "B.tsx").read_text(encoding="utf-8"), "B original")
        self.assertFalse((source / "C.tsx").exists())
        self.assertEqual(result["removed"], ["frontend/src/C.tsx"])
        self.assertIn("frontend/src/A.tsx", result["restored"])
        self.assertIn("frontend/src/B.tsx", result["restored"])

    def test_evidence_runtime_and_build_files_stay_outside_the_snapshot_scope(self):
        backend = self.output / "backend"
        (backend / "src").mkdir(parents=True)
        (backend / "src" / "app.js").write_text("app", encoding="utf-8")
        (backend / "database.db").write_text("db", encoding="utf-8")
        (backend / "node_modules").mkdir()
        (backend / "node_modules" / "dep.js").write_text("dep", encoding="utf-8")
        (backend / "dist").mkdir()
        (backend / "dist" / "bundle.js").write_text("bundle", encoding="utf-8")
        evidence = self.output / "module-observations" / "REQ-1" / "before"
        evidence.mkdir(parents=True)
        (evidence / "result.json").write_text("{}", encoding="utf-8")
        (evidence / "failure.png").write_bytes(b"png")
        (self.output / ".arc" / "codex-recovery").mkdir(parents=True)
        (self.output / ".arc" / "codex-recovery" / "events.jsonl").write_text("{}", encoding="utf-8")
        (self.output / ".codex" / "skills" / "example").mkdir(parents=True)
        (self.output / ".codex" / "skills" / "example" / "SKILL.md").write_text("skill", encoding="utf-8")
        (self.output / ".codex" / "auth.json").write_text("secret", encoding="utf-8")

        manifest = main.copy_repair_snapshot(self.output, self.snapshot)
        self.assertIn("backend/src/app.js", manifest)
        self.assertIn(".codex/skills/example/SKILL.md", manifest)
        for excluded in ("backend/database.db", "backend/node_modules/dep.js", "backend/dist/bundle.js",
                         "module-observations/REQ-1/before/result.json",
                         "module-observations/REQ-1/before/failure.png",
                         ".arc/codex-recovery/events.jsonl", ".codex/auth.json"):
            self.assertNotIn(excluded, manifest, excluded)

        # A repair that damages source, drops evidence and deletes the runtime database.
        (backend / "src" / "app.js").write_text("broken", encoding="utf-8")
        (evidence / "result.json").unlink()
        (backend / "database.db").unlink()
        main.restore_repair_snapshot(self.snapshot, self.output)

        self.assertEqual((backend / "src" / "app.js").read_text(encoding="utf-8"), "app")
        # Out-of-scope files are neither restored nor deleted.
        self.assertFalse((backend / "database.db").exists())
        self.assertFalse((evidence / "result.json").exists())
        self.assertTrue((evidence / "failure.png").is_file())
        self.assertTrue((backend / "node_modules" / "dep.js").is_file())
        self.assertTrue((backend / "dist" / "bundle.js").is_file())
        self.assertTrue((self.output / ".arc" / "codex-recovery" / "events.jsonl").is_file())
        self.assertTrue((self.output / ".codex" / "skills" / "example" / "SKILL.md").is_file())
        self.assertTrue((self.output / ".codex" / "auth.json").is_file())

    def test_dependency_sync_follows_the_restored_package_files(self):
        frontend = self.output / "frontend"
        frontend.mkdir(parents=True)
        (frontend / "package.json").write_text('{"name": "app"}', encoding="utf-8")
        (frontend / "package-lock.json").write_text('{"lockfileVersion": 3}', encoding="utf-8")
        state = self.output / ".arc" / "codex-recovery" / "installed-dependencies.json"
        state.parent.mkdir(parents=True)
        r0 = main.dependency_fingerprint(frontend)
        state.write_text(json.dumps({str(frontend.resolve()): r0}), encoding="utf-8")

        main.copy_repair_snapshot(self.output, self.snapshot)
        # The repair changes the dependency files and records a successful install against them.
        (frontend / "package.json").write_text('{"name": "app", "dependencies": {"extra": "1"}}',
                                               encoding="utf-8")
        r1 = main.dependency_fingerprint(frontend)
        self.assertNotEqual(r1, r0)
        state.write_text(json.dumps({str(frontend.resolve()): r1}), encoding="utf-8")

        main.restore_repair_snapshot(self.snapshot, self.output)
        main.invalidate_dependency_state(self.output)

        self.assertFalse(state.exists())
        self.assertEqual((frontend / "package.json").read_text(encoding="utf-8"), '{"name": "app"}')
        self.assertEqual(main.dependency_fingerprint(frontend), r0)


if __name__ == "__main__":
    unittest.main()
