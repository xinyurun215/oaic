"""Neutral host checks; model boundaries are substitutes, not real-model proof."""
import copy
import hashlib
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import test_recovery as support
main = support.main


class EngineeringFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.requirements = self.root / 'requirements'
        self.requirements.mkdir()
        self.output = self.root / 'output'
        self.output.mkdir()
        self.payload = {'id': 'ROOT', 'description': 'Persist shared records.', 'children': [
            {'id': 'VIEW', 'children': [{'id': 'VIEW-1', 'type': 'ATOMIC',
                'dependencies': ['STORE-1'], 'description': 'Display the stored title after reload.'}]},
            {'id': 'STORE', 'children': [{'id': 'STORE-1', 'type': 'ATOMIC',
                'description': 'Save a record title through the Create form.'}]}]}
        (self.requirements / 'requirements.yaml').write_text(json.dumps(self.payload))

    def engineering(self):
        module = getattr(main, 'engineering', None)
        self.assertIsNotNone(module, 'source engineering host mechanisms have not been implemented')
        return module

    def proposal(self):
        source = self.engineering().source_index(self.requirements)
        return {'source_sha256': source['source_sha256'], 'requirements': [
            {'id': item['id'], 'module_id': item['module_id'],
             'source_refs': [{'path': item['path'] + '/description', 'quote': item['node']['description']}],
             'entry': 'public home', 'preconditions': [{'kind': 'ui', 'text': 'Create a record'}],
             'roles': ['visitor'], 'actions': ['Use the visible form'],
             'observable_results': ['Source-required state'], 'persistence': 'reload',
             'shared_objects': ['record'], 'unknowns': []}
            for item in source['atoms']], 'shared_objects': [
                {'name': 'record', 'owner_module': 'STORE', 'consumer_modules': ['VIEW'],
                 'fields': ['title'], 'unknowns': []}], 'conflicts': []}

