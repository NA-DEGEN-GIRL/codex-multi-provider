"""Opt-in local Responses integration against an existing native executable.

Set LOCAL_NATIVE_E2E_RUNTIME to codex-app-server.exe. The only model endpoint is
the owned loopback fixture; homes and records live in a temporary repo/work
directory. No credentials, installed models, desktop, or runtime build are used.
"""
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import tomllib
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

from manager_core.external_profile import ExternalProfile
from manager_core.providers import ProviderRegistry
from test_manager_claude_runtime_e2e import Rpc


class BoundedRpc(Rpc):
    def until(self, predicate, timeout=30):
        return super().until(predicate, min(timeout, 30))


class LocalResponsesFixture:
    def __init__(self):
        self.calls = []
        self.errors = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.connection.settimeout(10)
                try:
                    size = int(self.headers.get('Content-Length', 0))
                    if self.path != '/v1/responses' or not 0 < size <= 8 * 1024 * 1024:
                        raise ValueError('Unexpected fixture request path or size')
                    body = json.loads(self.rfile.read(size))
                    owner.calls.append(dict(path=self.path, authorization=self.headers.get('Authorization'), body=body))
                    item = owner.output(body)
                    response_id = 'resp_' + uuid4().hex
                    events = [
                        dict(type='response.created', response=dict(id=response_id, status='in_progress', output=[])),
                        dict(type='response.output_item.added', output_index=0, item=item),
                        dict(type='response.output_item.done', output_index=0, item=item),
                        dict(type='response.completed', response=dict(id=response_id, status='completed', output=[item],
                            usage=dict(input_tokens=80, output_tokens=12, total_tokens=92))),
                    ]
                    data = ''.join('event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n'
                                   for event in events).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except Exception as error:
                    owner.errors.append(str(error))
                    self.send_error(500, 'Invalid offline fixture request')

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    @property
    def base_url(self):
        return f'http://127.0.0.1:{self.server.server_port}/v1'

    @staticmethod
    def message(text):
        return dict(type='message', id='msg_' + uuid4().hex, role='assistant', status='completed',
                    content=[dict(type='output_text', text=text, annotations=[])])

    @staticmethod
    def function(name, call_id, arguments):
        return dict(type='function_call', id='fc_' + uuid4().hex, status='completed',
                    name=name, call_id=call_id, arguments=json.dumps(arguments))

    def output(self, body):
        inputs = body['input']
        if any(tool.get('name') == 'manager_probe' for tool in body.get('tools', [])):
            nonce = re.search(r'value "([0-9a-f]{32})"', inputs[0]['content']).group(1)
            if len(inputs) == 1:
                if body.get('tool_choice') != 'auto':
                    raise ValueError('Local verification must allow automatic tool choice')
                return self.function('manager_probe', 'verification-call', {'value': nonce})
            if inputs[-1] != dict(type='function_call_output', call_id='verification-call', output=nonce):
                raise ValueError('Verification did not preserve its tool result')
            return self.message('verified:' + nonce)
        if not any(item.get('type') == 'function_call_output' and item.get('call_id') == 'local-echo-call'
                   for item in inputs):
            return self.function('fixture_echo', 'local-echo-call', {'value': 'LOCAL_TOOL_NONCE'})
        return self.message('LOCAL_NATIVE_DONE: def add(a, b): return a + b')

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=5)


@unittest.skipUnless(os.environ.get('LOCAL_NATIVE_E2E_RUNTIME'),
                     'Set LOCAL_NATIVE_E2E_RUNTIME to an existing native app-server executable.')
