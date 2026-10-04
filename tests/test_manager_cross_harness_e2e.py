"""Opt-in native cross-harness delegation. Synthetic accounts and loopback only.

After a successful build in an allowed environment, set CLAUDE_NATIVE_E2E_RUNTIME
to that codex/app-server executable and run this module with PYTHONPATH=tests;scripts.
Without the variable, three native scenarios are explicitly skipped, not validated;
the independent configuration-generation test can still pass. Do not point this at
an older published runtime as a substitute for compiling the changed sources.
"""
import json
import mmap
import os
from pathlib import Path
import re
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
from manager_core.execution_presets import ExecutionPresets
from manager_core.providers import ProviderRegistry, _BEGIN, _END
from manager_core.proxy_auth import account_fingerprint
from manager_core.store import Store
import test_manager_claude_runtime_e2e as claude_native
from test_manager_claude_runtime_e2e import Rpc
from test_manager_local_runtime_e2e import LocalResponsesFixture
from test_manager_proxy_auth import jwt


FAKE_CLAUDE = r'''
import json, os, subprocess, sys, time
from pathlib import Path
args = sys.argv[1:]
assert args[args.index('--disallowedTools')+1] == 'Agent,Task', 'Managed parent/leaf must use native delegation policy'
session = args[args.index('--resume')+1] if '--resume' in args else args[args.index('--session-id')+1]
request = json.loads(sys.stdin.readline())
trace = Path(TRACE_PATH)
with trace.open('a',encoding='utf-8') as stream:
    stream.write(json.dumps({'session':session,'request':request,'model':args[args.index('--model')+1],
        'effort':args[args.index('--effort')+1]})+'\n')
def emit(value): print(json.dumps(value),flush=True)
config = json.loads(Path(args[args.index('--mcp-config')+1]).read_text(encoding='utf-8')) if '--mcp-config' in args else {'mcpServers':{}}
emit({'type':'system','subtype':'init','session_id':session,'model':'fixture',
      'mcp_servers':[{'name':name,'status':'connected'} for name in config['mcpServers']]})
text = json.dumps(request)
if 'CLAUDE_PARENT_MARKER' in text and not trace.with_suffix('.parent-done').exists():
    definition = config['mcpServers']['codex_agents']
    helper = subprocess.Popen([definition['command']]+definition['args'],env=dict(os.environ,**definition['env']),
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,encoding='utf-8',
        **({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}))
    sequence = 0
    def rpc(method, params):
        global sequence
        sequence += 1
        helper.stdin.write(json.dumps({'jsonrpc':'2.0','id':sequence,'method':method,'params':params})+'\n'); helper.stdin.flush()
        return json.loads(helper.stdout.readline())['result']
    rpc('initialize',{'protocolVersion':'2024-11-05'})
    catalog = rpc('tools/list',{})['tools']
    assert len(catalog)==6, catalog
    def call(name, arguments):
        answer = rpc('tools/call',{'name':name,'arguments':arguments})
        if answer.get('isError'): raise RuntimeError(answer)
        result = json.loads(answer['content'][0]['text'])
        with trace.open('a',encoding='utf-8') as stream: stream.write(json.dumps({'tool':name,'result':result})+'\n')
        return result
    def wait_for(marker):
        for _ in range(12):
            result = call('wait_agent',{'timeout_ms':10000})
            if marker in json.dumps(result): return result
        raise RuntimeError('Missing child result '+marker)
    call('spawn_agent',{'agent_type':CHILD_ROLE,'task_name':'worker','fork_turns':'none','message':'CHILD_FIRST_MARKER'})
    wait_for('GPT_CHILD_FIRST_DONE')
    call('send_message',{'target':'worker','message':'CHILD_MAILBOX_MARKER'})
    call('followup_task',{'target':'worker','message':'CHILD_FOLLOWUP_MARKER'})
    wait_for('GPT_CHILD_FOLLOWUP_DONE')
    call('list_agents',{})
    call('spawn_agent',{'agent_type':CHILD_ROLE,'task_name':'cancelled','fork_turns':'none','message':'CHILD_CANCEL_MARKER'})
    deadline = time.monotonic()+15
    while not Path(__CANCEL_PATH__).exists() and time.monotonic()<deadline: time.sleep(.02)
    assert Path(__CANCEL_PATH__).exists(), 'GPT cancellation child never started'
    call('interrupt_agent',{'target':'cancelled'})
    deadline = time.monotonic()+10
    while True:
        agents = call('list_agents',{})['agents']
        if any(agent['agent_name'].endswith('/cancelled') and agent['agent_status']=='interrupted' for agent in agents): break
        assert time.monotonic()<deadline, 'GPT child did not reach interrupted status'
        time.sleep(.02)
    helper.stdin.close(); helper.wait(timeout=10); helper.stdout.close()
    answer = 'CLAUDE_PARENT_BOTH_DIRECTIONS_DONE'
    trace.with_suffix('.parent-done').touch()
elif trace.with_suffix('.parent-done').exists():
    answer = 'CLAUDE_PARENT_BOTH_DIRECTIONS_DONE'
elif 'CHILD_CANCEL_MARKER' in text:
    emit({'type':'stream_event','event':{'type':'content_block_delta','delta':{'type':'text_delta','text':'CHILD_CANCEL_RUNNING'}}})
    for line in sys.stdin: pass
    sys.exit(0)
elif 'CHILD_FOLLOWUP_MARKER' in text:
    answer = 'CLAUDE_CHILD_FOLLOWUP_DONE'
else:
    answer = 'CLAUDE_CHILD_FIRST_DONE'
emit({'type':'assistant','message':{'content':[{'type':'text','text':answer}], 'usage':{'input_tokens':10,'output_tokens':5}}})
emit({'type':'result','is_error':False,'session_id':session,'result':answer,'usage':{'input_tokens':10,'output_tokens':5}})
for line in sys.stdin: pass
'''