class EngineeringTests(EngineeringFixture):
    def test_nullable_implementation_declaration_is_auxiliary_unverified(self):
        engineering = self.engineering()
        engineering.capture_module_state(self.output, 'STORE', {'interfaces': None, 'requirements': None}, self.requirements)
        state = engineering.handoff(self.output, self.requirements)['modules'][0]
        self.assertEqual(state['declaration_status'], 'unverified')

    def test_nullable_claims_do_not_skip_independent_acceptance_inventory(self):
        self.engineering()
        module = main.load_root_modules(self.requirements)[0]
        inventory = main.requirement_inventory(module, main.observation_contract(module),
                                               {'requirements': None}, None, self.output)
        self.assertEqual(inventory['STORE-1']['model_status'], 'not_reported')

    def test_failed_module_restores_its_checkpoint_state_without_replacing_other_records(self):
        engineering = self.engineering()
        (self.output / 'records.js').write_text('title')
        good = {'interfaces': [{'name': 'record', 'definition': 'records.js', 'fields': ['title'], 'consumers': []}]}
        engineering.capture_module_state(self.output, 'STORE', good, self.requirements)
        identity = {'neutral': 1}
        main.save_checkpoint(self.output, ['STORE'], identity, {'status': 'passed'})
        (self.output / 'records.js').write_text('caption')
        engineering.capture_module_state(self.output, 'STORE', {'interfaces': [{'name': 'record', 'definition': 'records.js', 'fields': ['caption'], 'consumers': []}]}, self.requirements)
        engineering.capture_module_state(self.output, 'VIEW', {'requirements': []}, self.requirements)
        result = main.restore_failed_module(self.output, 'STORE', identity, Path(main.__file__).parent)
        self.assertEqual(result['status'], 'restored')
        records = {record['module_id']: record for record in engineering.handoff(self.output, self.requirements)['modules']}
        self.assertEqual(records['STORE']['interfaces'][0]['fields'], ['title'])
        self.assertIn('VIEW', records)

    def test_root_reference_is_legal_and_not_a_missing_module(self):
        engineering = self.engineering()
        self.payload['children'][0]['dependencies'] = ['ROOT']
        ordered, issues = engineering.module_order(self.payload['children'])
        self.assertEqual(issues, [])
        self.assertEqual([node['id'] for node in ordered], ['STORE', 'VIEW'])

    def test_checkpoint_carries_file_bound_handoff_to_separate_resume(self):
        folder = self.output / '.arc/engineering'
        folder.mkdir(parents=True)
        original = {'modules': {'STORE': {'module_id': 'STORE', 'files': {'records.js': 'old-digest'}}}}
        (folder / 'state.json').write_text(json.dumps(original))
        (self.output / 'records.js').write_text('title')
        main.save_checkpoint(self.output, ['STORE'], {'source': 'neutral'}, {'status': 'passed'})
        resumed = self.root / 'resumed'
        resumed.mkdir()
        main.restore_checkpoint(self.output, resumed, {'source': 'neutral'}, self.root)
        self.assertTrue((resumed / '.arc/engineering/state.json').is_file())
        self.assertEqual(json.loads((resumed / '.arc/engineering/state.json').read_text()), original)

    def test_new_host_module_changes_agent_identity(self):
        agent = self.root / 'agent'
        agent.mkdir()
        for name in ('main.py', 'requirements.txt', 'engineering.py'):
            (agent / name).write_text('first')
        original = main.input_identity(self.requirements, agent)
        (agent / 'engineering.py').write_text('changed')
        self.assertNotEqual(main.input_identity(self.requirements, agent)['agent_sha256'], original['agent_sha256'])

    def test_web_search_is_explicitly_disabled_in_runtime_config(self):
        def sdk_config(*, env, config_overrides):
            return {'env': env, 'config_overrides': config_overrides}
        config = main._codex_config(sdk_config)
        self.assertIn('web_search="disabled"', config['config_overrides'])

    def test_dependency_orders_provider_before_consumer(self):
        self.engineering()
        self.assertEqual([m.node_id for m in main.load_root_modules(self.requirements)], ['STORE', 'VIEW'])

    def test_cycle_and_missing_dependency_remain_generatable(self):
        engineering = self.engineering()
        self.payload['children'][1]['children'][0]['dependencies'] = ['VIEW-1', 'MISSING']
        (self.requirements / 'requirements.yaml').write_text(json.dumps(self.payload))
        modules, issues = engineering.module_order(self.payload['children'])
        self.assertEqual({m['id'] for m in modules}, {'STORE', 'VIEW'})
        self.assertEqual({i['kind'] for i in issues}, {'cycle', 'missing_dependency'})

    def test_contract_rejects_forged_quote_or_omission(self):
        engineering = self.engineering()
        proposal = self.proposal()
        engineering.validate_contract(proposal, self.requirements)
        forged = copy.deepcopy(proposal)
        forged['requirements'][0]['source_refs'][0]['quote'] = 'invented mandatory behavior'
        with self.assertRaisesRegex(ValueError, 'quote'):
            engineering.validate_contract(forged, self.requirements)
        proposal['requirements'].pop()
        with self.assertRaisesRegex(ValueError, 'atomic'):
            engineering.validate_contract(proposal, self.requirements)

    def test_contract_ref_must_belong_to_its_atomic_source(self):
        engineering = self.engineering()
        proposal = self.proposal()
        proposal['requirements'][0]['source_refs'] = proposal['requirements'][1]['source_refs']
        with self.assertRaisesRegex(ValueError, 'source'):
            engineering.validate_contract(proposal, self.requirements)

    def test_shared_record_binds_definition_and_consumers_then_goes_stale(self):
        engineering = self.engineering()
        for path in ['backend/records.js', 'frontend/view.js']:
            file = self.output / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text('title')
        declaration = {'requirements': [{'id': 'STORE-1', 'files': ['backend/records.js'], 'unfinished': ''}],
                       'interfaces': [{'name': 'record', 'definition': 'backend/records.js',
                                       'fields': ['title'], 'consumers': ['frontend/view.js']}]}
        engineering.capture_module_state(self.output, 'STORE', declaration, self.requirements)
        state = engineering.handoff(self.output, self.requirements, ['STORE'])
        self.assertEqual(state['modules'][0]['interfaces'][0]['status'], 'current')
        self.assertIn('frontend/view.js', state['modules'][0]['files'])
        (self.output / 'backend/records.js').write_text('caption')
        changed = engineering.handoff(self.output, self.requirements, ['STORE'])['modules'][0]
        self.assertEqual(changed['status'], 'stale')
        self.assertIn('implementation_evidence', changed)
        self.assertIn('caption', json.dumps(changed['implementation_evidence']))
        (self.requirements / 'requirements.yaml').write_text(json.dumps(self.payload) + '\n')
        self.assertEqual(engineering.handoff(self.output, self.requirements, ['STORE'])['modules'][0]['source_status'], 'stale')

    def test_shared_record_rejects_nonexistent_definition(self):
        engineering = self.engineering()
        engineering.capture_module_state(self.output, 'STORE', {'interfaces': [
            {'name': 'record', 'definition': '../outside.js', 'consumers': []}]}, self.requirements)
        state = engineering.handoff(self.output, self.requirements, ['STORE'])
        self.assertEqual(state['modules'][0]['interfaces'][0]['status'], 'invalid')


