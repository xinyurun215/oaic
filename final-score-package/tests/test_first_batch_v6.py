"""First-batch host checks: SDK/build/browser boundaries are substitutes; files are real."""
import json
import os
import unittest
from unittest.mock import patch

import test_recovery as support

main = support.main


class FirstBatchTests(unittest.TestCase):
    def setUp(self):
        self.harness = support.ModuleObservationFlowTests('test_first_pass_needs_no_repair')
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        self.harness.observe(['passed', 'passed'])

    def test_error_evidence_keeps_mapping_message_shape(self):
        self.assertEqual(main.error_evidence({'message': 'Quota exceeded'})['message'], 'Quota exceeded')

    def test_blocked_reason_always_exposes_nested_error_message(self):
        reason = main._stable_blocked_reason({'stage': 'repair', 'message': 'Quota exceeded'}, 'repair')
        self.assertEqual(reason['error']['message'], 'Quota exceeded')

    def test_deterministic_preparation_never_constructs_model_client(self):
        self.harness.output.mkdir()
        modules = main.load_root_modules(self.harness.requirements)
        with patch.object(main, 'Codex', side_effect=AssertionError('model forbidden')):
            receipt = main.prepare_engineering(self.harness.requirements, self.harness.output, modules, {})
        self.assertTrue(receipt['finished'])
        self.assertEqual(receipt['observations'], {})
        self.assertEqual(modules[0].planning_authority, receipt)

    def test_preparation_failure_retains_primary_error_and_uses_safe_source_state(self):
        with patch.object(main, 'prepare_engineering', side_effect=OSError('aux preparation failed')):
            self.assertEqual(self.harness.execute(), 0)
        summary = self.harness.summary()
        self.assertEqual(summary['completed_ids'], ['REQ-1', 'REQ-2'])
        self.assertIn('aux preparation failed', json.dumps(summary))
        self.assertNotIn('UnboundLocalError', json.dumps(summary))

    def test_aux_handoff_failure_keeps_code_and_does_not_retry_or_restore(self):
        with patch.object(main.engineering, 'handoff', side_effect=RuntimeError('aux handoff failed')), \
                patch.object(main, 'restore_failed_module', side_effect=AssertionError('rollback forbidden')):
            self.assertEqual(self.harness.execute(), 0)
        self.assertEqual(self.harness.summary()['completed_ids'], ['REQ-1', 'REQ-2'])
        self.assertEqual(len(support.FakeCodex.threads), 2)
        self.assertEqual((self.harness.output / 'REQ-1.txt').read_text(), 'REQ-1')
        self.assertIn('aux handoff failed', (self.harness.output / '.arc/codex-recovery/events.jsonl').read_text())

    def test_client_constructor_and_enter_failure_reach_real_delivery_reports(self):
        for phase in ('constructor', 'enter'):
            class Broken(support.FakeCodex):
                def __init__(self, **kwargs):
                    if phase == 'constructor':
                        raise RuntimeError('SDK constructor unavailable')
                    super().__init__(**kwargs)
                def __enter__(self):
                    raise RuntimeError('SDK enter unavailable')
            output = self.harness.root / phase
            with self.subTest(phase=phase), patch.object(main, 'Codex', Broken):
                self.assertEqual(self.harness.execute(output), 1)
                summary = json.loads((output / '.arc/codex-recovery/summary.json').read_text())
                self.assertEqual(summary['delivery']['status'], 'unavailable')
                self.assertIn('SDK ' + phase, json.dumps(summary))
                self.assertTrue((output / 'module-observations/generation.json').is_file())

    def test_client_exit_failure_delivers_checkpoints_and_uses_only_offline_acceptance(self):
        instances = []
        class BrokenExit(support.FakeCodex):
            def __init__(self, **kwargs):
                instances.append(self)
                if len(instances) > 1:
                    raise AssertionError('later client forbidden')
                super().__init__(**kwargs)
            def __exit__(self, *args):
                raise RuntimeError('Quota exceeded during SDK exit')
        with patch.object(main, 'Codex', BrokenExit):
            self.assertEqual(self.harness.execute(), 0)
        summary = self.harness.summary()
        self.assertEqual(summary['completed_ids'], ['REQ-1', 'REQ-2'])
        self.assertEqual(summary['acceptance']['mode'], 'observation_only')
        self.assertIn('Quota exceeded during SDK exit', json.dumps(summary))
        self.assertEqual(len(instances), 1)

    def test_login_failure_and_exit_error_preserve_both_primary_errors(self):
        class BrokenLogin(support.FakeCodex):
            def login_api_key(self, key):
                raise RuntimeError('HTTP 403 login denied')
            def __exit__(self, *args):
                raise RuntimeError('SDK exit also failed')
        args = ['main.py', str(self.harness.requirements), '--output-dir', str(self.harness.output)]
        with patch.object(main.sys, 'argv', args), \
                patch.dict(os.environ, {'MODEL': 'fake', 'OPENAI_API_KEY': 'offline-placeholder'}), \
                patch.object(main, 'Codex', BrokenLogin):
            self.assertEqual(main.main(), 1)
        report = json.dumps(self.harness.summary())
        self.assertIn('HTTP 403 login denied', report)
        self.assertIn('SDK exit also failed', report)
        self.assertEqual(support.FakeCodex.threads, [])

    def test_minimal_host_timing_and_usage_are_honest_records(self):
        self.assertEqual(self.harness.execute(), 0)
        summary = self.harness.summary()
        self.assertGreaterEqual(summary['timing']['generation_seconds'], 0)
        self.assertGreaterEqual(summary['timing']['first_coding_seconds'], 0)
        self.assertIsNone(summary['usage'])
        receipt = json.loads((self.harness.output / '.arc/engineering/preflight.json').read_text())
        self.assertTrue(all(entry['duration_seconds'] >= 0 for entry in receipt['observations'].values()))

    def test_source_missing_dependency_and_atomic_cycle_stop_before_model(self):
        for dependencies in (['MISSING'], ['REQ-1-1']):
            payload = json.loads((self.harness.requirements / 'requirements.yaml').read_text())
            payload['children'][0]['children'][0]['dependencies'] = dependencies
            (self.harness.requirements / 'requirements.yaml').write_text(json.dumps(payload))
            modules = main.load_root_modules(self.harness.requirements)
            with self.subTest(dependencies=dependencies), \
                    patch.object(main, 'Codex', side_effect=AssertionError('model forbidden')):
                with self.assertRaisesRegex(ValueError, 'dependency'):
                    main.prepare_engineering(self.harness.requirements, self.harness.output, modules, {})

    def test_untrusted_existing_preflight_never_resets_correction_budget(self):
        output = self.harness.output
        main.atomic_json(output / '.arc/engineering/preflight.json', {'script_corrections_used': 1})
        with patch.object(main, 'Codex', side_effect=AssertionError('model forbidden')):
            self.assertEqual(self.harness.execute(), 1)
        summary = self.harness.summary()
        self.assertEqual(support.FakeCodex.threads, [])
        self.assertIn('integrity', json.dumps(summary).lower())

    def test_preparation_fallback_honors_trusted_pending_correction_stop(self):
        self.assertEqual(self.harness.execute(), 0)
        output = self.harness.output
        receipt = json.loads((output / '.arc/engineering/preflight.json').read_text())
        receipt.update(script_correction_pending=True, script_corrections_used=1)
        main.atomic_json(output / '.arc/engineering/preflight.json', receipt)
        main.save_checkpoint(output, ['REQ-1'], receipt['identity'], {'status': 'passed'})
        with patch.object(main, 'prepare_engineering', side_effect=OSError('aux prep failure')), \
                patch.object(main, 'Codex', side_effect=AssertionError('pending forbids client')) as client:
            modules = main.load_root_modules(self.harness.requirements)
            summary = main.implement_modules(self.harness.requirements, output, modules,
                output / '.codex/skills', receipt['identity'], ['REQ-1'])
        self.assertEqual(summary['status'], 'blocked')
        self.assertEqual(client.call_count, 0)
        self.assertIn('script_correction_pending', json.dumps(summary))

    def test_checkpoint_candidate_rejects_foreign_identity_and_non_prefix_ids(self):
        self.assertEqual(self.harness.execute(), 0)
        latest_path = self.harness.output / '.arc/codex-recovery/latest.json'
        latest = json.loads(latest_path.read_text())
        identity = latest['identity']
        modules = [item.node_id for item in main.load_root_modules(self.harness.requirements)]
        self.assertTrue(main._checkpoint_candidate(self.harness.output, identity, modules))
        self.assertFalse(main._checkpoint_candidate(self.harness.output, {**identity, 'model': 'foreign'}, modules))
        latest['completed_ids'] = [modules[1]]
        main.atomic_json(latest_path, latest)
        self.assertFalse(main._checkpoint_candidate(self.harness.output, identity, modules))

    def test_missing_latest_never_rebuilds_empty_checkpoint(self):
        self.assertEqual(self.harness.execute(), 0)
        recovery = self.harness.output / '.arc/codex-recovery'
        latest = recovery / 'latest.json'
        latest.unlink()
        self.assertFalse(main._checkpoint_candidate(self.harness.output,
                                                    {'model': 'foreign'},
                                                    ['REQ-1', 'REQ-2']))
        self.assertFalse((recovery / 'checkpoints' / 'module-000-rebuilt').exists())

    def test_corrupt_latest_and_manifest_mismatch_are_unavailable(self):
        self.assertEqual(self.harness.execute(), 0)
        recovery = self.harness.output / '.arc/codex-recovery'
        latest_path = recovery / 'latest.json'
        latest = json.loads(latest_path.read_text())
        modules = ['REQ-1', 'REQ-2']
        identity = latest['identity']
        latest_path.write_text('{broken', encoding='utf-8')
        self.assertFalse(main._checkpoint_candidate(self.harness.output, identity, modules))
        main.atomic_json(latest_path, latest)
        manifest_path = recovery / 'checkpoints' / latest['checkpoint'] / 'checkpoint.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['completed_ids'] = ['REQ-1']
        main.atomic_json(manifest_path, manifest)
        self.assertFalse(main._checkpoint_candidate(self.harness.output, identity, modules))

    def test_structurally_corrupt_latest_is_unavailable(self):
        self.assertEqual(self.harness.execute(), 0)
        latest_path = self.harness.output / '.arc/codex-recovery/latest.json'
        latest_path.write_text('[]', encoding='utf-8')
        self.assertFalse(main._checkpoint_candidate(self.harness.output, {'foreign': True}, ['REQ-1', 'REQ-2']))

    def test_script_correction_missing_latest_integrity_stops_without_empty_checkpoint(self):
        self.assertEqual(self.harness.execute(), 0)
        output = self.harness.output
        receipt_path = output / '.arc/engineering/preflight.json'
        receipt = json.loads(receipt_path.read_text())
        (output / '.arc/codex-recovery/latest.json').unlink()
        modules = main.load_root_modules(self.harness.requirements)
        before_threads = len(support.FakeCodex.threads)
        observed = {'status': 'error',
                    'configuration_error': 'ReferenceError: test is not defined at checks.spec.js:1',
                    'evidence_path': 'module-observations/REQ-1/before'}
        with self.assertRaisesRegex(ValueError, 'trusted non-empty checkpoint'):
            main.correct_observation_script(support.FakeCodex(), modules[0], self.harness.requirements,
                                            output, receipt, observed, 'fake', modules)
        self.assertFalse((output / '.arc/codex-recovery/latest.json').exists())
        self.assertEqual(len(support.FakeCodex.threads), before_threads)

    def test_resume_foreign_checkpoint_writes_unavailable_integrity_summary(self):
        self.assertEqual(self.harness.execute(), 0)
        latest_path = self.harness.output / '.arc/codex-recovery/latest.json'
        latest = json.loads(latest_path.read_text())
        latest['identity'] = {**latest['identity'], 'model': 'foreign'}
        main.atomic_json(latest_path, latest)
        resumed = self.harness.root / 'resumed-foreign'
        with patch.object(main, 'Codex', side_effect=AssertionError('foreign checkpoint forbids model')) as client:
            self.assertEqual(self.harness.execute(resumed, self.harness.output), 1)
        client.assert_not_called()
        summary = json.loads((resumed / '.arc/codex-recovery/summary.json').read_text())
        self.assertEqual(summary['delivery']['status'], 'unavailable')
        self.assertEqual(summary['status'], 'blocked')
        self.assertIn('integrity', json.dumps(summary).lower())

    def test_foreign_checkpoint_with_prepared_application_is_unavailable(self):
        self.assertEqual(self.harness.execute(), 0)
        latest_path = self.harness.output / '.arc/codex-recovery/latest.json'
        latest = json.loads(latest_path.read_text())
        latest['identity'] = {**latest['identity'], 'model': 'foreign'}
        main.atomic_json(latest_path, latest)
        with patch.object(main, 'Codex', side_effect=AssertionError('foreign lineage forbids model')) as client:
            self.assertEqual(self.harness.execute(), 1)
        client.assert_not_called()
        self.assertEqual(self.harness.summary()['delivery']['status'], 'unavailable')

    def test_recovery_event_without_latest_is_not_fresh_planning(self):
        self.assertEqual(self.harness.execute(), 0)
        recovery = self.harness.output / '.arc/codex-recovery'
        (recovery / 'latest.json').unlink()
        (recovery / 'events.jsonl').write_text('{"stage":"model"}\n', encoding='utf-8')
        with patch.object(main, 'Codex', side_effect=AssertionError('old lineage forbids model')) as client:
            self.assertEqual(self.harness.execute(), 1)
        client.assert_not_called()
        self.assertEqual(self.harness.summary()['delivery']['status'], 'unavailable')

    def test_manifest_without_preflight_blocks_resume_and_final_delivery(self):
        self.assertEqual(self.harness.execute(), 0)
        recovery = self.harness.output / '.arc/codex-recovery'
        latest = json.loads((recovery / 'latest.json').read_text())
        manifest_path = recovery / 'checkpoints' / latest['checkpoint'] / 'checkpoint.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['engineering_files'].pop('preflight.json', None)
        main.atomic_json(manifest_path, manifest)
        self.assertFalse(main._checkpoint_candidate(self.harness.output, latest['identity'], ['REQ-1', 'REQ-2']))
        with patch.object(main, 'build_frontend', side_effect=AssertionError('integrity stop forbids final build')) as build:
            self.assertEqual(self.harness.execute(), 1)
        build.assert_not_called()
        self.assertEqual(self.harness.summary()['delivery']['status'], 'unavailable')


if __name__ == '__main__':
    unittest.main()