class LocalNativeIntegrationTests(unittest.TestCase):
    def setUp(self):
        executable = Path(os.environ['LOCAL_NATIVE_E2E_RUNTIME']).resolve(strict=True)
        self.executable = executable
        directory = ROOT / 'work'
        directory.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='local-native-offline-', dir=directory)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.cwd = self.base / 'project'
        self.cwd.mkdir()
        (self.cwd / 'AGENTS.md').write_text('Only use the synthetic fixture_echo tool for this offline test.\n', encoding='utf-8')
        self.fixture = LocalResponsesFixture()
        self.addCleanup(self.fixture.close)
        self.registry = ProviderRegistry(self.base / 'manager')
        self.wire_model = 'fixture/qwen3.8-flash-next-served'
        saved = self.registry.save(dict(name='Offline local Qwen', deployment='local', auth_type='none',
            execution_scope='all_hosts', base_url=self.fixture.base_url, protocol='responses'),
            dict(wire_model_id=self.wire_model, local_preset_id='qwen3.8-flash-next', reasoning_effort='max'))
        self.model_id = saved['model']['id']
        self.assertFalse(saved['model']['verified'])
        self.assertTrue(self.registry.verify(saved['model']['id'])['verified'])
        self.assertEqual(len(self.fixture.calls), 2)
        self.assertTrue(all(call['authorization'] is None for call in self.fixture.calls))
        self.fixture.calls.clear()
        home = self.base / 'home'
        home.mkdir()
        rendered = self.registry.render_for_host(home, True, [saved['model']['id']],
            primary_model_id=saved['model']['id'], primary_settings={'reasoning_effort': 'max'})
        for name, contents in rendered['files'].items():
            target = home / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents, encoding='utf-8')
        config = ('cli_auth_credentials_store="ephemeral"\napproval_policy="never"\n'
                  'sandbox_mode="read-only"\n' + rendered['files']['config.toml'])
        config += '\n[analytics]\nenabled=false\n[projects.' + json.dumps(str(self.cwd)) + ']\ntrust_level="trusted"\n'
        (home / 'config.toml').write_text(config, encoding='utf-8')
        self.config = tomllib.loads(config)
        self.rendered = rendered
        self.forwarded = []
        adapter = ExternalProfile({'CODEX_MANAGER_PRIMARY_MODEL': json.dumps(rendered['primary'])})

        def route(message):
            routed = adapter.request(message)
            self.forwarded.append(routed)
            return routed

        records = self.base / 'records'
        records.mkdir()
        environment = {key: value for key, value in os.environ.items()
                       if key.upper() in {'SYSTEMROOT', 'WINDIR', 'PATH', 'PATHEXT', 'COMSPEC'}}
        environment.update(CODEX_HOME=str(home), CODEX_RECORD_HOME=str(records), CODEX_SQLITE_HOME=str(records),
            CODEX_MANAGER_SHARED_EXECUTION='1', CODEX_RECORD_SHARED_APPEND='1',
            CODEX_MANAGER_SHARED_WRITER_ID=str(uuid4()), HOME=str(self.base), USERPROFILE=str(self.base),
            LOCALAPPDATA=str(self.base / 'local'), APPDATA=str(self.base / 'roaming'),
            TEMP=str(self.base), TMP=str(self.base))
        self.environment = environment
        self.rpc = BoundedRpc(executable, environment, self.cwd, route)
        self.addCleanup(self.rpc.close)
        self.rpc.approval_response = self.respond_tool
        self.rpc.request('initialize', dict(clientInfo=dict(name='local-offline-integration', version='1'),
            capabilities=dict(experimentalApi=True)))
        self.rpc.send(dict(method='initialized', params={}))

    def respond_tool(self, request):
        self.assertEqual(request['method'], 'item/tool/call')
        self.assertEqual(request['params']['tool'], 'fixture_echo')
        self.assertEqual(request['params']['arguments'], {'value': 'LOCAL_TOOL_NONCE'})
        return dict(contentItems=[dict(type='inputText', text='LOCAL_TOOL_RESULT')], success=True)

    def turn(self, thread_id, text, rpc=None, **extra):
        rpc = rpc or self.rpc
        started = rpc.request('turn/start', dict(threadId=thread_id,
            input=[dict(type='text', text=text, text_elements=[])], **extra))
        turn_id = started['turn']['id']
        completed = rpc.until(lambda message: message.get('method') == 'turn/completed'
            and message.get('params', {}).get('turn', {}).get('id') == turn_id)
        self.assertEqual(completed['params']['turn']['status'], 'completed', completed)

    def test_local_primary_tool_roundtrip_effort_persistence_and_full_context(self):
        account = self.rpc.request('account/read', {'refreshToken': False})
        self.assertFalse(account['requiresOpenaiAuth'])
        models = self.rpc.request('model/list', {})['data']
        model = next(model for model in models if self.wire_model in (model.get('id'), model.get('model')))
        self.assertEqual(model['defaultReasoningEffort'], 'xhigh')
        self.assertEqual([choice['reasoningEffort'] for choice in model['supportedReasoningEfforts']],
                         ['low', 'medium', 'xhigh'])
        for name, contents in self.rendered['files'].items():
            if name.startswith('catalogs/'):
                entry = json.loads(contents)['models'][0]
                self.assertEqual((entry['context_window'], entry['effective_context_window_percent']), (1000000, 100))
        thread_id = self.rpc.request('thread/start', dict(cwd=str(self.cwd), dynamicTools=[dict(
            name='fixture_echo', description='Offline echo; no process, filesystem, or network operation.',
            inputSchema=dict(type='object', properties={'value': {'type': 'string'}}, required=['value'],
                             additionalProperties=False))]))['thread']['id']
        self.turn(thread_id, 'Use fixture_echo and return a tiny add function.', model=self.wire_model, effort='low')
        self.assertEqual(len(self.rpc.server_requests), 1)
        self.assertEqual(len(self.fixture.calls), 2)
        self.assertTrue(all(call['body']['reasoning']['effort'] == 'low' for call in self.fixture.calls))
        self.assertIn('LOCAL_TOOL_RESULT', json.dumps(self.fixture.calls[-1]['body']['input']))
        self.rpc.request('thread/settings/update', dict(threadId=thread_id, model=self.wire_model, effort='max'))
        forwarded = next(message for message in reversed(self.forwarded) if message.get('method') == 'thread/settings/update')
        self.assertEqual(forwarded['params']['effort'], 'xhigh')
        self.turn(thread_id, 'Keep the saved effort and return the final fixture answer.')
        self.assertEqual(len(self.fixture.calls), 3)
        self.assertEqual(self.fixture.calls[-1]['body']['reasoning']['effort'], 'xhigh')
        self.assertTrue(all(call['authorization'] is None for call in self.fixture.calls))
        self.assertTrue(all(call['body']['model'] == self.wire_model for call in self.fixture.calls))
        usage = [message['params']['tokenUsage'] for message in self.rpc.pending
                 if message.get('method') == 'thread/tokenUsage/updated']
        self.assertTrue(usage, 'Native runtime did not publish context metadata')
        self.assertTrue(all(item['modelContextWindow'] == 1000000 for item in usage), usage)
        restored = self.rpc.request('thread/read', dict(threadId=thread_id, includeTurns=True))
        self.assertIn('LOCAL_NATIVE_DONE', json.dumps(restored))
        self.assertEqual(self.fixture.errors, [])

    def test_mock_gpt_primary_delegates_to_local_no_auth_role_with_fresh_context(self):
        home = self.base / 'gpt-home'
        home.mkdir()
        # Provider name selects the native GPT tool surface, while every request
        # is pinned to this owned HTTP fixture and authentication stays absent.
        existing = ('model="gpt-5.5"\nmodel_provider="fixture_gpt"\nmodel_reasoning_effort="high"\n'
                    'cli_auth_credentials_store="ephemeral"\napproval_policy="never"\nsandbox_mode="read-only"\n'
                    '[features]\nenable_request_compression=false\n'
                    '[model_providers.fixture_gpt]\nname="OpenAI"\nwire_api="responses"\n'
                    'requires_openai_auth=false\nsupports_websockets=false\nrequest_max_retries=0\nstream_max_retries=0\n'
                    'base_url=' + json.dumps(self.fixture.base_url) + '\n'
                    '[analytics]\nenabled=false\n'
                    '[projects.' + json.dumps(str(self.cwd)) + ']\ntrust_level="trusted"\n')
        rendered = self.registry.render_for_host(home, True, [self.model_id], existing)
        for name, contents in rendered['files'].items():
            target = home / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents, encoding='utf-8')
        role = rendered['bindings'][0]['role_id']
        child_seen = threading.Event()

        def respond(body):
            if body['model'] == self.wire_model:
                child_seen.set()
                return self.fixture.message('LOCAL_SUBAGENT_DONE')
            if body['model'] != 'gpt-5.5':
                raise ValueError('Unexpected model outside the mock GPT and local role')
            output = next((item for item in body['input'] if item.get('type') == 'function_call_output'
                           and item.get('call_id') == 'spawn-local-worker'), None)
            if output is None:
                item = self.fixture.function('spawn_agent', 'spawn-local-worker', dict(agent_type=role,
                    task_name='local_worker', fork_turns='none', message='LOCAL_SUBAGENT_TASK: return the fixture marker.'))
                item['namespace'] = 'external_agents'
                return item
            if not child_seen.wait(timeout=10):
                raise ValueError('Native delegation did not reach the configured local endpoint: ' + str(output))
            return self.fixture.message('GPT_FIXTURE_DELEGATION_DONE')

        self.fixture.output = respond
        environment = {**self.environment, 'CODEX_HOME': str(home), 'CODEX_MANAGER_SHARED_WRITER_ID': str(uuid4())}
        client = BoundedRpc(self.executable, environment, self.cwd)
        self.addCleanup(client.close)
        client.request('initialize', dict(clientInfo=dict(name='local-delegation-fixture', version='1'),
            capabilities=dict(experimentalApi=True)))
        client.send(dict(method='initialized', params={}))
        thread_id = client.request('thread/start', dict(cwd=str(self.cwd)))['thread']['id']
        self.turn(thread_id, 'PRIVATE_PARENT_MARKER: delegate one fresh task using the configured local role.', rpc=client)
        child_calls = [call for call in self.fixture.calls if call['body']['model'] == self.wire_model]
        self.assertEqual(len(child_calls), 1)
        self.assertIsNone(child_calls[0]['authorization'])
        self.assertEqual(child_calls[0]['body']['reasoning']['effort'], 'xhigh')
        child_input = json.dumps(child_calls[0]['body']['input'])
        self.assertIn('LOCAL_SUBAGENT_TASK', child_input)
        self.assertNotIn('PRIVATE_PARENT_MARKER', child_input)
        parent_calls = [call for call in self.fixture.calls if call['body']['model'] == 'gpt-5.5']
        self.assertEqual(len(parent_calls), 2)
        self.assertIn('external_agents', json.dumps(parent_calls[0]['body']['tools']))
        self.assertTrue(all(call['authorization'] is None for call in self.fixture.calls))
        restored = client.request('thread/read', dict(threadId=thread_id, includeTurns=True))
        self.assertIn('GPT_FIXTURE_DELEGATION_DONE', json.dumps(restored))
        self.assertEqual(self.fixture.errors, [])


if __name__ == '__main__':
    unittest.main()
