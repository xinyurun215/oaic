"""Real host/file/CLI behavior; only model and browser boundaries are substitutes."""
import hashlib
import json
import os
import re
import tempfile
import types
import unittest
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch

from test_engineering_handoff import EngineeringFixture
import test_recovery as support

main = support.main
COMPACT = 'remote compaction v2 expected exactly one compaction output item, got 0 from 2 output items'


class SourceScopeTests(EngineeringFixture):
    def test_explicit_dependency_quote_is_accepted_with_own_quote(self):
        proposal = self.proposal()
        proposal['requirements'][0]['source_refs'].append(proposal['requirements'][1]['source_refs'][0])
        checked = main.engineering.validate_contract(proposal, self.requirements)
        self.assertEqual(len(checked['requirements'][0]['source_refs']), 2)

    def test_ancestor_dependency_and_transitive_dependency_are_allowed(self):
        self.payload['children'][0]['children'][0].pop('dependencies')
        self.payload['children'][0]['dependencies'] = ['STORE-1']
        self.payload['children'][1]['children'][0]['dependencies'] = ['EXTRA-1']
        self.payload['children'].append({'id': 'EXTRA', 'children': [
            {'id': 'EXTRA-1', 'type': 'ATOMIC', 'description': 'A source-owned prerequisite.'}]})
        (self.requirements / 'requirements.yaml').write_text(json.dumps(self.payload))
        proposal = self.proposal()
        proposal['requirements'][0]['source_refs'].extend([
            proposal['requirements'][1]['source_refs'][0], proposal['requirements'][2]['source_refs'][0]])
        self.assertEqual(main.engineering.validate_contract(proposal, self.requirements)['version'], 1)

    def test_unrelated_quote_and_dependency_only_quote_are_rejected(self):
        proposal = self.proposal()
        proposal['requirements'][1]['source_refs'].append(proposal['requirements'][0]['source_refs'][0])
        with self.assertRaisesRegex(ValueError, 'another atomic source'):
            main.engineering.validate_contract(proposal, self.requirements)
        proposal = self.proposal()
        proposal['requirements'][0]['source_refs'] = proposal['requirements'][1]['source_refs']
        with self.assertRaisesRegex(ValueError, 'own atomic'):
            main.engineering.validate_contract(proposal, self.requirements)

    def test_cycle_and_missing_dependency_do_not_expand_scope(self):
        for dependency, message in [('VIEW-1', 'cycle'), ('ABSENT', 'missing')]:
            with self.subTest(dependency=dependency):
                self.payload['children'][1]['children'][0]['dependencies'] = [dependency]
                (self.requirements / 'requirements.yaml').write_text(json.dumps(self.payload))
                with self.assertRaisesRegex(ValueError, message):
                    main.engineering.validate_contract(self.proposal(), self.requirements)

    def test_same_owner_ancestor_dependency_is_not_a_self_cycle(self):
        self.payload['children'][0]['dependencies'] = ['VIEW-1']
        self.payload['children'][0]['children'][0].pop('dependencies')
        (self.requirements / 'requirements.yaml').write_text(json.dumps(self.payload))
        self.assertEqual(main.engineering.module_order(self.payload['children'])[1], [])
        self.assertEqual(main.engineering.validate_contract(self.proposal(), self.requirements)['version'], 1)

    def test_direct_atomic_self_dependency_is_still_rejected(self):
        self.payload['children'][0]['children'][0]['dependencies'] = ['VIEW-1']
        (self.requirements / 'requirements.yaml').write_text(json.dumps(self.payload))
        with self.assertRaisesRegex(ValueError, 'cycle'):
            main.engineering.validate_contract(self.proposal(), self.requirements)

    def test_ancestor_sibling_prerequisites_do_not_create_atomic_reverse_edges(self):
        self.payload['children'][0]['dependencies'] = ['VIEW-1', 'VIEW-2']
        self.payload['children'][0]['children'][0].pop('dependencies')
        self.payload['children'][0]['children'].append({'id': 'VIEW-2', 'type': 'ATOMIC',
            'description': 'Show the sibling state without an explicit atomic dependency.'})
        (self.requirements / 'requirements.yaml').write_text(json.dumps(self.payload))
        self.assertEqual(main.engineering.module_order(self.payload['children'])[1], [])
        proposal = self.proposal()
        proposal['requirements'][0]['source_refs'].append(proposal['requirements'][1]['source_refs'][0])
        self.assertEqual(main.engineering.validate_contract(proposal, self.requirements)['version'], 1)

    def test_real_cross_module_ordering_cycle_remains_rejected(self):
        self.payload['children'][0]['dependencies'] = ['STORE-1']
        self.payload['children'][0]['children'][0].pop('dependencies')
        self.payload['children'][1]['children'][0]['dependencies'] = ['VIEW-1']
        (self.requirements / 'requirements.yaml').write_text(json.dumps(self.payload))
        self.assertEqual(main.engineering.module_order(self.payload['children'])[1][0]['kind'], 'cycle')
        with self.assertRaisesRegex(ValueError, 'cycle'):
            main.engineering.validate_contract(self.proposal(), self.requirements)

    def test_atomic_dag_is_legal_despite_coarse_module_order_cycle(self):
        self.payload['children'][0]['children'].append({'id': 'VIEW-2', 'type': 'ATOMIC',
            'description': 'Independent visible state.'})
        self.payload['children'][1]['children'].append({'id': 'STORE-2', 'type': 'ATOMIC',
            'dependencies': ['VIEW-2'], 'description': 'Consume independent visible state.'})
        (self.requirements / 'requirements.yaml').write_text(json.dumps(self.payload))
        self.assertEqual(main.engineering.module_order(self.payload['children'])[1][0]['kind'], 'cycle')
        self.assertEqual(main.engineering.validate_contract(self.proposal(), self.requirements)['version'], 1)

    def test_forged_digest_pointer_and_noncontiguous_quote_still_fail(self):
        for mutation in ('digest', 'pointer', 'quote'):
            proposal = self.proposal()
            if mutation == 'digest': proposal['source_sha256'] = 'forged'
            elif mutation == 'pointer': proposal['requirements'][0]['source_refs'][0]['path'] += '/missing'
            else: proposal['requirements'][0]['source_refs'][0]['quote'] = 'Display title reload.'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                main.engineering.validate_contract(proposal, self.requirements)

    def test_source_preparation_keeps_valid_atomic_dag_module_cycle_without_model(self):
        self.payload['children'][0]['children'].append({'id': 'VIEW-2', 'type': 'ATOMIC',
            'description': 'Independent visible state.'})
        self.payload['children'][1]['children'].append({'id': 'STORE-2', 'type': 'ATOMIC',
            'dependencies': ['VIEW-2'], 'description': 'Consume independent visible state.'})
        (self.requirements / 'requirements.yaml').write_text(json.dumps(self.payload))
        modules = main.load_root_modules(self.requirements)
        with patch.object(main, 'Codex', side_effect=AssertionError('no source model turn')):
            receipt = main.prepare_engineering(self.requirements, self.output, modules, {})
        self.assertTrue(receipt['finished'])
        self.assertEqual(receipt['dependency_issues'][0]['kind'], 'cycle')
        self.assertFalse((self.output / '.arc/engineering/contract.json').exists())


class BoundedRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.requirements = self.root / 'requirements'
        self.requirements.mkdir()
        self.output = self.root / 'delivery'
        self.output.mkdir()
        self.actions = []
        self.calls = []
        self.observed = []
        self.counts = {'A': 9, 'B': 1, 'PASSED': 1}
        for project in ('frontend', 'backend'):
            folder = self.output / project
            folder.mkdir()
            (folder / 'node_modules').mkdir()
            package = {'name': project, 'version': '1.0.0', 'scripts': {
                'build': 'node build.cjs', 'start': 'node server.cjs'}}
            (folder / 'package.json').write_text(json.dumps(package))
        (self.output / 'frontend/build.cjs').write_text(
            "const fs=require('fs'); fs.mkdirSync('dist',{recursive:true}); fs.writeFileSync('dist/index.html','built');")
        (self.output / 'backend/server.cjs').write_text(
            "require('http').createServer((req,res)=>res.end('ready')).listen(process.env.PORT,'127.0.0.1');")
        owner = self
        class Client:
            def __init__(self, **_kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *_args): pass
            def thread_start(self, **kwargs):
                def run(prompt):
                    match = re.search(r'```json\s*(.*?)\s*```', prompt, re.S)
                    failures = json.loads(match.group(1))
                    ids = [item['atomic_id'] for item in failures]
                    owner.calls.append(ids)
                    owner.assertEqual(Path(kwargs['cwd']), owner.output)
                    owner.assertLessEqual(len(ids), 8)
                    action = owner.actions.pop(0) if owner.actions else 'success'
                    if action not in ('success', 'broken-build'):
                        (owner.output / 'unfinished.txt').write_text('failed draft')
                        raise RuntimeError(action)
                    for identifier in ids:
                        (owner.output / (identifier + '.txt')).write_text('repaired')
                    if action == 'broken-build':
                        (owner.output / 'frontend/build.cjs').write_text("throw Error('broken repair');")
                    return types.SimpleNamespace(error=None, status='completed')
                return types.SimpleNamespace(run=run)
        for name, value in [('Codex', Client), ('plan_module_observation', self.plan),
                            ('run_module_observation', self.observe)]:
            patched = patch.object(main, name, value)
            patched.start()
            self.addCleanup(patched.stop)
        environment = patch.dict(os.environ, {'OPENAI_API_KEY': '', 'npm_config_offline': 'true',
                                              'npm_config_audit': 'false', 'npm_config_fund': 'false'})
        environment.start()
        self.addCleanup(environment.stop)

    def plan(self, _client, module, *_args):
        folder = self.output / 'module-observations' / module.node_id
        folder.mkdir(parents=True)
        script = ('// immutable ' + module.node_id).encode()
        contract = main.observation_contract(module)
        main.atomic_json(folder / 'contract.json', contract)
        for name in ('checks.spec.js', 'frozen-v1.spec.js'): (folder / name).write_bytes(script)
        return contract, script

    def observe(self, output, module, phase):
        self.observed.append((module.node_id, phase))
        folder = output / 'module-observations' / module.node_id / phase
        folder.mkdir(parents=True, exist_ok=True)
        tests = []
        if module.node_id == 'UNVERIFIED':
            (folder / 'result.json').write_text(json.dumps({'error': 'Required preparation unavailable'}))
            return {'status': 'error', 'tests': [], 'duration_seconds': 0,
                    'configuration_error': 'Required preparation unavailable',
                    'evidence_path': folder.relative_to(output).as_posix()}
        for identifier in module.chain_titles:
            passed = module.node_id == 'PASSED' or (output / (identifier + '.txt')).is_file()
            tests.append({'title': identifier, 'status': 'expected' if passed else 'unexpected',
                'executed': True, 'result_status': 'passed' if passed else 'failed', 'error': '' if passed else 'Missing state'})
        (folder / 'result.json').write_text(json.dumps(tests))
        return {'status': 'passed' if all(t['result_status'] == 'passed' for t in tests) else 'failed',
                'tests': tests, 'duration_seconds': 0, 'evidence_path': folder.relative_to(output).as_posix()}

    def execute(self, denied_status=None):
        payload = {'id': 'ROOT', 'description': 'Neutral contract', 'children': [
            {'id': module_id, 'children': [{'id': f'{module_id}-{i}', 'type': 'ATOMIC',
                'description': 'Create the required visible state.'} for i in range(1, count + 1)]}
            for module_id, count in self.counts.items()]}
        (self.requirements / 'requirements.yaml').write_text(json.dumps(payload))
        modules = main.load_root_modules(self.requirements)
        if denied_status is not None:
            identity = {'fixture': 1}
            receipt = {'version': main.engineering.VERSION, 'identity': identity,
                'source_sha256': main.engineering.source_index(self.requirements)['source_sha256'],
                'observations': {}}
            for module in modules:
                folder = self.output / 'module-observations' / module.node_id
                if module.node_id == 'DENIED':
                    folder.mkdir(parents=True)
                    # Source-linked frozen receipt; the error's only status signal is its field.
                    receipt['observations'][module.node_id] = {'status': 'unverified', 'reason': {
                        'stage': 'planning', 'error': {'type': 'PermissionError', 'message': 'Access denied',
                            'status_code': denied_status, 'codex_error_info': 'unavailable',
                            'request_id': 'unavailable', 'retry_count': 'unavailable',
                            'retry_exhausted': 'unavailable'}}}
                    (folder / 'checks.spec.js').write_bytes(b'// unavailable')
                    (folder / 'frozen-v1.spec.js').write_bytes(b'// unavailable')
                else:
                    self.plan(None, module)
                    receipt['observations'][module.node_id] = {'status': 'prepared',
                        'script_sha256': hashlib.sha256((folder / 'checks.spec.js').read_bytes()).hexdigest(),
                        'contract_sha256': hashlib.sha256((folder / 'contract.json').read_bytes()).hexdigest()}
            modules = [replace(module, planning_authority=receipt) for module in modules]
        outcome = main.run_final_acceptance(self.requirements, self.output, modules, self.root / 'skills', {'fixture': 1})
        for module in modules:
            if module.node_id == 'DENIED': continue
            folder = self.output / 'module-observations' / module.node_id
            self.assertEqual((folder / 'checks.spec.js').read_bytes(), ('// immutable ' + module.node_id).encode())
            self.assertEqual((folder / 'frozen-v1.spec.js').read_bytes(), ('// immutable ' + module.node_id).encode())
        return outcome

    def test_successful_batches_survive_later_compaction_and_one_fresh_recovery(self):
        self.actions = ['success', 'success', COMPACT, 'success']
        outcome = self.execute()
        self.assertEqual(self.calls, [[f'A-{i}' for i in range(1, 9)], ['A-9'], ['B-1'], ['B-1']])
        self.assertEqual(outcome['status'], 'passed')
        self.assertEqual(outcome['repair']['recovery_attempts'], 1)
        self.assertTrue(outcome['repair']['kept'])
        self.assertFalse((self.output / 'unfinished.txt').exists())
        self.assertIn(('PASSED', 'after'), self.observed)
        self.assertEqual(outcome['chain_counts'], {'passed': 11, 'failed': 0, 'unverified': 0})

    def test_repeated_compaction_stops_and_replays_earlier_successes(self):
        self.actions = ['success', COMPACT, COMPACT]
        outcome = self.execute()
        self.assertEqual(len(self.calls), 4)
        self.assertTrue((self.output / 'A-1.txt').is_file())
        self.assertFalse((self.output / 'A-9.txt').exists())
        self.assertFalse((self.output / 'unfinished.txt').exists())
        self.assertEqual(outcome['chain_counts'], {'passed': 10, 'failed': 1, 'unverified': 0})
        self.assertEqual(outcome['repair']['unprocessed_atoms'], [])
        self.assertEqual(outcome['repair']['remaining_failed_atoms'], ['A-9'])
        self.assertIn(('PASSED', 'after'), self.observed)
        drafts = list((self.output / '.arc/codex-recovery/repair-drafts').rglob('unfinished.txt'))
        self.assertEqual(len(drafts), 2)

    def test_quota_denial_stops_without_recovery_and_keeps_earlier_batch(self):
        self.actions = ['success', 'Quota exceeded. Check billing details.']
        outcome = self.execute()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(outcome['repair']['recovery_attempts'], 0)
        self.assertTrue((self.output / 'A-1.txt').is_file())
        self.assertEqual(outcome['chain_counts']['passed'], 9)
        self.assertIn('blocked_reason', outcome['repair'])

    def test_normal_batch_limit_leaves_tail_unprocessed(self):
        self.counts = {'A': 65, 'PASSED': 1}
        outcome = self.execute()
        self.assertEqual(len(self.calls), 8)
        self.assertEqual(outcome['repair']['unprocessed_atoms'], ['A-65'])
        self.assertEqual(outcome['chain_counts'], {'passed': 65, 'failed': 1, 'unverified': 0})

    def test_connection_failure_does_not_get_compaction_recovery(self):
        self.actions = ['success', 'Connection reset by peer']
        outcome = self.execute()
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(outcome['repair']['recovery_attempts'], 0)
        self.assertEqual(outcome['chain_counts']['passed'], 11)

    def test_transient_failure_retries_once_then_keeps_prefix_and_continues(self):
        self.actions = ['success', 'Connection reset by peer', 'Connection reset by peer', 'success']
        outcome = self.execute()
        self.assertEqual(self.calls, [[f'A-{i}' for i in range(1, 9)], ['A-9'], ['A-9'], ['B-1']])
        self.assertTrue((self.output / 'A-1.txt').is_file())
        self.assertTrue((self.output / 'B-1.txt').is_file())
        self.assertEqual(outcome['repair']['batches'][1]['attempts'], 2)
        self.assertEqual(outcome['repair']['batches'][1]['status'], 'interrupted')
        self.assertEqual(outcome['repair']['batches'][2]['status'], 'runtime_valid')

    def test_successful_fresh_recovery_cannot_repeat_on_later_batch(self):
        self.actions = [COMPACT, 'success', COMPACT]
        outcome = self.execute()
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(outcome['repair']['recovery_attempts'], 1)
        self.assertEqual(outcome['chain_counts']['passed'], 10)

    def test_build_rejection_restores_only_current_batch(self):
        self.actions = ['success', 'broken-build']
        outcome = self.execute()
        self.assertEqual(len(self.calls), 3)
        self.assertTrue((self.output / 'A-1.txt').is_file())
        self.assertFalse((self.output / 'A-9.txt').exists())
        self.assertEqual(outcome['chain_counts']['passed'], 10)
        self.assertEqual(outcome['repair']['recovery_attempts'], 0)
        self.assertEqual(outcome['technical_failure']['stage'], 'observation_repair_build')

    def test_compaction_message_with_permission_denial_never_recovers(self):
        self.actions = ['HTTP 403 Forbidden: ' + COMPACT]
        outcome = self.execute()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(outcome['repair']['recovery_attempts'], 0)
        self.assertIn('blocked_reason', outcome['repair'])
        self.assertEqual(outcome['repair']['unprocessed_atoms'], ['A-9', 'B-1'])

    def test_handoff_preserves_observer_reason_for_unverified_atom(self):
        self.counts = {'A': 1, 'UNVERIFIED': 1, 'PASSED': 1}
        outcome = self.execute()
        handoff = json.loads((self.output / '.arc/codex-recovery/repair-handoff.json').read_text())
        self.assertFalse(any(item['id'] == 'UNVERIFIED-1' for item in handoff['prior']['atoms']))
        self.assertEqual(outcome['repair']['unverified_atoms'], ['UNVERIFIED-1'])

    def test_trusted_early_status_denial_prevents_any_repair_thread(self):
        self.counts = {'A': 1, 'DENIED': 1, 'PASSED': 1}
        (self.output / 'existing-valid.txt').write_text('keep prior valid source')
        outcome = self.execute(denied_status='403')
        self.assertEqual(self.calls, [])
        self.assertFalse(outcome['repair']['attempted'])
        self.assertEqual(outcome['repair']['recovery_attempts'], 0)
        self.assertEqual((self.output / 'existing-valid.txt').read_text(), 'keep prior valid source')
        self.assertEqual(outcome['chain_counts']['passed'], 1)
        self.assertEqual(outcome['repair']['blocked_reason']['error']['status_code'], '403')


if __name__ == '__main__':
    unittest.main()
