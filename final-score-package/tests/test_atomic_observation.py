"""Focused source-bound observer tests, without model or browser dependencies."""
import json
import tempfile
import types
import unittest
from pathlib import Path

from test_recovery import main


def module():
    return main.RequirementModule(1, 1, 'feature', 'Neutral feature', {
        'id': 'feature', 'children': [
            {'id': identifier, 'type': 'ATOMIC', 'description': 'A source behavior'}
            for identifier in ('alpha', 'beta: value', 'third / item')]})


def report(statuses, titles=None):
    titles = titles or ['alpha', 'beta: value', 'third / item']
    return {'errors': [], 'suites': [{'suites': [{'specs': [
        {'title': title, 'tests': [{'status': 'expected' if status == 'passed' else
                                  ('skipped' if status == 'skipped' else 'unexpected'),
                                 'results': [{'status': status, 'duration': 2, 'errors': []}]}]}
        for title, status in zip(titles, statuses)]}]}]}


class AtomicObservationTests(unittest.TestCase):
    def test_three_independent_results_survive_first_failure(self):
        result = main.parse_observation_report(report(['failed', 'passed', 'passed']), 1)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual([test['executed'] for test in result['tests']], [True] * 3)
        inventory = main.requirement_inventory(module(), main.observation_contract(module()),
                                               None, result, Path('.'))
        self.assertEqual([item['observation_status'] for item in inventory.values()],
                         ['unverified', 'observed_passed', 'observed_passed'])

    def test_structured_discovery_requires_exact_unique_source_ids(self):
        valid = report(['passed'] * 3)
        self.assertEqual(main.validate_discovered_chains(valid, list(module().subtree['children'][i]['id']
                         for i in range(3))), ['alpha', 'beta: value', 'third / item'])
        for titles in (['alpha', 'alpha', 'third / item'], ['alpha', 'beta: value'],
                       ['alpha', 'beta: value', 'extra']):
            with self.subTest(titles=titles), self.assertRaises(ValueError):
                main.validate_discovered_chains(report(['passed'] * len(titles), titles),
                                                ['alpha', 'beta: value', 'third / item'])

    def test_skipped_unexecuted_and_process_timeout_are_errors(self):
        for statuses in (['failed', 'skipped', 'passed'], ['passed', 'skipped', 'passed']):
            self.assertEqual(main.parse_observation_report(report(statuses), 1)['status'], 'error')
        missing = report(['passed'] * 3)
        missing['suites'][0]['suites'][0]['specs'][1]['tests'][0]['results'] = []
        self.assertEqual(main.parse_observation_report(missing, 0)['status'], 'error')
        result = main.parse_observation_report(report(['failed', 'passed', 'passed']), -1,
                                               process_timed_out=True, timeout_seconds=110)
        self.assertEqual(result['status'], 'error')
        self.assertIn('110 seconds', result['error'])

    def test_runtime_script_error_cannot_trigger_application_repair(self):
        invalid = report(['failed', 'passed', 'passed'])
        invalid['suites'][0]['suites'][0]['specs'][0]['tests'][0]['results'][0]['errors'] = [
            {'message': 'ReferenceError: missingHelper is not defined'}]
        result = main.parse_observation_report(invalid, 1)
        self.assertEqual(result['status'], 'error')
        self.assertIn('ReferenceError', result['configuration_error'])

    def test_unfinished_step_guard_overrides_ordinary_locator_failure(self):
        invalid = report(['failed', 'passed', 'passed'])
        invalid['suites'][0]['suites'][0]['specs'][0]['tests'][0]['results'][0]['errors'] = [
            {'message': 'Error: expect(locator).toHaveText failed'},
            {'message': 'Error: ARC_OBSERVATION_UNFINISHED_STEP: 1 test.step promise(s) still pending'}]
        result = main.parse_observation_report(invalid, 1)
        self.assertEqual(result['status'], 'error')
        self.assertIn('ARC_OBSERVATION_UNFINISHED_STEP', result['tests'][0]['configuration_error'])
        inventory = main.requirement_inventory(module(), main.observation_contract(module()),
                                               None, result, Path('.'))
        self.assertEqual([item['observation_status'] for item in inventory.values()],
                         ['unverified', 'observed_passed', 'observed_passed'])

    def test_clear_http_document_error_is_product_failure_but_test_origin_is_unverified(self):
        invalid = report(['failed', 'passed', 'passed'])
        errors = invalid['suites'][0]['suites'][0]['specs'][0]['tests'][0]['results'][0]['errors']
        errors.append({'location': {'column': 62, 'file': 'http://127.0.0.1:55277/', 'line': 22},
            'message': 'ReferenceError: app-origin reference fault\n'
                       '    at document.querySelector.onclick (http://127.0.0.1:55277/:22:62)'})
        result = main.parse_observation_report(invalid, 1)
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('configuration_error', result['tests'][0])
        for location, message in (
                ('checks.spec.js', errors[0]['message']),
                ('checks.spec.js', 'SyntaxError: wrong test at checks.spec.js:1'),
                ('http://127.0.0.1:55277/', 'ReferenceError: test local failure\n'
                 '    at checks.spec.js:1:1\n    at document.querySelector.onclick '
                 '(http://127.0.0.1:55277/:22:62)'),
                ('http://127.0.0.1:55277/', 'ReferenceError: page.evaluate: local evaluation failed\n'
                 '    at document.querySelector.onclick (http://127.0.0.1:55277/:22:62)')):
            with self.subTest(message=message):
                errors[:] = [{'location': {'file': location}, 'message': message}]
                self.assertEqual(main.parse_observation_report(invalid, 1)['status'], 'error')

    def test_exception_words_in_assertion_values_remain_business_failure(self):
        invalid = report(['failed', 'passed', 'passed'])
        for text in ('TypeError: source value', 'ReferenceError: source value',
                     'SyntaxError: source value', 'browserType.launch: source value'):
            invalid['suites'][0]['suites'][0]['specs'][0]['tests'][0]['results'][0]['errors'] = [
                {'message': f'Error: expect(received).toBe(expected) failed\nExpected: "{text}"\nReceived: "different"'}]
            with self.subTest(text=text):
                result = main.parse_observation_report(invalid, 1)
                self.assertEqual(result['status'], 'failed')
                self.assertNotIn('configuration_error', result)

    def test_local_script_error_preserves_passes_but_global_errors_do_not(self):
        invalid = report(['failed', 'passed', 'passed'])
        invalid['suites'][0]['suites'][0]['specs'][0]['tests'][0]['results'][0]['errors'] = [
            {'message': 'ReferenceError: missingHelper is not defined'}]
        result = main.parse_observation_report(invalid, 1)
        main.validate_discovered_chains(invalid, ['alpha', 'beta: value', 'third / item'])
        inventory = main.requirement_inventory(module(), main.observation_contract(module()),
                                               None, result, Path('.'))
        self.assertEqual(result['status'], 'error')
        self.assertEqual([item['observation_status'] for item in inventory.values()],
                         ['unverified', 'observed_passed', 'observed_passed'])
        for kind in ('timeout', 'report', 'discovery'):
            with self.subTest(kind=kind):
                global_report = json.loads(json.dumps(invalid))
                if kind == 'report': global_report['errors'] = [{'message': 'Worker stopped'}]
                global_result = main.parse_observation_report(global_report, -1,
                                                               process_timed_out=kind == 'timeout')
                if kind == 'discovery':
                    global_result.update(configuration_error='Wrong source ID set', configuration_error_scope='global')
                inventory = main.requirement_inventory(module(), main.observation_contract(module()),
                                                       None, global_result, Path('.'))
                self.assertTrue(all(item['observation_status'] == 'unverified' for item in inventory.values()))

    def test_planner_writes_only_script_and_host_owns_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requirements = root / 'requirements'
            requirements.mkdir()
            (requirements / 'requirements.yaml').write_text(json.dumps({
                'id': 'ROOT', 'children': [module().subtree]}))
            script = b"const {test}=require('@playwright/test');\ntest('alpha',async()=>{});"

            class Planner:
                def run(self, prompt):
                    (workspace / 'checks.spec.js').write_bytes(script)
                    return types.SimpleNamespace(error=None)

            class Codex:
                def thread_start(self, **kwargs):
                    nonlocal workspace
                    workspace = Path(kwargs['cwd'])
                    return Planner()

            workspace = None
            contract, frozen = main.plan_module_observation(Codex(), module(), requirements,
                                                           root / 'output', {'id': 'ROOT'}, None)
            self.assertEqual(frozen, script)
            self.assertEqual([item['title'] for item in contract['chains']],
                             ['alpha', 'beta: value', 'third / item'])
            self.assertEqual([item['requirement_ids'] for item in contract['chains']],
                             [['alpha'], ['beta: value'], ['third / item']])
            self.assertEqual(json.loads((root / 'output/module-observations/feature/contract.json').read_text()),
                             contract)