@unittest.skipUnless(os.environ.get('CLAUDE_NATIVE_E2E_RUNTIME'), 'Set CLAUDE_NATIVE_E2E_RUNTIME to the new native executable.')
class CrossHarnessTests(unittest.TestCase):
    environment = claude_native.NativeClaudeIntegrationTests.environment

    def turn(self, thread_id, text, rpc=None, extra=None):
        try:
            return claude_native.NativeClaudeIntegrationTests.turn(self, thread_id, text, rpc, extra)
        except AssertionError as error:
            client = rpc or self.rpc
            diagnostics = dict(
                trace=[item for item in self.traces() if 'tool' in item][-8:],
                requests=len(self.fixture.calls), errors=self.fixture.errors,
                other_http=getattr(self.fixture, 'other_http', []),
                results=[item for call in self.fixture.calls[-2:] for item in call['body'].get('input', [])
                         if item.get('type') in ('function_call_output', 'agent_message')][-8:],
                events=[item for item in client.pending[-30:] if item.get('method') in
                        ('error', 'turn/completed', 'item/completed')][-8:])
            raise AssertionError(str(error) + '\nOffline fixture diagnostics: '
                                 + json.dumps(diagnostics, ensure_ascii=False)[:24000]) from error

    @classmethod
    def setUpClass(cls):
        executable=Path(os.environ['CLAUDE_NATIVE_E2E_RUNTIME']).resolve(strict=True)
        with executable.open('rb') as stream, mmap.mmap(stream.fileno(),0,access=mmap.ACCESS_READ) as data:
            if any(data.find(marker)<0 for marker in (b'CODEX_MANAGER_EXECUTION_PRESETS',b'CODEX_CLAUDE_CODE_AGENT_V1')):
                raise AssertionError('The configured executable lacks the new preset/Claude capabilities; build the changed native sources first.')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cross-harness-offline-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root, self.cwd, self.shared = (self.base / name for name in ('manager','project','canonical'))
        for path in (self.root,self.cwd,self.shared): path.mkdir()
        self.fixture = LocalResponsesFixture()
        self.fixture.other_http = []
        original_post = self.fixture.server.RequestHandlerClass.do_POST
        def offline_post(handler):
            if handler.path != '/v1/responses':
                self.fixture.other_http.append(handler.path)
            original_post(handler)
        self.fixture.server.RequestHandlerClass.do_POST = offline_post
        # Built-in OpenAI enables WebSockets. A 426 selects its supported HTTP
        # fallback; overriding the built-in provider definition is forbidden.
        def offline_get(handler):
            if handler.headers.get('Upgrade', '').lower() == 'websocket':
                handler.send_response(426)
                handler.send_header('Content-Length', '0')
                handler.end_headers()
            else:
                payload = b'{"models":[]}'
                handler.send_response(200)
                handler.send_header('Content-Type', 'application/json')
                handler.send_header('Content-Length', str(len(payload)))
                handler.end_headers()
                handler.wfile.write(payload)
        self.fixture.server.RequestHandlerClass.do_GET = offline_get
        self.addCleanup(self.fixture.close)
        self.store = Store(self.root)
        self.providers = ProviderRegistry(self.root)
        self.presets = ExecutionPresets(self.store,self.providers)
        self.gpt = self.store.add_profile('Synthetic GPT account B')
        self.claude = self.store.add_profile('Synthetic Claude account',claude_settings={})
        self.gpt_token = jwt('synthetic-account-b',expiry=int(time.time())+86400)
        self.store.mutate(lambda data: next(profile for profile in data['profiles'] if profile['id']==self.gpt['id']).update(
            auth_mode='native',account_fingerprint=account_fingerprint('synthetic-account-b')))
        self.gpt = self.store.profile(self.gpt['id'])
        home = Path(self.gpt['home']); home.mkdir(parents=True)
        self.auth_bytes = json.dumps({'auth_mode':'chatgptAuthTokens','tokens':{'id_token':self.gpt_token,
            'access_token':self.gpt_token,'refresh_token':'synthetic-no-refresh','account_id':'synthetic-account-b'}}).encode()
        (home/'auth.json').write_bytes(self.auth_bytes)
        self.trace = self.base/'claude-trace.jsonl'
        self.cancel_marker = self.base/'gpt-cancel-started'
        self.cancel_release = threading.Event()
        self.addCleanup(self.cancel_release.set)

    def prepare(self, parent_claude, alternate=False):
        owner = self.claude if parent_claude else self.gpt
        child = self.gpt if parent_claude else self.claude
        home = Path(owner['home']); home.mkdir(parents=True,exist_ok=True)
        preset = self.presets.save(owner['id'],dict(name='Cross harness fixture',roles=[dict(name='Fixture child',
            profile_id=child['id'],model='gpt-6-astra' if parent_claude else 'opus',effort='medium' if parent_claude else 'high')]))
        self.presets.set_default(owner['id'],preset['id'],preset['revision'])
        if alternate:
            self.alternate_preset=self.presets.save(owner['id'],dict(name='Alternate prepared preset',roles=[dict(name='Alternate child',
                profile_id=child['id'],model='gpt-5.6-sol' if parent_claude else 'claude-opus-5-5',effort='high')]))
        base = ('approval_policy="never"\nsandbox_mode="danger-full-access"\ncli_auth_credentials_store="file"\n'
                'chatgpt_base_url='+json.dumps(self.fixture.base_url)+'\n'
                'openai_base_url='+json.dumps(self.fixture.base_url)+'\n'
                '[features]\nenable_request_compression=false\nshell_tool=false\napps=false\n'
                '[analytics]\nenabled=false\n[projects.'+json.dumps(str(self.cwd))+']\ntrust_level="trusted"\n')
        if parent_claude:
            primary = render(self.providers,home,dict(id=owner['id'],settings={}),base)
            files = primary['files']
        else:
            files = {'config.toml':'model="gpt-5.5"\nmodel_provider="openai"\nmodel_reasoning_effort="medium"\n'+base+'\n'+_BEGIN+'\n'+_END+'\n'}
        generated = self.presets.render_native_registry(owner['id'],files['config.toml'])
        files.update(generated['files'])
        role = next(item for item in generated['manifest']['presets'] if item['id']==preset['id'])['role_ids'][0]
        source = FAKE_CLAUDE.replace('TRACE_PATH',repr(str(self.trace))).replace('CHILD_ROLE',repr(role)).replace('__CANCEL_PATH__',repr(str(self.cancel_marker)))
        fake = self.base/'fake-claude.py'; fake.write_text(source,encoding='utf-8')
        wrapper = self.base/'runner.py'
        wrapper.write_text('import sys\n'+f'sys.path.insert(0,{str(Path(__file__).resolve().parents[1]/"scripts")!r})\n'+
            'from manager_core.claude_runner import serve\n'+
            f'raise SystemExit(serve({str(self.root)!r},{self.claude["id"]!r},_test_command=[sys.executable,"-X","utf8",{str(fake)!r}],'+
            f'_test_auth={{"logged_in":True,"method":"fixture","cli_version":"2.1.282","account_identity":{self.claude["id"]!r}}},_test_settings={{}}))\n',encoding='utf-8')
        config = files['config.toml']
        def replace_agent(match):
            agent = tomllib.loads(match.group(0))['agent']
            agent['command'], agent['args'] = sys.executable,['-X','utf8',str(wrapper)]
            return 'agent = '+toml_value(agent)
        files['config.toml'] = re.sub(r'^agent = .*$',replace_agent,config,flags=re.MULTILINE)
        for name,contents in files.items():
            path = home/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_text(contents,encoding='utf-8')
        environment = self.environment(home,owner['id'])
        environment['CODEX_MANAGER_EXECUTION_PRESETS']=generated['path']
        client = Rpc(os.environ['CLAUDE_NATIVE_E2E_RUNTIME'],environment,self.cwd)
        self.addCleanup(client.close)
        client.request('initialize',{'clientInfo':{'name':'cross-harness-offline','version':'1'},'capabilities':{'experimentalApi':True}})
        client.send({'method':'initialized','params':{}})
        self.rpc=client
        self.connection=(environment,self.cwd)
        return client,role,preset

    def traces(self):
        return [json.loads(line) for line in self.trace.read_text(encoding='utf-8').splitlines()] if self.trace.exists() else []

    def test_claude_parent_controls_native_gpt_child_with_selected_account(self):
        def output(body):
            text=json.dumps(body['input'])
            if 'CHILD_CANCEL_MARKER' in text:
                self.cancel_marker.touch(); self.cancel_release.wait(20)
                return self.fixture.message('CANCELLED_RESPONSE_MUST_NOT_COMPLETE')
            return self.fixture.message('GPT_CHILD_FOLLOWUP_DONE' if 'CHILD_FOLLOWUP_MARKER' in text else 'GPT_CHILD_FIRST_DONE')
        self.fixture.output=output
        client,role,preset=self.prepare(True)
        self.assertFalse((Path(self.claude['home'])/'auth.json').exists())
        thread=client.request('thread/start',{'cwd':str(self.cwd)})['thread']['id']
        self.turn(thread,'CLAUDE_PARENT_MARKER: use the configured native GPT role.',client)
        trace=self.traces()
        tools=[item['tool'] for item in trace if 'tool' in item and item['tool']!='wait_agent']
        self.assertEqual(tools[:6],
            ['spawn_agent','send_message','followup_task','list_agents','spawn_agent','interrupt_agent'])
        self.assertTrue(tools[6:] and all(name=='list_agents' for name in tools[6:]))
        self.assertGreaterEqual(len(self.fixture.calls),3)
        self.assertTrue(all(call['authorization']=='Bearer '+self.gpt_token for call in self.fixture.calls))
        self.assertTrue(all(call['body']['model']=='gpt-6-astra' for call in self.fixture.calls))
        prompts=[json.dumps(call['body']['input']) for call in self.fixture.calls]
        self.assertTrue(any('CHILD_MAILBOX_MARKER' in text and 'CHILD_FOLLOWUP_MARKER' in text for text in prompts))
        self.assertTrue(all('CLAUDE_PARENT_MARKER' not in text for text in prompts))
        self.assertEqual((Path(self.gpt['home'])/'auth.json').read_bytes(),self.auth_bytes)
        restored=client.request('thread/read',{'threadId':thread,'includeTurns':True})
        self.assertIn('CLAUDE_PARENT_BOTH_DIRECTIONS_DONE',json.dumps(restored))
        self.cancel_release.set()

    def test_native_gpt_parent_controls_claude_child_and_preserves_cli_session(self):
        client,role,preset=self.prepare(False)
        stage=0
        spawn_output=None
        def output(body):
            nonlocal stage, spawn_output
            stage+=1
            if stage>30: raise AssertionError('Delegation fixture exceeded bounded turn count')
            text=json.dumps(body['input'])
            def call(name,identity,args,namespace='external_agents'):
                item=self.fixture.function(name,identity,args);item['namespace']=namespace; return item
            if 'spawn-first' not in text:
                return call('spawn_agent','spawn-first',dict(agent_type=role,task_name='worker',fork_turns='none',message='CHILD_FIRST_MARKER'))
            spawn_output = next((item.get('output') for item in body['input']
                                 if item.get('type') == 'function_call_output' and item.get('call_id') == 'spawn-first'), None)
            if spawn_output is not None and '"task_name"' not in spawn_output:
                return self.fixture.message('FIXTURE_SPAWN_REFUSED')
            if 'CLAUDE_CHILD_FIRST_DONE' not in text:
                return call('wait_agent','wait-first-'+str(stage),{'timeout_ms':10000},'collaboration')
            if 'mail-child' not in text:
                return call('send_message','mail-child',{'target':'worker','message':'CHILD_MAILBOX_MARKER'})
            if 'follow-child' not in text:
                return call('followup_task','follow-child',{'target':'worker','message':'CHILD_FOLLOWUP_MARKER'})
            if 'CLAUDE_CHILD_FOLLOWUP_DONE' not in text:
                return call('wait_agent','wait-follow-'+str(stage),{'timeout_ms':10000},'collaboration')
            if 'spawn-cancel' not in text:
                return call('spawn_agent','spawn-cancel',dict(agent_type=role,task_name='cancelled',fork_turns='none',message='CHILD_CANCEL_MARKER'))
            if 'interrupt-child' not in text:
                deadline=time.monotonic()+15
                while not any('CHILD_CANCEL_MARKER' in json.dumps(item.get('request')) for item in self.traces()):
                    if time.monotonic()>deadline: raise AssertionError('Claude cancellation child did not start')
                    time.sleep(.02)
                return call('interrupt_agent','interrupt-child',{'target':'cancelled'},'collaboration')
            interrupted = any(
                agent.get('agent_name', '').endswith('/cancelled') and agent.get('agent_status')=='interrupted'
                for item in body['input'] if item.get('type')=='function_call_output'
                and item.get('call_id', '').startswith('cancel-status-')
                for agent in json.loads(item['output']).get('agents', []))
            if not interrupted:
                return call('list_agents','cancel-status-'+str(stage),{},'collaboration')
            return self.fixture.message('GPT_PARENT_CLAUDE_DELEGATION_DONE')
        self.fixture.output=output
        thread=client.request('thread/start',{'cwd':str(self.cwd)})['thread']['id']
        self.turn(thread,'PRIVATE_GPT_PARENT_MARKER: delegate to the configured Claude role.',client)
        calls=[item for item in self.traces() if 'request' in item]
        self.assertGreaterEqual(len(calls),3,spawn_output)
        self.assertEqual(calls[0]['session'],calls[1]['session'])
        self.assertNotEqual(calls[0]['session'],calls[-1]['session'])
        self.assertTrue(all('PRIVATE_GPT_PARENT_MARKER' not in json.dumps(call['request']) for call in calls))
        self.assertIn('CHILD_MAILBOX_MARKER',json.dumps(calls[1]['request']))
        self.assertEqual({(call['model'],call['effort']) for call in calls},{('opus','high')})
        restored=client.request('thread/read',{'threadId':thread,'includeTurns':True})
        self.assertIn('GPT_PARENT_CLAUDE_DELEGATION_DONE',json.dumps(restored))
        self.assertEqual(self.fixture.errors,[])

    def test_task_preset_change_is_deferred_to_next_turn_and_persists_on_resume(self):
        client,first_role,first_preset=self.prepare(False,alternate=True)
        second_role=self.presets.role_id(self.alternate_preset['roles'][0])
        captured=threading.Event()
        release=threading.Event()
        self.addCleanup(release.set)
        def output(body):
            if len(self.fixture.calls)==1:
                captured.set()
                if not release.wait(20): raise AssertionError('Fixture turn was not released')
            return self.fixture.message('PRESET_CATALOG_DONE')
        self.fixture.output=output
        thread=client.request('thread/start',{'cwd':str(self.cwd)})['thread']['id']
        started=client.request('turn/start',{'threadId':thread,'input':[{'type':'text','text':'FIRST_PRESET_MARKER','text_elements':[]}]})
        self.assertTrue(captured.wait(15),'First provider request did not start')
        selected={'id':self.alternate_preset['id'],'revision':self.alternate_preset['revision']}
        client.request('thread/settings/update',{'threadId':thread,'executionPreset':selected})
        release.set()
        finished=client.until(lambda item:item.get('method')=='turn/completed' and item.get('params',{}).get('turn',{}).get('id')==started['turn']['id'])
        self.assertEqual(finished['params']['turn']['status'],'completed',finished)
        self.turn(thread,'SECOND_PRESET_MARKER',client)
        self.assertEqual(len(self.fixture.calls),2)
        catalogs=[json.dumps(call['body'].get('tools',[])) for call in self.fixture.calls]
        self.assertIn(first_role,catalogs[0]); self.assertNotIn(second_role,catalogs[0])
        self.assertIn(second_role,catalogs[1]); self.assertNotIn(first_role,catalogs[1])
        client.close()
        environment,cwd=self.connection
        reopened=Rpc(os.environ['CLAUDE_NATIVE_E2E_RUNTIME'],environment,cwd)
        self.addCleanup(reopened.close)
        reopened.request('initialize',{'clientInfo':{'name':'preset-resume-fixture','version':'1'},'capabilities':{'experimentalApi':True}})
        reopened.send({'method':'initialized','params':{}})
        reopened.request('thread/resume',{'threadId':thread,'excludeTurns':True})
        self.turn(thread,'RESUMED_PRESET_MARKER',reopened)
        resumed_catalog=json.dumps(self.fixture.calls[-1]['body'].get('tools',[]))
        self.assertIn(second_role,resumed_catalog); self.assertNotIn(first_role,resumed_catalog)
        self.assertEqual((Path(self.gpt['home'])/'auth.json').read_bytes(),self.auth_bytes)


