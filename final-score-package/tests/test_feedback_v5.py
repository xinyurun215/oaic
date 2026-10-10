"""Neutral feedback-flow controls with real files and substituted model calls."""

import hashlib
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_recovery import main


class ScriptCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.requirements = self.root / "requirements"
        self.requirements.mkdir()
        (self.requirements / "requirements.yaml").write_text(json.dumps({"id": "ROOT", "children": [
            {"id": "REQ-1", "children": [{"id": "REQ-1-1", "type": "ATOMIC",
                                            "description": "Show the correct value."}]}]}))
        self.output = self.root / "output"
        self.folder = self.output / "module-observations/REQ-1"
        self.folder.mkdir(parents=True)
        (self.output / 'backend').mkdir()
        (self.output / 'backend/app.py').write_bytes(b'original product')
        self.original = b"test('REQ-1-1', async ({page}) => {\n  expect(1).toBe(1);\n});\n"
        (self.folder / "checks.spec.js").write_bytes(self.original)
        (self.folder / "frozen-v1.spec.js").write_bytes(self.original)
        self.module = main.load_root_modules(self.requirements)[0]
        contract = main.observation_contract(self.module)
        main.atomic_json(self.folder / "contract.json", contract)
        source = main.engineering.source_index(self.requirements)
        self.identity = {"neutral": 1}
        self.receipt = {"version": main.engineering.VERSION, "identity": self.identity,
                        "source_sha256": source["source_sha256"], "finished": True,
                        "observations": {"REQ-1": {"status": "prepared",
                            "script_sha256": hashlib.sha256(self.original).hexdigest(),
                            "contract_sha256": hashlib.sha256((self.folder / "contract.json").read_bytes()).hexdigest()}}}
        main.atomic_json(self.output / ".arc/engineering/preflight.json", self.receipt)
        self.modules = [self.module]

    def observation(self, message="ReferenceError: test is not defined at checks.spec.js:1"):
        return {"status": "error", "configuration_error_scope": "test", "configuration_error": message,
                "tests": [{"title": "REQ-1-1", "status": "unexpected", "executed": True,
                           "result_status": "failed", "error": message,
                           "configuration_error": message}], "evidence_path": "module-observations/REQ-1/early"}

    def test_correction_preserves_original_and_binds_v2_to_authority(self):
        calls = []
        class Client:
            def thread_start(self, **options):
                pending = main.engineering.checkpoint_authority(self_outer.output, self_outer.identity)
                self_outer.assertTrue(pending['script_correction_pending'])
                self_outer.assertEqual(pending['script_corrections_used'], 1)
                self_outer.assertEqual(main.engineering.observation_from_authority(
                    self_outer.output, 'REQ-1', self_outer.identity,
                    self_outer.receipt['source_sha256'], pending)[1], self_outer.original)
                workspace = Path(options["cwd"])
                self_outer.assertFalse((workspace / "frontend").exists())
                self_outer.assertTrue((workspace / "requirements/requirements.yaml").is_file())
                def run(prompt):
                    calls.append(prompt)
                    (workspace / "corrected.spec.js").write_bytes(
                        b"const { test, expect } = require('@playwright/test');\n" + self_outer.original)
                    return types.SimpleNamespace(error=None)
                return types.SimpleNamespace(run=run)
        self_outer = self
        changed = main.correct_observation_script(Client(), self.module, self.requirements, self.output,
                                                   self.receipt, self.observation(), None, self.modules)
        self.assertTrue(changed)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.receipt["script_corrections_used"], 1)
        self.assertEqual((self.folder / "frozen-v1.spec.js").read_bytes(), self.original)
        self.assertTrue((self.folder / "frozen-v2.spec.js").is_file())
        self.assertEqual(self.receipt["observations"]["REQ-1"]["version"], 2)
        self.assertIsNotNone(main.engineering.observation_from_authority(
            self.output, "REQ-1", self.identity, self.receipt["source_sha256"], self.receipt))
        terminal = main.engineering.checkpoint_authority(self.output, self.identity)
        self.assertFalse(terminal.get('script_correction_pending', True))
        self.assertEqual(terminal['script_corrections_used'], 1)
        self.assertEqual(terminal['observations']['REQ-1']['version'], 2)
        self.receipt.update(normal_repairs_used=7, compaction_recovery_used=1)
        main.atomic_json(self.output / '.arc/engineering/preflight.json', self.receipt)
        main.save_checkpoint(self.output, ['REQ-1'], self.identity, {'status': 'passed'})
        trusted = main.engineering.checkpoint_authority(self.output, self.identity)
        resumed = self.root / 'resumed'
        resumed.mkdir()
        self.assertEqual(main.restore_checkpoint(self.output, resumed, self.identity,
                                                Path(main.__file__).parent), ['REQ-1'])
        restored = main.engineering.observation_from_authority(
            resumed, 'REQ-1', self.identity, self.receipt['source_sha256'], trusted)
        self.assertEqual(restored[1], (self.folder / 'frozen-v2.spec.js').read_bytes())
        self.assertEqual(trusted['script_corrections_used'], 1)
        self.assertFalse(trusted.get('script_correction_pending', True))
        self.assertEqual(trusted['normal_repairs_used'], 7)
        self.assertEqual(trusted['compaction_recovery_used'], 1)
        (resumed / 'module-observations/REQ-1/frozen-v2.spec.js').write_bytes(b'corrupt v2')
        self.assertIsNone(main.engineering.observation_from_authority(
            resumed, 'REQ-1', self.identity, self.receipt['source_sha256'], trusted))
        (self.folder / "frozen-v1.spec.js").write_bytes(b"tampered")
        self.assertIsNone(main.engineering.observation_from_authority(
            self.output, "REQ-1", self.identity, self.receipt["source_sha256"], self.receipt))

    def test_pending_receipt_write_failure_stops_before_correction_request(self):
        main.save_checkpoint(self.output, ['REQ-1'], self.identity, {'status': 'passed'})
        original_write = main.atomic_json
        calls = []
        owner = self
        class Client:
            def thread_start(self, **options):
                calls.append('model')
                workspace = Path(options['cwd'])
                def run(prompt):
                    (workspace / 'corrected.spec.js').write_bytes(
                        b"const { test, expect } = require('@playwright/test');\n" + owner.original)
                    return types.SimpleNamespace(error=None)
                return types.SimpleNamespace(run=run)
        def write(path, value):
            if path.name == 'preflight.json' and value.get('script_correction_pending'):
                raise OSError('pending authority write unavailable')
            return original_write(path, value)
        with patch.object(main, 'atomic_json', side_effect=write):
            with self.assertRaisesRegex(OSError, 'pending authority write unavailable'):
                main.correct_observation_script(Client(), self.module, self.requirements, self.output,
                    self.receipt, self.observation(), None, self.modules)
        self.assertEqual(calls, [])
        self.assertTrue(self.receipt['script_correction_pending'])
        self.assertEqual(self.receipt['script_corrections_used'], 1)
        self.assertTrue(self.modules[0].planning_authority['script_correction_pending'])
        self.assertEqual((self.output / 'backend/app.py').read_bytes(), b'original product')
        self.assertEqual((self.folder / 'checks.spec.js').read_bytes(), self.original)
        self.assertEqual((self.folder / 'frozen-v1.spec.js').read_bytes(), self.original)
        self.assertFalse((self.folder / 'frozen-v2.spec.js').exists())

    def test_stale_checkpoint_authority_stops_before_correction_request(self):
        main.save_checkpoint(self.output, ['REQ-1'], self.identity, {'status': 'passed'})
        calls = []
        owner = self
        class Client:
            def thread_start(self, **options):
                calls.append('model')
                workspace = Path(options['cwd'])
                def run(prompt):
                    (workspace / 'corrected.spec.js').write_bytes(
                        b"const { test, expect } = require('@playwright/test');\n" + owner.original)
                    return types.SimpleNamespace(error=None)
                return types.SimpleNamespace(run=run)
        with patch.object(main, 'save_checkpoint', return_value=None):
            with self.assertRaisesRegex(ValueError, 'pending authority'):
                main.correct_observation_script(Client(), self.module, self.requirements, self.output,
                    self.receipt, self.observation(), None, self.modules)
        self.assertEqual(calls, [])
        self.assertTrue(self.receipt['script_correction_pending'])
        self.assertEqual(self.receipt['script_corrections_used'], 1)
        self.assertEqual((self.folder / 'frozen-v1.spec.js').read_bytes(), self.original)

    def test_locator_failure_and_unverified_prerequisite_do_not_use_correction_turn(self):
        class Client:
            def thread_start(self, **_):
                self_outer.fail("locator failure must not call the correction model")
        self_outer = self
        for message in ("TimeoutError: button not found", "expect(locator).toHaveText failed",
                        "TypeError: product failed at checks.spec.js:1",
                        "ReferenceError: businessValue is not defined at checks.spec.js:1",
                        "page.evaluate: ReferenceError: app is not defined at checks.spec.js:1"):
            self.assertFalse(main.correct_observation_script(Client(), self.module, self.requirements,
                             self.output, self.receipt, self.observation(message), None, self.modules))
        self.assertFalse(main.correct_observation_script(Client(), self.module, self.requirements,
                         self.output, self.receipt, {"status": "error", "tests": []}, None, self.modules))
        self.assertEqual(self.receipt.get("script_corrections_used", 0), 0)

    def test_browser_origin_reference_error_with_spec_frame_never_corrects_script(self):
        class Client:
            def thread_start(self, **_):
                self_outer.fail("browser-origin error must not call the correction model")
        self_outer = self
        error = ("ReferenceError: test is not defined\n"
                 "    at document.querySelector.onclick (http://127.0.0.1:3000/:22:62)\n"
                 "    at checks.spec.js:5:13")
        self.assertFalse(main.correct_observation_script(Client(), self.module, self.requirements,
                         self.output, self.receipt, self.observation(error), None, self.modules))
        self.assertEqual(self.receipt.get('script_corrections_used', 0), 0)

    def test_correction_rejects_and_restores_product_and_original_script_mutation(self):
        app = self.output / 'backend/app.py'
        app.write_bytes(b'original product')
        self_outer = self
        class Client:
            def thread_start(self, **options):
                workspace = Path(options['cwd'])
                def run(prompt):
                    app.write_bytes(b'forbidden product mutation')
                    (app.parent / 'added.py').write_bytes(b'forbidden added product')
                    (self_outer.folder / 'frozen-v1.spec.js').write_bytes(b'forbidden original mutation')
                    (workspace / 'corrected.spec.js').write_bytes(
                        b"const { test, expect } = require('@playwright/test');\n" + self_outer.original)
                    return types.SimpleNamespace(error=None)
                return types.SimpleNamespace(run=run)
        with self.assertRaisesRegex(ValueError, 'outside correction workspace'):
            main.correct_observation_script(Client(), self.module, self.requirements, self.output,
                                            self.receipt, self.observation(), None, self.modules)
        self.assertEqual(app.read_bytes(), b'original product')
        self.assertFalse((app.parent / 'added.py').exists())
        self.assertEqual((self.folder / 'frozen-v1.spec.js').read_bytes(), self.original)
        self.assertEqual((self.folder / 'checks.spec.js').read_bytes(), self.original)
        self.assertFalse((self.folder / 'frozen-v2.spec.js').exists())

        terminal = main.engineering.checkpoint_authority(self.output, self.identity)
        self.assertFalse(terminal['script_correction_pending'])
        self.assertEqual(terminal['script_corrections_used'], 1)
        self.assertEqual(self.receipt['script_corrections_used'], 1)
        self.assertEqual(self.receipt['observations']['REQ-1'].get('version', 1), 1)
        self.assertEqual(self.modules[0].planning_authority['script_corrections_used'], 1)

    def test_correction_rejects_changed_multiline_expectation(self):
        self.original = b"test('REQ-1-1', async () => {\n  expect(1)\n    .toBe(1);\n});\n"
        for name in ('checks.spec.js', 'frozen-v1.spec.js'):
            (self.folder / name).write_bytes(self.original)
        self.receipt['observations']['REQ-1']['script_sha256'] = hashlib.sha256(self.original).hexdigest()
        self_outer = self
        class Client:
            def thread_start(self, **options):
                workspace = Path(options['cwd'])
                def run(prompt):
                    (workspace / 'corrected.spec.js').write_bytes(
                        b"const { test, expect } = require('@playwright/test');\n" +
                        self_outer.original.replace(b'.toBe(1)', b'.toBe(2)'))
                    return types.SimpleNamespace(error=None)
                return types.SimpleNamespace(run=run)
        with self.assertRaisesRegex(ValueError, 'source assertions and actions'):
            main.correct_observation_script(Client(), self.module, self.requirements, self.output,
                                            self.receipt, self.observation(), None, self.modules)
        self.assertEqual((self.folder / 'frozen-v1.spec.js').read_bytes(), self.original)
        self.assertFalse((self.folder / 'frozen-v2.spec.js').exists())

    def test_failed_correction_restores_mutation_and_keeps_consumed_allowance(self):
        main.save_checkpoint(self.output, ['REQ-1'], self.identity, {'status': 'passed'})
        self_outer = self
        calls = []
        class Client:
            def thread_start(self, **options):
                def run(prompt):
                    calls.append(prompt)
                    (self_outer.output / 'backend/app.py').write_bytes(b'forbidden failed draft')
                    return types.SimpleNamespace(error=types.SimpleNamespace(message='Forbidden', status_code=403))
                return types.SimpleNamespace(run=run)
        with self.assertRaises(main.ModuleResultError) as raised:
            main.correct_observation_script(Client(), self.module, self.requirements, self.output,
                                            self.receipt, self.observation(), None, self.modules)
        self.assertEqual(main.model_failure_kind(raised.exception.error), 'blocked')
        self.assertEqual((self.output / 'backend/app.py').read_bytes(), b'original product')
        self.assertEqual(self.modules[0].planning_authority['script_corrections_used'], 1)
        trusted = main.engineering.checkpoint_authority(self.output, self.identity)
        self.assertEqual(trusted['script_corrections_used'], 1)
        self.assertFalse(main.correct_observation_script(Client(), self.module, self.requirements, self.output,
                         self.receipt, self.observation(), None, self.modules))
        self.assertEqual(len(calls), 1)

    def test_native_pending_marker_allows_only_await_prefix_insertions(self):
        self.original = (b"const { test, expect } = require('@playwright/test');\n"
                         b"test('REQ-1-1', async ({page}) => {\n"
                         b"  test.step('pending', async () => {\n"
                         b"    await expect(page.getByText('required value')).toBeVisible();\n"
                         b"  });\n});\n")
        for name in ('checks.spec.js', 'frozen-v1.spec.js'):
            (self.folder / name).write_bytes(self.original)
        self.receipt['observations']['REQ-1']['script_sha256'] = hashlib.sha256(self.original).hexdigest()
        self_outer = self
        calls = []
        class Client:
            def thread_start(self, **options):
                workspace = Path(options['cwd'])
                def run(prompt):
                    calls.append(prompt)
                    (workspace / 'corrected.spec.js').write_bytes(
                        self_outer.original.replace(b'  test.step(', b'  await test.step('))
                    return types.SimpleNamespace(error=None)
                return types.SimpleNamespace(run=run)
        observed = self.observation('Error: expect(locator).toBeVisible failed; '
                                   'Error: ARC_OBSERVATION_UNFINISHED_STEP: 1 test.step promise(s) pending at test end')
        self.assertTrue(main.correct_observation_script(Client(), self.module, self.requirements, self.output,
                        self.receipt, observed, None, self.modules))
        self.assertEqual(len(calls), 1)
        self.assertEqual((self.folder / 'frozen-v1.spec.js').read_bytes(), self.original)
        self.assertEqual((self.folder / 'frozen-v2.spec.js').read_bytes(),
                         self.original.replace(b'  test.step(', b'  await test.step('))
        self.assertFalse(main.correct_observation_script(Client(), self.module, self.requirements, self.output,
                         self.receipt, observed, None, self.modules))

    def test_application_marker_and_aggregate_only_marker_cannot_spend_correction(self):
        message = ('Error: ARC_OBSERVATION_UNFINISHED_STEP: application error value\n'
                   '    at document.querySelector.onclick (http://127.0.0.1:55277/:22:62)')
        report = {'errors': [], 'suites': [{'specs': [{'title': 'REQ-1-1', 'tests': [{
            'status': 'unexpected', 'results': [{'status': 'failed', 'errors': [{
                'location': {'file': 'http://127.0.0.1:55277/', 'line': 22, 'column': 62},
                'message': message}]}]}]}]}]}
        observed = main.parse_observation_report(report, 1)
        self.assertEqual(observed['status'], 'failed')
        self.assertNotIn('configuration_error', observed)
        class Client:
            def thread_start(self, **options):
                raise AssertionError('application/aggregate marker cannot authorize correction')
        for invalid in (observed, dict(self.observation(message), tests=[]),
                        dict(self.observation(message), tests=[{'error_origin': 'application',
                            'executed': True, 'result_status': 'failed', 'configuration_error': message}])):
            self.assertFalse(main.correct_observation_script(Client(), self.module, self.requirements,
                             self.output, self.receipt, invalid, None, self.modules))
        self.assertEqual(self.receipt.get('script_corrections_used', 0), 0)

    def test_failure_report_error_preserves_first_sdk_forbidden(self):
        original = types.SimpleNamespace(message='Forbidden', status_code=403)
        class Client:
            def thread_start(self, **options):
                return types.SimpleNamespace(run=lambda prompt: types.SimpleNamespace(error=original))
        original_write = main.atomic_json
        def write(path, data):
            if path.name == 'correction-failure.json':
                raise OSError('injected failure report write error')
            return original_write(path, data)
        with patch.object(main, 'atomic_json', side_effect=write):
            with self.assertRaises(main.ModuleResultError) as raised:
                main.correct_observation_script(Client(), self.module, self.requirements, self.output,
                                                self.receipt, self.observation(), None, self.modules)
        self.assertIs(raised.exception.error, original)
        self.assertEqual(main.model_failure_kind(raised.exception.error), 'blocked')
        self.assertEqual(self.receipt['script_corrections_used'], 1)

    def test_temporary_cleanup_error_preserves_first_sdk_forbidden(self):
        original = types.SimpleNamespace(message='Forbidden', status_code=403)
        class Client:
            def thread_start(self, **options):
                return types.SimpleNamespace(run=lambda prompt: types.SimpleNamespace(error=original))
        real_cleanup = main.shutil.rmtree
        retained = []
        def cleanup(path, *args, **kwargs):
            if Path(path).name.startswith('arc-observation-correction-'):
                retained.append(Path(path))
                raise OSError('injected temporary workspace cleanup error')
            return real_cleanup(path, *args, **kwargs)
        with patch.object(main.shutil, 'rmtree', side_effect=cleanup):
            with self.assertRaises(main.ModuleResultError) as raised:
                main.correct_observation_script(Client(), self.module, self.requirements, self.output,
                                                self.receipt, self.observation(), None, self.modules)
        for path in retained:
            if path.exists():
                self.addCleanup(real_cleanup, path)
        self.assertIs(raised.exception.error, original)
        self.assertEqual(main.model_failure_kind(raised.exception.error), 'blocked')

    def test_stop_checkpoint_export_error_preserves_first_sdk_and_retains_snapshot(self):
        main.save_checkpoint(self.output, ['REQ-1'], self.identity, {'status': 'passed'})
        original = types.SimpleNamespace(message='Forbidden', status_code=403)
        requested = []
        class Client:
            def thread_start(self, **options):
                def run(prompt):
                    requested.append(prompt)
                    return types.SimpleNamespace(error=original)
                return types.SimpleNamespace(run=run)
        original_write = main.atomic_json
        def write(path, data):
            if requested and path.name == 'latest.json':
                raise OSError('STOP checkpoint export unavailable')
            return original_write(path, data)
        with patch.object(main, 'atomic_json', side_effect=write):
            with self.assertRaises(main.ModuleResultError) as raised:
                main.correct_observation_script(Client(), self.module, self.requirements, self.output,
                                                self.receipt, self.observation(), None, self.modules)
        self.assertIs(raised.exception.error, original)
        self.assertEqual(main.model_failure_kind(raised.exception.error), 'blocked')
        detail = raised.exception.host_integrity_stop
        self.assertIn('terminal state could not be checkpointed', detail['message'])
        snapshot = Path(detail['snapshot_path'])
        self.assertTrue(snapshot.is_dir())
        self.assertEqual((snapshot / 'backend/app.py').read_bytes(), b'original product')
        self.assertEqual(self.receipt['script_corrections_used'], 1)
        self.assertEqual(self.receipt['model_blocked_reason']['error']['status_code'], '403')
        pending = main.engineering.checkpoint_authority(self.output, self.identity)
        self.assertTrue(pending['script_correction_pending'])
        self.assertEqual(pending['script_corrections_used'], 1)
        self.assertTrue(snapshot.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()))
        self.addCleanup(main.shutil.rmtree, snapshot)


if __name__ == "__main__":
    unittest.main()
