"""Opt-in native app-server -> Python runner -> fake CLI integration, fully offline.

Set CLAUDE_NATIVE_E2E_RUNTIME to the newly built codex-app-server executable.
Only the fake process is launched; no Claude login, credentials or model request.
"""
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from manager_core.claude_profiles import render
from manager_core.common import toml_value
from manager_core.external_profile import ExternalProfile
from manager_core.providers import ProviderRegistry
from tests.test_manager_claude_runner import FAKE
from test_shared_editing_headless import Fixture


class Rpc:
    def __init__(self, executable, environment, cwd, adapter=None):
        self.adapter = adapter
        command = [str(executable)] + ([] if 'app-server' in Path(executable).stem else ['app-server'])
        command += ['--listen', 'stdio://']
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=environment, cwd=cwd,
            **({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
        self.messages = queue.Queue()
        self.pending = []
        self.server_requests = []
        self.approval_response = None
        self.next_id = 1
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    self.messages.put(json.loads(line))
                except ValueError:
                    pass
        finally:
            self.messages.put(None)

    def send(self, message):
        if self.adapter:
            message = self.adapter(message)
        self.process.stdin.write(json.dumps(message).encode() + b'\n')
        self.process.stdin.flush()

    def until(self, predicate, timeout=90):
        for index, message in enumerate(self.pending):
            if predicate(message):
                return self.pending.pop(index)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                message = self.messages.get(timeout=max(.01, deadline-time.monotonic()))
            except queue.Empty:
                break
            if message is None:
                raise AssertionError(f'Native app-server exited early with status {self.process.poll()}')
            if 'method' in message and 'id' in message and self.approval_response:
                self.server_requests.append(message)
                self.send({'id':message['id'],'result':self.approval_response(message)})
                continue
            if predicate(message):
                return message
            self.pending.append(message)
        raise AssertionError('Native app-server response timed out; received methods: ' +
                             repr([message.get('method') for message in self.pending[-20:]]))

    def request(self, method, params):
        request_id = self.next_id
        self.next_id += 1
        self.send({'id':request_id, 'method':method, 'params':params})
        response = self.until(lambda message: message.get('id') == request_id and 'method' not in message)
        if 'error' in response:
            raise AssertionError(f'{method} failed: {response["error"]}')
        return response['result']

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.process.stdout.close()


@unittest.skipUnless(os.environ.get('CLAUDE_NATIVE_E2E_RUNTIME'), 'Set CLAUDE_NATIVE_E2E_RUNTIME to the built native app-server.')
class NativeClaudeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='claude-native-offline-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'manager'
        self.root.mkdir()
        self.cwd = self.base / 'trusted-project'
        self.cwd.mkdir()
        (self.cwd / 'AGENTS.md').write_text('AGENTS_PROJECT_MARKER: preserve this project instruction in every agent.\n', encoding='utf-8')
        self.shared = self.base / 'canonical'
        self.shared.mkdir()
        self.rpc, self.profile, self.trace = self.make_claude('a', '안녕하세요 CLAUDE_A_DONE')

    def environment(self, home, writer_id):
        environment = {key:value for key,value in os.environ.items() if not key.startswith(
            ('ANTHROPIC_', 'OPENAI_', 'AZURE_OPENAI_', 'CHATGPT_', 'CODEX_', 'DEEPSEEK_', 'CLAUDE_CODE_'))}
        environment.update(CODEX_HOME=str(home), LOCALAPPDATA=str(self.base / 'local'),
            CODEX_RECORD_HOME=str(self.shared), CODEX_SQLITE_HOME=str(self.shared),
            CODEX_MANAGER_SHARED_EXECUTION='1', CODEX_RECORD_SHARED_APPEND='1',
            CODEX_MANAGER_SHARED_WRITER_ID=writer_id, CLAUDE_FIXTURE_SCENARIO='success')
        return environment

    def connect(self, home, writer_id, binding, environment=None):
        adapter = ExternalProfile({'CODEX_MANAGER_PRIMARY_MODEL':json.dumps(binding)})
        rpc = Rpc(os.environ['CLAUDE_NATIVE_E2E_RUNTIME'], environment or self.environment(home, writer_id),
                  self.cwd, adapter.request)
        rpc.connection_args = (home, writer_id, binding, environment)
        self.addCleanup(rpc.close)
        rpc.request('initialize', {'clientInfo':{'name':'claude-offline-integration','version':'1.0'},
                                   'capabilities':{'experimentalApi':True}})
        rpc.send({'method':'initialized','params':{}})
        return rpc

    def make_claude(self, name, answer, scenario='success', approval='never', handoff_budget_tokens=None,
                    selected_settings=None):
        profile = str(uuid4())
        home = self.base / ('codex-' + name)
        home.mkdir()
        trace = self.base / ('trace-' + name + '.jsonl')
        fake = self.base / ('fake-claude-' + name + '.py')
        source = FAKE.replace('안녕하세요', answer)
        source = source.replace('request = json.loads(sys.stdin.readline())',
            'request = json.loads(sys.stdin.readline())\n' +
            f'with open({str(trace)!r}, "a", encoding="utf-8") as trace:\n' +
            '    instructions = Path(args[args.index("--append-system-prompt-file")+1]).read_text(encoding="utf-8") if "--append-system-prompt-file" in args else ""\n' +
            '    trace.write(json.dumps({"session":session,"resume":"--resume" in args,"request":request,"instructions":instructions,"permission_mode":args[args.index("--permission-mode")+1],"model":args[args.index("--model")+1],"effort":args[args.index("--effort")+1],"autocompact":args[args.index("--autocompact")+1] if "--autocompact" in args else None},ensure_ascii=False)+"\\n")')
        fake.write_text(source, encoding='utf-8')
        wrapper = self.base / ('runner-' + name + '.py')
        scripts = Path(__file__).resolve().parents[1] / 'scripts'
        wrapper.write_text('import sys\n'
            f'sys.path.insert(0, {str(scripts)!r})\n'
            'from manager_core.claude_runner import serve\n'
            f'raise SystemExit(serve({str(self.root)!r}, {profile!r}, '
            f'_test_command=[sys.executable, "-X", "utf8", {str(fake)!r}], '
            '_test_auth={"logged_in":True,"method":"fixture","cli_version":"2.1.282",'
            f'"account_identity":{profile!r}}}, _test_settings={{}}))\n', encoding='utf-8')
        result = render(ProviderRegistry(self.root), home, {'id':profile, 'settings':selected_settings or {}})
        for name, content in result['files'].items():
            target = home / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding='utf-8')
        config = result['files']['config.toml']
        parsed = tomllib.loads(config)
        agent = parsed['model_providers']['claude_code']['agent']
        agent['args'] = ['-X', 'utf8', str(wrapper)]
        if handoff_budget_tokens is not None:
            agent['handoff_budget_tokens'] = handoff_budget_tokens
        config = re.sub(r'^agent = .*$', lambda match: 'agent = ' + toml_value(agent), config, flags=re.MULTILINE)
        config = 'approval_policy = ' + json.dumps(approval) + '\nsandbox_mode = "danger-full-access"\n' + config
        config += '\n[projects.' + json.dumps(str(self.cwd)) + ']\ntrust_level = "trusted"\n'
        (home / 'config.toml').write_text(config, encoding='utf-8')
        environment = self.environment(home, profile)
        environment['CLAUDE_FIXTURE_SCENARIO'] = scenario
        return self.connect(home, profile, result['primary'], environment), profile, trace

    def make_gpt(self, fixture, name='gpt', context_window=200000, compact_limit=None):
        home = self.base / name
        home.mkdir()
        config = ('model="gpt-5.5"\nmodel_provider="fixture"\nmodel_reasoning_effort="medium"\n'
            'model_context_window='+str(context_window)+'\n' +
            ('model_auto_compact_token_limit='+str(compact_limit)+'\n' if compact_limit else '') +
            'cli_auth_credentials_store="ephemeral"\napproval_policy="never"\nsandbox_mode="danger-full-access"\n'
            '[features]\nshell_tool=false\n'
            '[model_providers.fixture]\nname="Loopback fixture"\nbase_url="http://127.0.0.1:'+str(fixture.server.server_port)+'/v1"\n'
            'env_key="LOCAL_FIXTURE_TOKEN"\nwire_api="responses"\n'
            '[projects.'+json.dumps(str(self.cwd))+']\ntrust_level="trusted"\n')
        (home / 'config.toml').write_text(config, encoding='utf-8')
        writer = str(uuid4())
        environment = self.environment(home, writer)
        environment['LOCAL_FIXTURE_TOKEN'] = 'fixture-A'
        return self.connect(home, writer, {'model':'gpt-5.5','model_provider':'fixture','reasoning_effort':'medium'}, environment)

    def turn(self, thread_id, text, rpc=None, extra=None):
        rpc = rpc or self.rpc
        result = rpc.request('turn/start', {'threadId':thread_id, 'input':[{'type':'text','text':text,'text_elements':[]}], **(extra or {})})
        turn_id = result['turn']['id']
        complete = rpc.until(lambda message: message.get('method') == 'turn/completed'
                and message.get('params',{}).get('turn',{}).get('id') == turn_id)
        turn = complete['params']['turn']
        self.assertEqual(turn['status'], 'completed', turn)
        return turn_id

    def test_no_login_wall_catalog_stream_commit_resume_and_shared_readback(self):
        account = self.rpc.request('account/read', {'refreshToken':False})
        self.assertIs(account.get('requiresOpenaiAuth'), False, account)
        catalog = self.rpc.request('model/list', {})
        self.assertTrue(any(model.get('model') == 'cc-opus' or model.get('id') == 'cc-opus'
                            for model in catalog.get('data',[])), catalog)
        started = self.rpc.request('thread/start', {'cwd':str(self.cwd), 'model':'cc-opus',
            'modelProvider':'claude_code', 'approvalPolicy':'never', 'sandbox':'danger-full-access'})
        thread_id = started['thread']['id']
        first = self.turn(thread_id, 'Offline fixture first request.')
        ledger_files = list((self.root / 'work/control-center/claude/sessions').rglob('*.json'))
        self.assertEqual(len(ledger_files), 1)
        first_ledger = json.loads(ledger_files[0].read_text(encoding='utf-8'))
        self.assertFalse(first_ledger['dirty'])
        self.assertIn(first, first_ledger['covered'])
        second = self.turn(thread_id, 'Offline fixture follow-up request.')
        second_ledger = json.loads(ledger_files[0].read_text(encoding='utf-8'))
        self.assertEqual(second_ledger['id'], first_ledger['id'])
        calls = [json.loads(line) for line in self.trace.read_text(encoding='utf-8').splitlines()]
        self.assertTrue(all('AGENTS_PROJECT_MARKER' in call['instructions'] for call in calls))
        self.assertTrue({first, second}.issubset(second_ledger['covered']))
        restored = self.rpc.request('thread/read', {'threadId':thread_id, 'includeTurns':True})
        self.assertIn('안녕하세요', json.dumps(restored, ensure_ascii=False))
        self.assertTrue(any(message.get('method') == 'item/agentMessage/delta' for message in self.rpc.pending))

    def test_ultracode_picker_value_and_native_context_defaults_reach_cli(self):
        client, profile, trace = self.make_claude('ultracode', 'ULTRACODE_DONE', selected_settings={
            'reasoning_effort': 'ultracode', 'context_window': None, 'auto_compact_percent': None})
        catalog = client.request('model/list', {})
        model = next(model for model in catalog['data']
                     if model.get('model') == 'cc-opus' or model.get('id') == 'cc-opus')
        self.assertIn('ultracode', [item['reasoningEffort'] for item in model['supportedReasoningEfforts']])
        thread_id = client.request('thread/start', {'cwd':str(self.cwd)})['thread']['id']
        self.turn(thread_id, 'ULTRACODE_DEFAULT_MARKER: run the configured workflow effort.', client)
        self.turn(thread_id, 'MAX_CHOICE_MARKER: use the separate max effort.', client,
                  extra={'model': 'cc-opus', 'effort': 'max'})
        self.turn(thread_id, 'ULTRACODE_CHOICE_MARKER: select ultracode in this task.', client,
                  extra={'model': 'cc-opus', 'effort': 'ultracode'})
        calls = [json.loads(line) for line in trace.read_text(encoding='utf-8').splitlines()]
        self.assertEqual([call['effort'] for call in calls], ['ultracode', 'max', 'ultracode'])
        self.assertEqual([call['autocompact'] for call in calls], ['auto', 'auto', 'auto'])
        usage = [message['params']['tokenUsage'] for message in client.pending
                 if message.get('method') == 'thread/tokenUsage/updated']
        self.assertTrue(usage, 'The native agent did not report its context window.')
        self.assertTrue(all(item['modelContextWindow'] == 1000000 for item in usage), usage)
        ledgers = list((self.root / 'work/control-center/claude/sessions' / profile).rglob('*.json'))
        self.assertTrue(ledgers)
        for path in ledgers:
            selected = json.loads(path.read_text(encoding='utf-8'))['settings']
            self.assertIsNone(selected['context_window'])
            self.assertIsNone(selected['auto_compact_percent'])

    def test_model_and_effort_switches_survive_sparse_turns_and_desktop_resume_in_same_cli_session(self):
        catalog = self.rpc.request('model/list', {})
        models = {model.get('model') or model['id']: model for model in catalog['data']}
        self.assertTrue({'cc-opus', 'cc-claude-opus-5-5', 'cc-sonnet', 'cc-fable'}.issubset(models), catalog)
        for model_id in ('cc-opus', 'cc-claude-opus-5-5', 'cc-sonnet'):
            efforts = {item['reasoningEffort'] for item in models[model_id]['supportedReasoningEfforts']}
            self.assertTrue({'max', 'ultracode'}.issubset(efforts), models[model_id])

        thread_id = self.rpc.request('thread/start', {'cwd':str(self.cwd)})['thread']['id']
        turns = [self.turn(thread_id, 'SWITCH_OPUS_MARKER: start with the profile default.')]
        turns.append(self.turn(thread_id, 'SWITCH_EXPLICIT_MARKER: select the explicit Opus version.',
                               extra={'model':'cc-claude-opus-5-5', 'effort':'ultracode'}))
        turns.append(self.turn(thread_id, 'SPARSE_EXPLICIT_MARKER: retain the selected model and effort.'))

        self.rpc.request('thread/settings/update', {'threadId':thread_id, 'model':'cc-sonnet', 'effort':'max'})
        turns.append(self.turn(thread_id, 'SETTINGS_SONNET_MARKER: use the saved composer selection.'))
        turns.append(self.turn(thread_id, 'SPARSE_SONNET_MARKER: keep Sonnet and max.'))
        self.rpc.request('thread/settings/update', {'threadId':thread_id, 'effort':'ultracode'})
        turns.append(self.turn(thread_id, 'SETTINGS_EFFORT_MARKER: change only the workflow effort.'))

        # Reopen only this temporary app-server. Match desktop_profile_resume.cjs:
        # same-provider UI resume reads durable metadata and carries its model
        # and effort explicitly, because a provider override disables native
        # automatic restoration. This does not assert bare API resume behavior.
        connection_args = self.rpc.connection_args
        self.rpc.close()
        reopened = self.connect(*connection_args)
        saved = reopened.request('thread/read', {'threadId':thread_id, 'includeTurns':False})['thread']
        self.assertEqual((saved['modelProvider'], saved['model'], saved['reasoningEffort']),
                         ('claude_code', 'cc-sonnet', 'ultracode'))
        resumed = reopened.request('thread/resume', {'threadId':thread_id, 'excludeTurns':True,
            'modelProvider':saved['modelProvider'], 'model':saved['model'],
            'config':{'model_provider':saved['modelProvider'], 'model_reasoning_effort':saved['reasoningEffort']}})
        self.assertEqual(resumed['thread']['id'], thread_id)
        self.assertEqual(resumed['model'], 'cc-sonnet', resumed)
        turns.append(self.turn(thread_id, 'RESTART_SPARSE_MARKER: retain Sonnet and UltraCode.', reopened))
        turns.append(self.turn(thread_id, 'TURN_EFFORT_MARKER: change only effort to max.', reopened,
                               extra={'effort':'max'}))
        turns.append(self.turn(thread_id, 'TURN_SPARSE_MARKER: keep the last model and effort.', reopened))

        calls = [json.loads(line) for line in self.trace.read_text(encoding='utf-8').splitlines()]
        self.assertEqual([(call['model'], call['effort']) for call in calls], [
            ('opus', 'high'),
            ('claude-opus-5-5', 'ultracode'),
            ('claude-opus-5-5', 'ultracode'),
            ('sonnet', 'max'),
            ('sonnet', 'max'),
            ('sonnet', 'ultracode'),
            ('sonnet', 'ultracode'),
            ('sonnet', 'max'),
            ('sonnet', 'max'),
        ])
        self.assertEqual(len({call['session'] for call in calls}), 1, calls)
        self.assertFalse(calls[0]['resume'])
        self.assertTrue(all(call['resume'] for call in calls[1:]))
        ledgers = list((self.root / 'work/control-center/claude/sessions' / self.profile).rglob('*.json'))
        self.assertEqual(len(ledgers), 1)
        ledger = json.loads(ledgers[0].read_text(encoding='utf-8'))
        self.assertEqual(ledger['id'], calls[0]['session'])
        self.assertFalse(ledger['dirty'])
        self.assertTrue(set(turns).issubset(ledger['covered']))

    def test_gpt_to_claude_to_gpt_keeps_shared_context_and_valid_gpt_provider(self):
        fixture = Fixture()
        self.addCleanup(fixture.close)
        gpt = self.make_gpt(fixture)
        started = gpt.request('thread/start', {'cwd':str(self.cwd)})
        thread_id = started['thread']['id']
        self.turn(thread_id, 'GPT_ORIGIN_MARKER: remember the blue widget.', gpt)
        self.rpc.request('thread/resume', {'threadId':thread_id, 'excludeTurns':True})
        self.turn(thread_id, 'CLAUDE_HANDOFF_MARKER: continue the shared task.')
        claude_calls = [json.loads(line) for line in self.trace.read_text(encoding='utf-8').splitlines()]
        first_prompt = json.dumps(claude_calls[0]['request'])
        self.assertIn('GPT_ORIGIN_MARKER', first_prompt)
        self.assertIn('PROFILE_A_DONE', first_prompt)
        self.assertNotIn('<environment_context>', first_prompt)
        self.assertNotIn('You are Codex', first_prompt)
        resumed = gpt.request('thread/resume', {'threadId':thread_id, 'excludeTurns':True})
        self.assertEqual(resumed.get('model'), 'gpt-5.5', resumed)
        self.turn(thread_id, 'GPT_RETURN_MARKER: continue after Claude.', gpt)
        self.assertGreaterEqual(len(fixture.calls), 2)
        self.assertEqual(fixture.calls[-1]['model'], 'gpt-5.5')
        request = json.dumps(fixture.calls[-1]['input'], ensure_ascii=False)
        self.assertIn('CLAUDE_HANDOFF_MARKER', request)
        self.assertIn('CLAUDE_A_DONE', request)
        self.assertIn('GPT_ORIGIN_MARKER', request)

    def test_claude_account_a_b_a_resumes_a_with_only_unseen_b_context(self):
        other, other_profile, other_trace = self.make_claude('b', 'CLAUDE_B_DONE')
        started = self.rpc.request('thread/start', {'cwd':str(self.cwd)})
        thread_id = started['thread']['id']
        first = self.turn(thread_id, 'CLAUDE_A_FIRST_MARKER: keep this original task context.')
        other.request('thread/resume', {'threadId':thread_id, 'excludeTurns':True})
        second = self.turn(thread_id, 'CLAUDE_B_MARKER: add this second-account context.', other)
        self.rpc.request('thread/resume', {'threadId':thread_id, 'excludeTurns':True})
        third = self.turn(thread_id, 'CLAUDE_A_RETURN_MARKER: finish with new shared context.')
        a = [json.loads(line) for line in self.trace.read_text(encoding='utf-8').splitlines()]
        b = [json.loads(line) for line in other_trace.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(a), 2)
        self.assertEqual(a[0]['session'], a[1]['session'])
        self.assertTrue(a[1]['resume'])
        self.assertNotEqual(a[0]['session'], b[0]['session'])
        self.assertIn('CLAUDE_A_FIRST_MARKER', json.dumps(b[0]['request']))
        update = json.dumps(a[1]['request'])
        self.assertIn('CLAUDE_B_MARKER', update)
        self.assertIn('CLAUDE_B_DONE', update)
        self.assertNotIn('CLAUDE_A_FIRST_MARKER', update)
        ledgers = list((self.root / 'work/control-center/claude/sessions' / self.profile).rglob('*.json'))
        self.assertEqual(len(ledgers), 1)
        self.assertTrue({first,second,third}.issubset(json.loads(ledgers[0].read_text(encoding='utf-8'))['covered']))

    def test_gpt_image_history_reaches_claude_as_an_exact_local_reference(self):
        import base64
        import struct
        import zlib
        def chunk(kind, data):
            return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
        image = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
                 + chunk(b'IDAT', zlib.compress(b'\x00\xff\x00\x00')) + chunk(b'IEND', b''))
        url = 'data:image/png;base64,' + base64.b64encode(image).decode()
        fixture = Fixture()
        self.addCleanup(fixture.close)
        gpt = self.make_gpt(fixture)
        thread = gpt.request('thread/start', {'cwd': str(self.cwd)})['thread']['id']
        self.turn(thread, 'remember this screenshot', gpt, extra={'input': [
            {'type': 'text', 'text': 'IMAGE_HISTORY_MARKER', 'text_elements': []},
            {'type': 'image', 'url': url}]})
        self.rpc.request('thread/resume', {'threadId': thread, 'excludeTurns': True})
        self.turn(thread, 'continue with the earlier screenshot')
        prompt = json.dumps(json.loads(self.trace.read_text(encoding='utf-8').splitlines()[0])['request'])
        self.assertIn('IMAGE_HISTORY_MARKER', prompt)
        self.assertIn('Use the Read tool', prompt)
        self.assertNotIn('stored-attachment-', prompt)
        self.assertNotIn('data:image/png', prompt)
        paths = list((self.base / 'codex-a' / 'claude-image-handoff').glob('*.png'))
        self.assertEqual([path.read_bytes() for path in paths], [image])
        original = next((self.shared / 'sessions').rglob('*' + thread + '.jsonl'))
        self.assertIn(url, original.read_text(encoding='utf-8'))

    def test_verified_autocompaction_checkpoint_reduces_other_account_handoff_but_keeps_originals(self):
        fixture = Fixture()
        self.addCleanup(fixture.close)
        request_paths = []
        original_post = fixture.server.RequestHandlerClass.do_POST
        def track_post(handler):
            request_paths.append(handler.path)
            return original_post(handler)
        fixture.server.RequestHandlerClass.do_POST = track_post
        gpt = self.make_gpt(fixture)
        started = gpt.request('thread/start', {'cwd':str(self.cwd)})
        thread_id = started['thread']['id']
        self.turn(thread_id, 'COMPACT_OLD_CONTEXT_MARKER: preserve this original record.\n' + 'historical detail ' * 12000, gpt)
        compactor, _, trace = self.make_claude('compactor', 'CLAUDE_COMPACTOR_DONE', scenario='compact')
        compactor.request('thread/resume', {'threadId':thread_id,'excludeTurns':True})
        self.turn(thread_id, 'COMPACT_CURRENT_MARKER: continue and compact.', compactor)
        calls = [json.loads(line) for line in trace.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1]['resume'])
        self.assertIn('CODEX_PORTABLE_CHECKPOINT_V1', json.dumps(calls[1]['request']))
        self.assertIn('AGENTS_PROJECT_MARKER', calls[1]['instructions'])
        receiver, _, receiver_trace = self.make_claude('receiver', 'CLAUDE_RECEIVER_DONE', handoff_budget_tokens=8000)
        receiver.request('thread/resume', {'threadId':thread_id,'excludeTurns':True})
        self.turn(thread_id, 'RECEIVER_AFTER_COMPACTION_MARKER: continue from the portable checkpoint.', receiver)
        received = json.loads(receiver_trace.read_text(encoding='utf-8').splitlines()[0])
        prompt = json.dumps(received['request'])
        self.assertTrue('Deliberate complete portable summary' in prompt, 'Claude receiver omitted the proven portable summary.')
        self.assertTrue('COMPACT_CURRENT_MARKER' in prompt, 'Claude receiver omitted the uncovered current turn.')
        self.assertFalse('COMPACT_OLD_CONTEXT_MARKER' in prompt, 'Claude receiver repeated a covered raw history turn.')
        restored = receiver.request('thread/read', {'threadId':thread_id,'includeTurns':True})
        self.assertIn('COMPACT_OLD_CONTEXT_MARKER', json.dumps(restored))
        # Leave room for native base instructions/tools while keeping the
        # original history above the available replay budget.
        gpt_return = self.make_gpt(fixture, name='gpt-return', context_window=40000, compact_limit=34000)
        gpt_return.request('thread/resume', {'threadId':thread_id,'excludeTurns':True})
        self.turn(thread_id, 'GPT_AFTER_COMPACTION_MARKER: continue with the same compacted context.', gpt_return)
        self.assertFalse(any('/compact' in path for path in request_paths),
                         'Portable checkpoint replay unexpectedly invoked a provider compact endpoint.')
        gpt_prompt = json.dumps(fixture.calls[-1]['input'], ensure_ascii=False)
        self.assertTrue('Deliberate complete portable summary' in gpt_prompt, 'GPT receiver omitted the proven portable summary.')
        self.assertTrue('COMPACT_CURRENT_MARKER' in gpt_prompt, 'GPT receiver omitted the uncovered current turn.')
        self.assertTrue('RECEIVER_AFTER_COMPACTION_MARKER' in gpt_prompt, 'GPT receiver omitted the subsequent Claude turn.')
        self.assertFalse('COMPACT_OLD_CONTEXT_MARKER' in gpt_prompt, 'GPT receiver repeated a covered raw history turn despite context overflow.')

    def test_long_gpt_tail_after_portable_summary_still_reaches_claude(self):
        # A GPT task whose turns after its newest portable compaction summary exceed the
        # Claude handoff budget used to be refused outright ("shared history exceeds the
        # handoff budget"). Claude now gets the summary plus the newest turns that fit.
        fixture = Fixture()
        self.addCleanup(fixture.close)
        gpt = self.make_gpt(fixture, context_window=60000, compact_limit=20000)
        thread_id = gpt.request('thread/start', {'cwd':str(self.cwd)})['thread']['id']
        self.turn(thread_id, 'LONG_TAIL_OLD_MARKER: ' + 'early detail ' * 9000, gpt)
        self.turn(thread_id, 'LONG_TAIL_SECOND_MARKER: ' + 'second detail ' * 9000, gpt)
        # The loopback server reports no usage, so compact explicitly; its canned answer
        # becomes both the compaction summary and the portable summary.
        gpt.request('thread/compact/start', {'threadId':thread_id})
        compacted = gpt.until(lambda message: message.get('method') == 'turn/completed'
                              and message.get('params',{}).get('threadId') == thread_id)
        self.assertEqual(compacted['params']['turn']['status'], 'completed', compacted)
        self.turn(thread_id, 'LONG_TAIL_AFTER_COMPACTION_MARKER: ' + 'tail detail ' * 4000, gpt)
        self.turn(thread_id, 'LONG_TAIL_NEWEST_MARKER: newest GPT turn.', gpt)
        receiver, _, trace = self.make_claude('tail-receiver', 'CLAUDE_TAIL_DONE', handoff_budget_tokens=8000)
        receiver.request('thread/resume', {'threadId':thread_id,'excludeTurns':True})
        self.turn(thread_id, 'LONG_TAIL_CLAUDE_MARKER: continue the task.', receiver)
        prompt = json.dumps(json.loads(trace.read_text(encoding='utf-8').splitlines()[0])['request'])
        self.assertIn('LONG_TAIL_NEWEST_MARKER', prompt)
        self.assertIn('LONG_TAIL_CLAUDE_MARKER', prompt)
        self.assertIn('summary produced by the other language model', prompt)
        self.assertIn('PROFILE_A_DONE', prompt, 'Claude did not receive the portable summary.')
        self.assertIn('History note', prompt)
        self.assertNotIn('LONG_TAIL_OLD_MARKER: early detail early detail', prompt)

    def test_native_permission_approval_roundtrip_reaches_actual_mcp_helper(self):
        client, _, _ = self.make_claude('permission', 'PERMISSION_DONE', scenario='permission', approval='on-request')
        def approve(request):
            self.assertEqual(request['method'], 'item/commandExecution/requestApproval')
            self.assertIn('python', json.dumps(request['params']))
            return {'decision':'accept'}
        client.approval_response = approve
        started = client.request('thread/start', {'cwd':str(self.cwd)})
        thread_id = started['thread']['id']
        self.turn(thread_id, 'FAKE_PERMISSION_MARKER: exercise the offline approval bridge.', client)
        self.assertEqual(len(client.server_requests), 1)
        restored = client.request('thread/read', {'threadId':thread_id,'includeTurns':True})
        self.assertIn('allow', json.dumps(restored))

    def test_native_interrupt_marks_ledger_dirty_and_retains_partial_output(self):
        client, profile, _ = self.make_claude('cancel', 'CANCEL_DONE', scenario='wait')
        thread_id = client.request('thread/start', {'cwd':str(self.cwd)})['thread']['id']
        started = client.request('turn/start', {'threadId':thread_id,'input':[{'type':'text','text':'FAKE_CANCEL_MARKER','text_elements':[]}]})
        turn_id = started['turn']['id']
        client.until(lambda message: message.get('method') == 'item/agentMessage/delta'
                     and 'waiting' in message.get('params',{}).get('delta',''))
        client.request('turn/interrupt', {'threadId':thread_id,'turnId':turn_id})
        completed = client.until(lambda message: message.get('method') == 'turn/completed'
                                and message.get('params',{}).get('turn',{}).get('id') == turn_id)
        self.assertEqual(completed['params']['turn']['status'], 'interrupted')
        ledgers = list((self.root / 'work/control-center/claude/sessions' / profile).rglob('*.json'))
        self.assertEqual(len(ledgers), 1)
        self.assertTrue(json.loads(ledgers[0].read_text(encoding='utf-8'))['dirty'])
        restored = client.request('thread/read', {'threadId':thread_id,'includeTurns':True})
        self.assertIn('waiting', json.dumps(restored))

    def test_native_plan_collaboration_maps_to_cli_plan_mode(self):
        thread_id = self.rpc.request('thread/start', {'cwd':str(self.cwd)})['thread']['id']
        self.turn(thread_id, 'PLAN_MODE_MARKER: describe the planned work.', extra={
            'collaborationMode':{'mode':'plan','settings':{'model':'cc-opus','reasoning_effort':'high','developer_instructions':None}}})
        call = json.loads(self.trace.read_text(encoding='utf-8').splitlines()[0])
        self.assertEqual(call['permission_mode'], 'plan')


class NativeHarnessConfigurationTests(unittest.TestCase):
    def test_generated_scripts_and_windows_paths_parse_without_launching_runtime(self):
        case = NativeClaudeIntegrationTests('test_no_login_wall_catalog_stream_commit_resume_and_shared_readback')
        try:
            with patch.object(case, 'connect', return_value=object()):
                case.setUp()
            for path in case.base.glob('*.py'):
                compile(path.read_text(encoding='utf-8'), str(path), 'exec')
            config = tomllib.loads((case.base / 'codex-a/config.toml').read_text(encoding='utf-8'))
            self.assertEqual(config['model_providers']['claude_code']['agent']['profile_id'], case.profile)
            self.assertEqual(config['projects'][str(case.cwd)]['trust_level'], 'trusted')
        finally:
            case.doCleanups()


if __name__ == '__main__':
    unittest.main()