class CrossHarnessConfigurationTests(unittest.TestCase):
    def test_fixture_scripts_and_both_generated_account_bindings_parse_offline(self):
        for direction in (False,True):
            case=CrossHarnessTests('test_claude_parent_controls_native_gpt_child_with_selected_account')
            try:
                case.setUp()
                with patch.dict(os.environ,{'CLAUDE_NATIVE_E2E_RUNTIME':'offline-not-launched'}), patch(__name__+'.Rpc'):
                    _,role,_=case.prepare(direction)
                for path in case.base.glob('*.py'): compile(path.read_text(encoding='utf-8'),str(path),'exec')
                owner=case.claude if direction else case.gpt
                home=Path(owner['home'])
                config=tomllib.loads((home/'config.toml').read_text(encoding='utf-8'))
                self.assertNotIn('openai', config.get('model_providers', {}))
                self.assertEqual(config['openai_base_url'], case.fixture.base_url)
                manifest=json.loads((home/'manager-execution-presets.json').read_text(encoding='utf-8'))
                self.assertIn(role,config['agents'])
                self.assertEqual(manifest['roles'][role]['model_provider'],'openai' if direction else 'cc_claude_'+case.claude['id'].replace('-',''))
            finally:
                case.doCleanups()


if __name__=='__main__': unittest.main()