class PreflightFlowTests(EngineeringFixture):
    def test_consistent_three_file_rewrite_cannot_replace_the_host_frozen_plan(self):
        self.tamper_plan = True
        self.test_planning_precedes_code_and_final_observation_reuses_it()

    def test_nullable_declaration_does_not_interrupt_real_generation_flow(self):
        self.null_declaration = True
        self.test_planning_precedes_code_and_final_observation_reuses_it()

    def test_planning_precedes_code_and_final_observation_reuses_it(self):
        self.engineering()
        events = []
        proposal = self.proposal()
        output = self.output
        class Thread:
            def __init__(self, options): self.options = options
            def run(self, prompt):
                workspace = Path(self.options['cwd'])
                if 'engineering-contract.json' in prompt:
                    events.append('contract')
                    self_assert = workspace / 'requirements/requirements.yaml'
                    assert self_assert.is_file() and not (workspace / 'backend').exists()
                    (workspace / 'engineering-contract.json').write_text(json.dumps(proposal))
                else:
                    identifier = prompt.split(': ')[1].split(' - ')[0]
                    events.append('code:' + identifier)
                    (output / (identifier + '.txt')).write_text('product')
                    if getattr(self_outer, 'null_declaration', False):
                        folder = output / 'module-observations' / identifier
                        (folder / 'implementation.json').write_text('{"interfaces":null,"requirements":null}')
                return types.SimpleNamespace(error=None)
        class Codex:
            def __init__(self, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def login_api_key(self, key): pass
            def thread_start(self, **kwargs): return Thread(kwargs)
        def plan(codex, module, requirements, output, context, model):
            events.append('plan:' + module.node_id)
            return support.fake_plan(codex, module, requirements, output, context, model)
        modules = main.load_root_modules(self.requirements)
        identity = {'neutral': 1}
        self_outer = self
        with patch.object(main, 'Codex', Codex), patch.object(main, 'plan_module_observation', side_effect=plan), \
             patch.object(main, 'build_frontend', return_value={'status':'passed'}), \
             patch.object(main, 'verify_backend_startup', return_value={'status':'passed'}), \
             patch.object(main, 'run_module_observation', side_effect=support.fake_observe_pass), \
             patch.dict('os.environ', {'OPENAI_API_KEY': '', 'MODEL': 'neutral'}):
            result = main.implement_modules(self.requirements, output, modules, output / 'skills', identity, [])
            self.assertEqual(result['status'], 'complete')
            if getattr(self, 'tamper_plan', False):
                receipt_path = output / '.arc/engineering/preflight.json'
                receipt = json.loads(receipt_path.read_text())
                for module in modules:
                    folder = output / 'module-observations' / module.node_id
                    for name in ('checks.spec.js', 'frozen-v1.spec.js'):
                        (folder / name).write_text('// consistently rewritten script')
                    contract = json.loads((folder / 'contract.json').read_text())
                    contract['forged'] = True
                    (folder / 'contract.json').write_text(json.dumps(contract))
                    receipt['observations'][module.node_id].update(
                        script_sha256=hashlib.sha256((folder / 'checks.spec.js').read_bytes()).hexdigest(),
                        contract_sha256=hashlib.sha256((folder / 'contract.json').read_bytes()).hexdigest())
                receipt_path.write_text(json.dumps(receipt))
            acceptance = main.run_final_acceptance(self.requirements, output, modules, output / 'skills', identity)
        self.assertEqual(events, ['plan:STORE', 'code:STORE', 'plan:VIEW', 'code:VIEW'])
        self.assertEqual(acceptance['status'], 'unverified' if getattr(self, 'tamper_plan', False) else 'passed')

    def test_early_failed_planner_is_not_retried_after_code(self):
        self.engineering()
        calls = []
        class Codex:
            def __init__(self, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def login_api_key(self, key): pass
            def thread_start(self, **kwargs):
                return types.SimpleNamespace(run=lambda prompt: (_ for _ in ()).throw(RuntimeError('Quota exceeded')))
        modules = main.load_root_modules(self.requirements)
        with patch.object(main, 'Codex', Codex), \
             patch.object(main, 'plan_module_observation', side_effect=lambda *args: calls.append('plan') or (_ for _ in ()).throw(RuntimeError('Quota exceeded'))):
            receipt = main.prepare_engineering(self.requirements, self.output, modules, {'neutral':1})
            main.prepare_module_observation(Codex(), modules[0], self.requirements, self.output,
                                            main.load_root_context(self.requirements), 'neutral', receipt, modules)
            (self.output / 'product.txt').write_text('preserved')
            acceptance = main.run_final_acceptance(self.requirements, self.output, modules, self.output / 'skills', {'neutral':1})
        self.assertEqual(calls, ['plan'])
        self.assertTrue((self.output / 'product.txt').is_file())
        self.assertEqual(acceptance['status'], 'unverified')


if __name__ == '__main__': unittest.main()
