"""V4 host behavior: real reports/checkpoints; model and browser boundaries are offline doubles."""
import contextlib
import io
import json
import os
import re
import unittest
from pathlib import Path
from unittest.mock import patch

import test_recovery as support

main = support.main
REAL_PLAN_MODULE_OBSERVATION = main.plan_module_observation


class PartialV4Tests(unittest.TestCase):
    def setUp(self):
        self.harness = support.ModuleObservationFlowTests('test_first_pass_needs_no_repair')
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        self.output = self.harness.output

    def generation(self):
        return json.loads((self.output / 'module-observations/generation.json').read_text())

    def fail_second(self, message='HTTP 403; token=secret-example'):
        support.FakeCodex.failure = 'REQ-2'
        support.FakeCodex.failure_message = message

    def test_failed_module_exports_redacted_error_and_full_atomic_delivery_status(self):
        payload = json.loads((self.harness.requirements / 'requirements.yaml').read_text())
        payload['children'].append({'id': 'REQ-3', 'children': [
            {'id': 'REQ-3-1', 'type': 'ATOMIC', 'description': 'Later work'}]})
        (self.harness.requirements / 'requirements.yaml').write_text(json.dumps(payload))
        self.fail_second()
        self.harness.observe(['passed'])
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(self.harness.execute(), 0)
        report = self.generation()
        by_id = {item['module_id']: item for item in report['modules']}
        self.assertEqual(list(by_id), ['REQ-1', 'REQ-2', 'REQ-3'])
        self.assertEqual(by_id['REQ-1']['generation_status'], 'implemented')
        self.assertEqual(by_id['REQ-2']['generation_status'], 'blocked')
        self.assertEqual(by_id['REQ-3']['generation_status'], 'unattempted')
        self.assertEqual(by_id['REQ-2']['failure']['error']['status_code'], '403')
        self.assertEqual(by_id['REQ-2']['failure']['recovery']['status'], 'restored')
        self.assertTrue(by_id['REQ-2']['failure']['recovery']['failed_source'])
        self.assertEqual(by_id['REQ-2']['requirements']['REQ-2-1']['observation_status'], 'unverified')
        self.assertGreaterEqual(report['unverified_count'], 3)
        self.assertIn('HTTP 403', stdout.getvalue())
        self.assertNotIn('secret-example', json.dumps(report) + stdout.getvalue())

    def test_event_writer_failure_does_not_prevent_checkpointed_generation(self):
        self.harness.observe(['passed', 'passed'])
        original = Path.open
        def write_error(path, *args, **kwargs):
            if path.name == 'events.jsonl':
                raise OSError('event sink failed token=secret-example')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', write_error), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.harness.execute(), 0)
        self.assertEqual(self.harness.summary()['completed_ids'], ['REQ-1', 'REQ-2'])

    def test_generation_report_writer_failure_keeps_partial_candidate_and_stdout_error(self):
        self.fail_second()
        self.harness.observe(['passed'])
        original = main.atomic_json
        def write_error(path, payload):
            if path.name == 'generation.json':
                raise OSError('public report sink failed token=secret-example')
            return original(path, payload)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(main, 'atomic_json', side_effect=write_error), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(self.harness.execute(), 0)
        self.assertIn('public report sink failed', stderr.getvalue())
        self.assertIn('HTTP 403', stdout.getvalue())
        self.assertNotIn('secret-example', stderr.getvalue() + stdout.getvalue())
        self.assertEqual((self.output / 'REQ-1.txt').read_text(), 'REQ-1')

    def test_dirty_transient_retry_restores_checkpoint_and_retains_original_draft(self):
        original = support.FakeThread.run
        failed = False
        def dirty_turn(thread, prompt):
            nonlocal failed
            identifier = re.search(r'Implement ROOT module \d+/\d+: (\S+)', prompt).group(1)
            if identifier == 'REQ-2' and not failed:
                failed = True
                (thread.output / 'REQ-1.txt').write_text('damaged earlier module')
                (thread.output / 'unfinished.txt').write_text('failed draft')
                (thread.output / '.env.failed').write_text('sensitive-secret')
                raise RuntimeError('Connection reset by peer; token=secret-example')
            if identifier == 'REQ-2':
                self.assertEqual((thread.output / 'REQ-1.txt').read_text(), 'REQ-1')
                self.assertFalse((thread.output / 'unfinished.txt').exists())
            return original(thread, prompt)
        self.harness.observe(['passed', 'passed'])
        stdout = io.StringIO()
        with patch.object(support.FakeThread, 'run', dirty_turn), contextlib.redirect_stdout(stdout):
            self.assertEqual(self.harness.execute(), 0)
        self.assertIn('Connection reset by peer', stdout.getvalue())
        self.assertNotIn('secret-example', stdout.getvalue())
        report = self.generation()['modules'][1]
        self.assertEqual(report['attempts'], 2)
        recovery = report['failure_history'][0]['recovery']
        self.assertEqual(recovery['status'], 'restored')
        draft = self.output / recovery['failed_source']
        self.assertEqual((draft / 'unfinished.txt').read_text(), 'failed draft')
        self.assertFalse((draft / '.env.failed').exists())
        self.assertEqual(len(support.FakeCodex.threads), 3)

    def test_unavailable_or_rolled_back_recovery_never_retries_model(self):
        for status in ('unavailable', 'rolled_back', 'indeterminate'):
            with self.subTest(status=status):
                support.FakeCodex.threads = []
                self.fail_second('Connection reset by peer')
                with patch.object(main, 'restore_failed_module', return_value={'status': status}), \
                        patch.object(main, 'run_module_observation', side_effect=support.fake_observe_pass):
                    self.assertEqual(self.harness.execute(self.harness.root / status), 0)
                self.assertEqual(len(support.FakeCodex.threads), 2)

    def test_first_module_transient_without_checkpoint_stops_with_original_draft(self):
        support.FakeCodex.actions = {'REQ-1': ['compact_after_write']}
        self.assertEqual(self.harness.execute(), 1)
        self.assertEqual(len(support.FakeCodex.threads), 1)
        self.assertEqual((self.output / 'unfinished.txt').read_text(), 'current work')
        self.assertEqual(self.generation()['modules'][0]['failure']['recovery']['status'], 'unavailable')

    def test_preparation_structured_forbidden_stops_all_later_planning_and_coding(self):
        error = main.ModuleResultError(type('ForbiddenError', (), {'message': 'Forbidden', 'status_code': 403})())
        with patch.object(main, 'prepare_engineering', side_effect=error), \
                patch.object(main, 'plan_module_observation', side_effect=AssertionError('later planning forbidden')):
            self.assertEqual(self.harness.execute(), 1)
        self.assertEqual(support.FakeCodex.threads, [])
        self.assertEqual(self.generation()['pending_issues'][0]['error']['status_code'], '403')

    def test_observation_planning_quota_stops_coding_after_one_planner(self):
        with patch.object(main, 'plan_module_observation', side_effect=RuntimeError('Quota exceeded')) as planner:
            self.assertEqual(self.harness.execute(), 1)
        self.assertEqual(planner.call_count, 1)
        self.assertEqual(support.FakeCodex.threads, [])

    def test_partial_hard_block_observes_completed_only_without_another_client_or_repair(self):
        self.fail_second()
        calls = self.harness.observe(['failed'])
        instances = []
        class OnceCodex(support.FakeCodex):
            def __init__(self, *args, **kwargs):
                if instances:
                    raise AssertionError('Resource-blocked run must not construct a later model client')
                instances.append(self)
                super().__init__(*args, **kwargs)
        with patch.object(main, 'Codex', OnceCodex):
            self.assertEqual(self.harness.execute(), 0)
        summary = self.harness.summary()
        self.assertEqual(calls, [('REQ-1', 'early'), ('REQ-1', 'early')])
        self.assertEqual(summary['status'], 'blocked')
        self.assertEqual(summary['completed_ids'], [])
        self.assertEqual(self.generation()['unverified_count'], 4)
        self.assertEqual(len(support.FakeCodex.threads), 2)

    def test_offline_scope_ignores_previous_full_pass_and_never_late_plans_missing_frozen_input(self):
        self.harness.observe(['passed', 'passed', 'passed'])
        self.assertEqual(self.harness.execute(), 0)
        root = Path(main.__file__).parent
        identity = main.input_identity(self.harness.requirements, root)
        # The harness environment is part of checkpoint identity.
        with patch.dict(os.environ, {'MODEL': 'fake', 'OPENAI_API_KEY': ''}):
            identity = main.input_identity(self.harness.requirements, root)
        modules = main.load_root_modules(self.harness.requirements)
        before = main.product_source_digest(self.output)
        with patch.object(main, 'Codex', side_effect=AssertionError('offline must not construct Codex')), \
                patch.object(main, 'plan_module_observation', side_effect=AssertionError('no late plan')):
            acceptance = main.run_final_acceptance(self.harness.requirements, self.output, modules,
                self.output / '.codex/skills', identity, completed_ids=['REQ-1'])
        self.assertEqual(acceptance['status'], 'unverified')
        self.assertEqual(acceptance['unverified_count'], 2)
        self.assertEqual(acceptance['unverified_ids'], ['REQ-2-1', 'REQ-2-2'])
        self.assertEqual(main.product_source_digest(self.output), before)
        (self.output / 'module-observations/REQ-1/frozen-v1.spec.js').write_text('// changed')
        with patch.object(main, 'Codex', side_effect=AssertionError('offline must not construct Codex')), \
                patch.object(main, 'plan_module_observation', side_effect=AssertionError('no late plan')):
            acceptance = main.run_final_acceptance(self.harness.requirements, self.output, modules,
                self.output / '.codex/skills', identity, completed_ids=['REQ-1'])
        self.assertEqual(acceptance['unverified_count'], 4)
        self.assertEqual(acceptance['chain_counts']['unverified'], 4)

    def test_acceptance_exception_reports_every_atom_as_unverified(self):
        self.harness.observe(['passed', 'passed'])
        with patch.object(main, 'run_final_acceptance', side_effect=RuntimeError('acceptance unavailable')):
            self.assertEqual(self.harness.execute(), 0)
        acceptance = self.harness.summary()['acceptance']
        self.assertEqual(acceptance['unverified_count'], 4)
        self.assertEqual(acceptance['unverified_ids'], ['REQ-1-1', 'REQ-1-2', 'REQ-2-1', 'REQ-2-2'])

    def test_forbidden_planning_record_writer_failure_cannot_erase_resource_block(self):
        original = main.atomic_json
        def failed_report(path, payload):
            if path.name in ('planning-failure.json', 'preflight.json'):
                raise OSError('planning record sink unavailable')
            return original(path, payload)
        error = main.ModuleResultError(type('ForbiddenError', (), {'message': 'Forbidden', 'status_code': 403})())
        with patch.object(main, 'atomic_json', side_effect=failed_report), \
                patch.object(main, 'prepare_engineering', side_effect=error):
            self.assertEqual(self.harness.execute(), 1)
        self.assertEqual(support.FakeCodex.threads, [])
        self.assertEqual(self.generation()['pending_issues'][0]['error']['status_code'], '403')

    def test_failed_restore_verification_rolls_back_draft_and_stops_retry(self):
        original_turn = support.FakeThread.run
        def dirty_turn(thread, prompt):
            if 'Implement ROOT module 2/2: REQ-2' in prompt:
                (thread.output / 'unfinished.txt').write_text('failed draft')
                raise RuntimeError('Connection reset by peer')
            return original_turn(thread, prompt)
        original_restore = main.restore_repair_snapshot
        calls = []
        def corrupt_restore(snapshot, output):
            result = original_restore(snapshot, output)
            calls.append(snapshot.name)
            if snapshot.name == 'checkpoint-source':
                (output / 'REQ-1.txt').write_text('copy corruption')
            return result
        self.harness.observe(['passed'])
        with patch.object(support.FakeThread, 'run', dirty_turn), \
                patch.object(main, 'restore_repair_snapshot', side_effect=corrupt_restore):
            self.assertEqual(self.harness.execute(), 0)
        self.assertEqual(len(support.FakeCodex.threads), 2)
        failure = self.generation()['modules'][1]['failure']
        self.assertEqual(failure['recovery']['status'], 'rolled_back')
        self.assertEqual((self.output / 'unfinished.txt').read_text(), 'failed draft')
        self.assertEqual((self.output / 'REQ-1.txt').read_text(), 'REQ-1')

    def test_resume_commit_failure_still_exports_and_offline_observes_completed_source(self):
        self.fail_second()
        self.harness.observe(['passed'])
        self.assertEqual(self.harness.execute(), 0)
        resumed = self.harness.root / 'resumed'
        original = main.commit_module
        def denied_commit(runtime, module_id, output, label):
            if module_id == 'recovery-baseline':
                raise RuntimeError('baseline Git denied')
            return original(runtime, module_id, output, label)
        with patch.object(main, 'commit_module', side_effect=denied_commit):
            self.assertEqual(self.harness.execute(resumed, self.output), 0)
        report = json.loads((resumed / 'module-observations/generation.json').read_text())
        self.assertEqual(report['unverified_count'], 2)
        acceptance = json.loads((resumed / 'module-observations/acceptance.json').read_text())
        self.assertEqual(acceptance['mode'], 'observation_only')
        self.assertEqual(acceptance['completed_ids'], ['REQ-1'])
        self.assertEqual(len(support.FakeCodex.threads), 2)

    def planner_export_failure(self, sdk_error):
        calls = []
        class Turn:
            def __init__(self, cwd): self.cwd = Path(cwd)
            def run(self, _prompt):
                (self.cwd / 'checks.spec.js').write_bytes(b'// new independent observation')
                return support.types.SimpleNamespace(error=sdk_error, status='completed')
        class Planner:
            def thread_start(self, **kwargs):
                calls.append(kwargs['cwd'])
                return Turn(kwargs['cwd'])
        original_write = Path.write_bytes
        def failed_export(path, data):
            if path == self.output / 'module-observations/REQ-1/checks.spec.js':
                raise OSError('script export denied token=secret-example')
            return original_write(path, data)
        with patch.object(main, 'plan_module_observation', REAL_PLAN_MODULE_OBSERVATION), \
                patch.object(Path, 'write_bytes', failed_export):
            modules = main.load_root_modules(self.harness.requirements)
            receipt = main.prepare_engineering(self.harness.requirements, self.output, modules, {})
            for module in modules:
                entry = main.prepare_module_observation(Planner(), module, self.harness.requirements,
                    self.output, main.load_root_context(self.harness.requirements), None, receipt, modules)
                if main.model_failure_kind((entry.get('reason') or {}).get('error', {})) == 'blocked':
                    break
        return receipt, calls

    def test_script_export_failure_preserves_structured_forbidden_and_stops_next_observer(self):
        error = support.types.SimpleNamespace(message='Forbidden', status_code=403)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            receipt, calls = self.planner_export_failure(error)
        self.assertEqual(len(calls), 1)
        reason = receipt['observations']['REQ-1']['reason']
        self.assertEqual(reason['error']['status_code'], '403')
        self.assertEqual(reason['error']['message'], 'Forbidden')
        warning = json.loads((self.output / 'module-observations/REQ-1/planning-export-warning.json').read_text())
        self.assertIn('script export denied', warning['error']['message'])
        self.assertIn('script export denied', stderr.getvalue())
        self.assertNotIn('secret-example', json.dumps(warning) + stderr.getvalue())

    def test_successful_turn_export_failure_is_unverified_and_cannot_freeze_old_script(self):
        folder = self.output / 'module-observations/REQ-1'
        folder.mkdir(parents=True)
        (folder / 'checks.spec.js').write_bytes(b'// old observation')
        (folder / 'frozen-v1.spec.js').write_bytes(b'// old observation')
        receipt, calls = self.planner_export_failure(None)
        self.assertEqual(len(calls), 2)
        self.assertEqual(receipt['observations']['REQ-1']['status'], 'unverified')
        self.assertIn('script export denied', receipt['observations']['REQ-1']['reason']['error']['message'])
        self.assertEqual((folder / 'frozen-v1.spec.js').read_bytes(), b'// old observation')
        self.assertNotIn('script_sha256', receipt['observations']['REQ-1'])


if __name__ == '__main__':
    unittest.main()
