"""Real SDK checks: run this file alone in a fresh, credential-free process.

Recovery fault tests install an SDK substitute; a combined discovery must not
present that substitute as successful real-client verification.
"""

from importlib.metadata import version
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import openai_codex
from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox

import main


LOCAL_SERVER = r'''
import json, sys
log, status = sys.argv[1:]
for line in sys.stdin:
    request = json.loads(line)
    with open(log, 'a') as stream:
        stream.write(json.dumps(request) + '\n')
    method = request['method']
    if method == 'initialized':
        continue
    if method == 'initialize':
        result = {'userAgent': 'codex/0.157.1', 'serverInfo': {'name': 'codex', 'version': '0.157.1'}}
    elif method == 'account/login/start':
        result = {'type': 'apiKey'}
    elif method == 'thread/start':
        parameters = request['params']
        result = {'approvalPolicy': 'never', 'approvalsReviewer': 'user',
            'cwd': parameters['cwd'], 'model': parameters['model'], 'modelProvider': 'openai',
            'sandbox': {'type': 'dangerFullAccess'}, 'thread': {'id': 'offline-thread',
            'sessionId': 'offline-session', 'cliVersion': '0.157.1', 'createdAt': 1,
            'updatedAt': 1, 'cwd': parameters['cwd'], 'ephemeral': True, 'preview': '',
            'modelProvider': 'openai', 'source': 'appServer', 'status': {'type': 'idle'}, 'turns': []}}
    elif method == 'turn/start':
        result = {'turn': {'id': 'offline-turn', 'items': [], 'status': 'inProgress'}}
    else:
        raise RuntimeError('unexpected offline method: ' + method)
    print(json.dumps({'id': request['id'], 'result': result}), flush=True)
    if method == 'turn/start':
        turn = {'id': 'offline-turn', 'items': [], 'status': status}
        if status == 'failed':
            turn['error'] = {'message': 'Quota exceeded. Check billing details.'}
        print(json.dumps({'method': 'turn/completed',
            'params': {'threadId': 'offline-thread', 'turn': turn}}), flush=True)
'''


@contextmanager
def closing_client(config):
    client = Codex(config)
    process = client._client._proc
    try:
        yield client
    finally:
        client.close()
        # SDK close does not always reap its kill fallback or close output pipes.
        # Keep test resource cleanup outside production code.
        process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()


@unittest.skipIf(not getattr(openai_codex, '__file__', None),
                 'Run SDK compatibility separately; recovery tests loaded an SDK substitute')
