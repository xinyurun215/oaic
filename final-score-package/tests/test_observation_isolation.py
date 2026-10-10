"""Real process, HTTP and SQLite checks; replace only the npm/Playwright tool boundary."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_recovery import main


SERVER = r'''
import os,sqlite3
from http.server import BaseHTTPRequestHandler,HTTPServer
database=sqlite3.connect(os.environ['ARC_DB_FILE'])
database.execute('create table state(name text)')
database.execute("insert into state values ('Original')")
database.commit()
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  if self.path=='/rename':
   database.execute("update state set name='Renamed'");database.commit()
  self.send_response(200);self.end_headers()
  self.wfile.write(database.execute('select name from state').fetchone()[0].encode())
 def log_message(self,*args):pass
HTTPServer(('127.0.0.1',int(os.environ['PORT'])),Handler).serve_forever()
'''

DRIVER = r'''
import sys,re,json,os,urllib.request
from pathlib import Path
config=Path(sys.argv[sys.argv.index('--config')+1]).read_text()
port=re.search(r'127\.0\.0\.1:(\d+)',config).group(1)
ids=['REQ-1-1','REQ-1-10']
discovery='--list' in sys.argv
if discovery and os.environ.get('PROBE_BAD_DISCOVERY'):
 print('SyntaxError: Unexpected token at checks.spec.js:1',file=sys.stderr)
 sys.exit(1)
if '--grep' in sys.argv:
 pattern=sys.argv[sys.argv.index('--grep')+1]
 ids=[title for title in ids if re.search(pattern,'checks.spec.js '+title)]
if not discovery and os.environ.get('PROBE_EMPTY_RUN'):ids=[]
if not discovery and os.environ.get('PROBE_THROW_RUN'):raise RuntimeError('Probe driver crashed after startup')
specs=[]
for title in ids:
 if discovery:
  results=[];status='expected'
 else:
  value=urllib.request.urlopen('http://127.0.0.1:'+port+('/rename' if title=='REQ-1-1' else '/')).read().decode()
  passed=value==('Renamed' if title=='REQ-1-1' else 'Original')
  if title=='REQ-1-1' and os.environ.get('PROBE_FAIL_CASE'):passed=False
  status='expected' if passed else 'unexpected'
  results=[{'status':'passed' if passed else 'failed','duration':1,'errors':[] if passed else [{'message':'Persistent state inherited from another test: '+value}]}]
 specs.append({'title':title,'tests':[{'status':status,'results':results}]})
Path('result.json').write_text(json.dumps({'errors':[],'suites':[{'specs':specs}]}))
sys.exit(0 if all(s['tests'][0]['status']=='expected' for s in specs) else 1)
'''


class ObservationIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        backend = self.output / 'backend'
        backend.mkdir()
        self.server = backend / 'server.py'
        self.server.write_text(SERVER, encoding='utf-8')
        self.driver = backend / 'driver.py'
        self.driver.write_text(DRIVER, encoding='utf-8')
        folder = self.output / 'module-observations/REQ-1'
        folder.mkdir(parents=True)
        self.script = b'// immutable neutral observation script'
        for name in ('checks.spec.js', 'frozen-v1.spec.js'):
            (folder / name).write_bytes(self.script)
        self.module = main.RequirementModule(1, 1, 'REQ-1', 'neutral', {
            'id': 'REQ-1', 'children': [{'id': title, 'type': 'ATOMIC'}
                                       for title in ('REQ-1-1', 'REQ-1-10')]},
            frozen_script=self.script)
        self.databases = []
        self.services = []

    def run_observation(self):
        real_run, real_popen = subprocess.run, subprocess.Popen

        def replace(command):
            if command[1:] == ['run', 'start']:
                return [sys.executable, str(self.server)]
            if len(command) > 1 and command[1] == 'test':
                return [sys.executable, str(self.driver), *command[2:]]
            return command

        def start(command, **kwargs):
            service = command[1:] == ['run', 'start']
            process = real_popen(replace(command), **kwargs)
            if service:
                self.databases.append(kwargs['env']['ARC_DB_FILE'])
                self.services.append(process)
            return process

        with patch.object(main, 'ensure_project_dependencies', return_value={}), patch.object(
                main.subprocess, 'run', side_effect=lambda command, **kwargs: real_run(replace(command), **kwargs)), patch.object(
                main.subprocess, 'Popen', side_effect=start):
            return main.run_module_observation(self.output, self.module, 'before')

    def test_each_atomic_check_gets_fresh_persistent_state_and_executes_once(self):
        result = self.run_observation()
        self.assertEqual(result['status'], 'passed', result)
        self.assertEqual([test['title'] for test in result['tests']], ['REQ-1-1', 'REQ-1-10'])
        self.assertTrue(all(test['executed'] for test in result['tests']))
        self.assertEqual(len(set(self.databases)), 2)
        self.assertTrue(all(process.poll() is not None for process in self.services))
        self.assertTrue(all(not Path(path).exists() for path in self.databases))
        self.assertEqual((self.output / 'module-observations/REQ-1/checks.spec.js').read_bytes(), self.script)

    def test_zero_selected_execution_is_error_and_services_are_stopped(self):
        with patch.dict(os.environ, {'PROBE_EMPTY_RUN': '1'}):
            result = self.run_observation()
        self.assertEqual(result['status'], 'error')
        self.assertFalse(any(test.get('executed') for test in result['tests']))
        self.assertTrue(all(process.poll() is not None for process in self.services))
        evidence = self.output / result['evidence_path']
        self.assertTrue((evidence / 'stderr.txt').is_file())

    def test_runtime_guard_is_on_runner_copy_and_original_bytes_are_preserved(self):
        result = self.run_observation()
        self.assertEqual(result['status'], 'passed', result)
        for run in result['atomic_results']:
            evidence = self.output / run['evidence_path']
            self.assertEqual((evidence / 'checks.spec.js').read_bytes(), self.script)
            runner = (evidence / 'runner-checks.spec.js').read_bytes()
            self.assertTrue(runner.startswith(self.script))
            self.assertIn(b"require('./arc-step-guard.cjs')", runner)
            self.assertIn('ARC_OBSERVATION_UNFINISHED_STEP',
                          (evidence / 'arc-step-guard.cjs').read_text())
        for name in ('checks.spec.js', 'frozen-v1.spec.js'):
            self.assertEqual((self.output / 'module-observations/REQ-1' / name).read_bytes(), self.script)

    def test_executed_failure_is_preserved_with_other_atomic_pass(self):
        with patch.dict(os.environ, {'PROBE_FAIL_CASE': '1'}):
            result = self.run_observation()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual([test['status'] for test in result['tests']], ['unexpected', 'expected'])
        self.assertTrue(all(test['executed'] for test in result['tests']))
        self.assertTrue(result['tests'][0]['error'])
        self.assertTrue(all(process.poll() is not None for process in self.services))

    def test_driver_crash_keeps_stderr_and_cleans_each_service_and_database(self):
        with patch.dict(os.environ, {'PROBE_THROW_RUN': '1'}):
            result = self.run_observation()
        self.assertEqual(result['status'], 'error')
        self.assertEqual(len(result['atomic_results']), 2)
        self.assertTrue(all(process.poll() is not None for process in self.services))
        self.assertTrue(all(not Path(path).exists() for path in self.databases))
        text = (self.output / result['evidence_path'] / 'stderr.txt').read_text(encoding='utf-8')
        self.assertIn('Probe driver crashed after startup', text)
        self.assertIn('service stopped: True', text)

    def test_discovery_syntax_error_reaches_correction_evidence(self):
        with patch.dict(os.environ, {'PROBE_BAD_DISCOVERY': '1'}):
            result = self.run_observation()
        self.assertEqual(result['status'], 'error')
        self.assertIn('SyntaxError:', result['configuration_error'])
        self.assertIn('checks.spec.js', result['configuration_error'])


if __name__ == '__main__':
    unittest.main()