class SdkCompatibilityTests(unittest.TestCase):
    def test_real_exports_and_matching_installed_runtime(self):
        pin = next(line for line in (Path(main.__file__).parent / 'requirements.txt').read_text().splitlines()
                   if line.startswith('openai-codex==')).split('==')[1]
        self.assertEqual(version('openai-codex'), pin)
        self.assertEqual(version('openai-codex-cli-bin'), pin)
        self.assertEqual(ApprovalMode.deny_all.value, 'deny_all')
        self.assertIsNotNone(Sandbox.full_access)
        self.assertTrue(callable(Codex.thread_start))
        self.assertTrue(callable(Codex.login_api_key))
        self.assertEqual(CodexConfig(env={}, config_overrides=()).config_overrides, ())

    def test_bundled_app_server_handshake_without_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            clean = {'PATH': os.defpath, 'HOME': directory}
            with patch.dict(os.environ, clean, clear=True):
                with closing_client(CodexConfig(env={}, config_overrides=(), cwd=directory)) as client:
                    self.assertEqual(client.metadata.serverInfo.version.split()[0],
                                     version('openai-codex-cli-bin'))
                    self.assertTrue(client.metadata.userAgent)
                    print('HANDSHAKE', client.metadata.model_dump_json())
            self.assertFalse((Path(directory) / '.codex' / 'auth.json').exists())

    def test_local_protocol_parameters_and_host_turn_statuses(self):
        for status in ('completed', 'failed', 'interrupted', 'inProgress'):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                log = Path(directory) / 'protocol.jsonl'
                clean = {'PATH': os.defpath, 'HOME': directory}
                config = CodexConfig(cwd=directory, env={}, launch_args_override=(
                    sys.executable, '-u', '-c', LOCAL_SERVER, str(log), status))
                with patch.dict(os.environ, clean, clear=True), closing_client(config) as client:
                    client.login_api_key('offline-test-placeholder')
                    thread = client.thread_start(cwd=directory, model='offline-model',
                        sandbox=Sandbox.full_access, approval_mode=ApprovalMode.deny_all,
                        developer_instructions='Neutral offline protocol test')
                    if status == 'completed':
                        main._completed_turn(thread, 'neutral offline input')
                    elif status == 'failed':
                        with self.assertRaisesRegex(RuntimeError, 'Quota exceeded'):
                            main._completed_turn(thread, 'neutral offline input')
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'Codex turn did not complete'):
                            main._completed_turn(thread, 'neutral offline input')
                messages = [json.loads(line) for line in log.read_text().splitlines()]
                print('LOCAL_PROTOCOL', status, json.dumps(messages))
                self.assertEqual([message['method'] for message in messages],
                    ['initialize', 'initialized', 'account/login/start', 'thread/start', 'turn/start'])
                start = messages[3]['params']
                self.assertEqual(start['approvalPolicy'], 'never')
                self.assertEqual(start['sandbox'], 'danger-full-access')
                self.assertEqual(start['cwd'], directory)
                self.assertEqual(start['model'], 'offline-model')
                self.assertEqual(start['developerInstructions'], 'Neutral offline protocol test')
                self.assertEqual(messages[4]['params']['input'][0]['text'], 'neutral offline input')

    def test_dependency_upgrade_rejects_old_checkpoint_and_restores_new(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requirements = root / 'input'
            requirements.mkdir()
            (requirements / 'requirements.yaml').write_text('id: ROOT\nname: Neutral input\n')
            agent = root / 'agent'
            agent.mkdir()
            (agent / 'main.py').write_bytes((Path(main.__file__)).read_bytes())
            for name in ('template', 'skills', 'arcbench-agent-runtime'):
                (agent / name).mkdir()
            dependency_file = agent / 'requirements.txt'
            current = (Path(main.__file__).parent / 'requirements.txt').read_text()
            dependency_file.write_text(current.replace('openai-codex==0.157.1', 'openai-codex==0.155.1'))
            old_identity = main.input_identity(requirements, agent)
            old = root / 'old'
            old.mkdir()
            (old / 'neutral.txt').write_text('saved neutral content')
            main.save_checkpoint(old, ['REQ-1'], old_identity, {'status': 'passed'})
            old_pointer = (old / '.arc/codex-recovery/latest.json').read_bytes()
            dependency_file.write_text(current)
            identity = main.input_identity(requirements, agent)
            self.assertNotEqual(identity, old_identity)
            rejected = root / 'rejected'
            rejected.mkdir()
            with self.assertRaisesRegex(ValueError, 'configuration differs'):
                main.restore_checkpoint(old, rejected, identity, agent)
            self.assertEqual((old / '.arc/codex-recovery/latest.json').read_bytes(), old_pointer)
            self.assertEqual(list(rejected.iterdir()), [])
            new = root / 'new'
            new.mkdir()
            (new / 'neutral.txt').write_text('new neutral content')
            main.save_checkpoint(new, ['REQ-1'], identity, {'status': 'passed'})
            restored = root / 'restored'
            restored.mkdir()
            self.assertEqual(main.restore_checkpoint(new, restored, identity, agent), ['REQ-1'])
            self.assertEqual((restored / 'neutral.txt').read_text(), 'new neutral content')


if __name__ == '__main__':
    unittest.main()
