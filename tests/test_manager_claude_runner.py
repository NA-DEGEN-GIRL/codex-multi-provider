"""Offline CLI adapter tests. These never log in or call an Anthropic model."""
import io
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from manager_core.claude_auth import (ClaudeError, config_dir, discover_cli, mask_email,
                                     sanitize_status, scrub_environment)
from manager_core.claude_protocol import MAX_LINE_BYTES, ProtocolError, normalize, read_message
from manager_core.claude_profiles import automatic_context_window
from manager_core.claude_runner import (SessionLedger, compact_summary, digest, run_settings,
                                       serve, session_lock, PermissionBridge, system_prompt_text,
                                       private_temporary_directory, build_command, AUTH_FAILURES,
                                       AUTH_STOP_NOTE, AUTH_TOOL_STOPPED, BorrowedLogin, renew_login,
                                       token_expired)


FAKE = r'''
import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
session = args[args.index('--resume') + 1] if '--resume' in args else args[args.index('--session-id') + 1]
scenario = os.environ.get('CLAUDE_FIXTURE_SCENARIO', 'success')
def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)
request = json.loads(sys.stdin.readline())
if os.environ.get('CLAUDE_FIXTURE_TRACE'):
    with open(os.environ['CLAUDE_FIXTURE_TRACE'], 'a', encoding='utf-8') as trace:
        trace.write(json.dumps({'token':os.environ.get('CLAUDE_CODE_OAUTH_TOKEN'),'resume':'--resume' in args,
            'session':session,'request':request,'checkpoint':'CODEX_PORTABLE_CHECKPOINT_V1' in json.dumps(request)})+'\n')
if 'CODEX_PORTABLE_CHECKPOINT_V1' in json.dumps(request):
    assert '--resume' in args and '--plugin-dir' not in args and '--permission-prompt-tool' not in args
    assert args[args.index('--tools')+1] == '' and args[args.index('--disallowedTools')+1] == '*'
    assert args[args.index('--max-turns')+1] == '1' and args[args.index('--permission-prompts')+1] == 'none'
    assert '--safe-mode' in args and '--strict-mcp-config' in args
    assert json.loads(args[args.index('--mcp-config')+1]) == {'mcpServers':{}}
    emit({'type':'system','subtype':'init','session_id':session,'model':'fixture','mcp_servers':[]})
    if scenario == 'compact_nested':
        emit({'type':'system','subtype':'compact_boundary','compact_metadata':{'trigger':'auto'}})
    if scenario == 'compact_rate_limit':
        emit({'type':'rate_limit_event','rate_limit_info':{'status':'allowed_warning','utilization':0.9}})
    if scenario == 'auth_checkpoint' and os.environ.get('CLAUDE_CODE_OAUTH_TOKEN') == 'dummy-token-1':
        text = 'Failed to authenticate. API Error: 401 OAuth access token has expired'
        emit({'type':'assistant','error':'authentication_failed',
              'message':{'content':[{'type':'text','text':text}],'usage':{'input_tokens':0,'output_tokens':0}}})
        emit({'type':'result','subtype':'success','is_error':True,'session_id':session,'result':text,
              'api_error_status':401,'usage':{'input_tokens':0,'output_tokens':0},'total_cost_usd':0.02})
    elif scenario != 'compact_cancel':
        summary = 'Deliberate complete portable summary: goal, constraints, completed work and next steps.'
        emit({'type':'assistant','message':{'content':[{'type':'text','text':summary}],
              'usage':{'input_tokens':3,'output_tokens':2,'cache_read_input_tokens':50}}})
        emit({'type':'result','is_error':scenario == 'compact_failure','session_id':session,'result':summary,
              'usage':{'input_tokens':3,'output_tokens':2,'cache_read_input_tokens':50},'total_cost_usd':0.03})
    for line in sys.stdin:
        pass
    sys.exit(0)
if scenario == 'oversize':
    print('x' * (8 * 1024 * 1024 + 1), flush=True)
else:
    emit({'type':'system','subtype':'init','session_id':session,'model':'fixture',
          'mcp_servers':[{'name':name,'status':'connected'} for name in ('codex_bridge','codex_agents')]})
    if scenario == 'usage':
        emit({'type':'rate_limit_event','rate_limit_info':{'rateLimitType':'five_hour',
              'utilization':0.25,'resetsAt':2000000000}})
    emit({'type':'user','message':request['message']})
    print('PRIVATE_STDERR_AUTH_SECRET', file=sys.stderr, flush=True)
    if scenario == 'ultracode_inactive':
        check = json.loads(sys.stdin.readline())
        assert check['request'] == {'subtype':'get_settings'}
        emit({'type':'control_response','response':{'subtype':'success','request_id':check['request_id'],
              'response':{'applied':{'model':'fixture','effort':'xhigh','ultracode':False}}}})
    if scenario == 'control':
        emit({'type':'control_request','request':{'private':'PRIVATE_CONTROL_AUTH_SECRET'}})
    elif scenario in ('workflow', 'workflow_queued'):
        settings = json.loads(args[args.index('--settings')+1])
        assert settings['permissions']['allow'] == ['Workflow'], settings
        task = {'task_id':'wf-1','task_type':'local_workflow','description':'review'}
        watcher = {'task_id':'watch-1','task_type':'local_workflow','description':'watch','ambient':True}
        emit({'type':'system','subtype':'task_started','task_id':'wf-1','task_type':'local_workflow','workflow_name':'review'})
        emit({'type':'system','subtype':'background_tasks_changed','tasks':[task, watcher]})
        if scenario == 'workflow_queued':
            # The workflow reported back before the first turn ended.
            emit({'type':'system','subtype':'background_tasks_changed','tasks':[watcher]})
            emit({'type':'system','subtype':'task_notification','task_id':'wf-1','status':'completed','summary':'3 findings'})
        emit({'type':'assistant','message':{'content':[{'type':'text','text':'Workflow launched.'}],
              'usage':{'input_tokens':10,'output_tokens':4}}})
        emit({'type':'result','subtype':'success','is_error':False,'session_id':session,'result':'Workflow launched.',
              'usage':{'input_tokens':10,'output_tokens':4},'total_cost_usd':0.02})
        if scenario == 'workflow':
            emit({'type':'system','subtype':'background_tasks_changed','tasks':[watcher]})
            emit({'type':'system','subtype':'task_notification','task_id':'wf-1','status':'completed','summary':'3 findings'})
        emit({'type':'stream_event','event':{'type':'content_block_delta','delta':{'type':'text_delta','text':'Final'}}})
        emit({'type':'assistant','message':{'content':[{'type':'text','text':'Final answer from the workflow.'}],
              'usage':{'input_tokens':20,'output_tokens':6}}})
        emit({'type':'result','subtype':'success','is_error':False,'session_id':session,
              'result':'Final answer from the workflow.','usage':{'input_tokens':20,'output_tokens':6},
              'total_cost_usd':0.05})
    elif scenario == 'wait':
        emit({'type':'stream_event','event':{'type':'content_block_delta','delta':{'type':'text_delta','text':'waiting'}}})
    elif scenario == 'api_error' or (scenario.startswith('auth_') and scenario != 'auth_checkpoint' and (
            scenario == 'auth_always' or os.environ.get('CLAUDE_CODE_OAUTH_TOKEN') == 'dummy-token-1')):
        if scenario == 'auth_tool':
            # A background agent's command that is still running when the main request fails.
            emit({'type':'assistant','parent_tool_use_id':'agent-1','message':{'content':[
                  {'type':'tool_use','id':'bg-tool-1','name':'Bash','input':{'command':'sleep 600'}}]}})
        # The official CLI's shape for a failed API request (2.1.282).
        text = ('API Error: 529 Overloaded' if scenario == 'api_error'
                else 'Failed to authenticate. API Error: 401 Invalid bearer token' if scenario == 'auth_rejected'
                else 'Failed to authenticate. API Error: 401 OAuth access token has expired')
        emit({'type':'assistant','error':'server_error' if scenario == 'api_error' else 'authentication_failed',
              'message':{'content':[{'type':'text','text':text}],'usage':{'input_tokens':0,'output_tokens':0}}})
        if scenario == 'auth_no_result':
            sys.exit(1)  # A crash after the failed request, before the final result.
        emit({'type':'result','subtype':'success','is_error':True,'session_id':session,'result':text,
              'api_error_status':529 if scenario == 'api_error' else 401,
              'usage':{'input_tokens':1,'output_tokens':0},'total_cost_usd':0.01})
    elif scenario == 'auth_background':
        # The renewed launch starts a background agent and reports after its first result.
        emit({'type':'system','subtype':'background_tasks_changed',
              'tasks':[{'task_id':'agent-1','task_type':'local_agent','description':'review'}]})
        emit({'type':'result','subtype':'success','is_error':False,'session_id':session,'result':'Agent started.',
              'usage':{'input_tokens':5,'output_tokens':2},'total_cost_usd':0.02})
        emit({'type':'system','subtype':'background_tasks_changed','tasks':[]})
        emit({'type':'assistant','message':{'content':[{'type':'text','text':'Agent finished.'}],
              'usage':{'input_tokens':6,'output_tokens':3}}})
        emit({'type':'result','subtype':'success','is_error':False,'session_id':session,'result':'Agent finished.',
              'usage':{'input_tokens':6,'output_tokens':3},'total_cost_usd':0.03})
    else:
        if scenario == 'managed_leaf':
            assert '--mcp-config' not in args
            assert args[args.index('--disallowedTools')+1] == 'Agent,Task'
            instructions = Path(args[args.index('--append-system-prompt-file')+1]).read_text(encoding='utf-8')
            assert 'Task delegation is disabled by the selected execution preset' in instructions
        if scenario == 'delegation':
            assert '--permission-prompt-tool' not in args
            assert args[args.index('--disallowedTools')+1] == 'Agent,Task'
            assert {'Agent','Task'}.issubset(json.loads(args[args.index('--settings')+1])['permissions']['deny'])
            instructions = Path(args[args.index('--append-system-prompt-file')+1]).read_text(encoding='utf-8')
            assert 'use only the codex_agents MCP tools' in instructions
            config = json.loads(Path(args[args.index('--mcp-config')+1]).read_text(encoding='utf-8'))
            assert set(config['mcpServers']) == {'codex_agents'}
            definition = config['mcpServers']['codex_agents']
            helper = subprocess.Popen([definition['command']] + definition['args'],
                env=dict(os.environ, **definition['env']), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
                **({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
            def rpc(identifier, method, params):
                helper.stdin.write(json.dumps({'jsonrpc':'2.0','id':identifier,'method':method,'params':params})+'\n')
                helper.stdin.flush()
                return json.loads(helper.stdout.readline())['result']
            rpc(1, 'initialize', {'protocolVersion':'2024-11-05'})
            catalog = rpc(2, 'tools/list', {})['tools']
            for identifier, tool in enumerate(catalog, 3):
                answer = rpc(identifier, 'tools/call', {'name':tool['name'],'arguments':{'fixture':tool['name']}})
                assert not answer['isError']
                assert json.loads(answer['content'][0]['text']) == {'native_result':tool['name']}
            helper.stdin.close()
            helper.wait(timeout=10)
            helper.stdout.close()
        if scenario == 'permission':
            inp = {'command':'python -c "print(1)"'}
            emit({'type':'assistant','message':{'content':[{'type':'tool_use','id':'tool-1','name':'Bash','input':inp}]}})
            config = json.loads(Path(args[args.index('--mcp-config')+1]).read_text(encoding='utf-8'))
            definition = config['mcpServers']['codex_bridge']
            helper = subprocess.Popen([definition['command']] + definition['args'],
                env=dict(os.environ, **definition['env']), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
                **({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
            def rpc(identifier, method, params):
                helper.stdin.write(json.dumps({'jsonrpc':'2.0','id':identifier,'method':method,'params':params})+'\n')
                helper.stdin.flush()
                return json.loads(helper.stdout.readline())['result']
            rpc(1, 'initialize', {'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'fake-cli','version':'1'}})
            assert rpc(2, 'tools/list', {})['tools'][0]['name'] == 'approve'
            answer = rpc(3, 'tools/call', {'name':'approve','arguments':{'tool_name':'Bash','tool_use_id':'tool-1','input':inp}})
            decision = json.loads(answer['content'][0]['text'])
            helper.stdin.close()
            helper.wait(timeout=10)
            helper.stdout.close()
            emit({'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'tool-1',
                  'content':decision['behavior'],'is_error':decision['behavior'] != 'allow'}]}})
        if scenario in ('compact','compact_failure','compact_cancel','compact_nested','compact_rate_limit','auth_compact',
                        'auth_checkpoint'):
            emit({'type':'system','subtype':'compact_boundary','compact_metadata':{'trigger':'auto','pre_tokens':170000}})
            path = Path(os.environ['CLAUDE_CONFIG_DIR']) / 'projects' / 'fixture' / (session + '.jsonl')
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(json.dumps({'type':'user','sessionId':session,'isCompactSummary':True,
                 'message':{'content':'Verified compact summary: changed README.'}})+'\n',encoding='utf-8')
        emit({'type':'stream_event','event':{'type':'content_block_delta','delta':{'type':'text_delta','text':'안녕하세요'}}})
        emit({'type':'assistant','message':{'content':[{'type':'text','text':'안녕하세요'}],
              'usage':{'input_tokens':10,'output_tokens':4,'cache_read_input_tokens':100}}})
        emit({'type':'result','subtype':'success','is_error':False,'session_id':session,'result':'안녕하세요',
              'usage':{'input_tokens':10,'output_tokens':4,'cache_read_input_tokens':100},'total_cost_usd':0.02})
for line in sys.stdin:
    pass
'''


class Incoming:
    def __init__(self):
        self.messages = queue.Queue()

    def send(self, message):
        self.messages.put(json.dumps(message).encode() + b'\n' if message is not None else b'')

    def readline(self, size=-1):
        return self.messages.get()


class Outgoing(io.BytesIO):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def write(self, payload):
        result = super().write(payload)
        self.callback(json.loads(payload))
        return result


class BorrowedContext:
    """The SSH execution context's shape with a dummy lent token; no host binding."""
    def __init__(self, root, cli, profile, expires_at):
        self.ledger_directory = root / 'remote-state'
        self.configuration_directory = root.parent / 'remote-account'
        self.cli, self.profile, self.expires_at = cli, profile, expires_at
        self.identity = digest('remote-account')

    def prepare(self, cwd):
        self.configuration_directory.mkdir(exist_ok=True)
        environment = scrub_environment(self.configuration_directory)
        environment['CLAUDE_CODE_OAUTH_TOKEN'] = 'dummy-token-1'
        return dict(settings={}, configuration_directory=self.configuration_directory, environment=environment,
                    cli=self.cli, plugins=[], status=dict(logged_in=True, method='oauth_token',
                    cli_version='2.1.282', account_identity=self.identity),
                    borrowed_auth=dict(profileId=self.profile, accountIdentity=self.identity,
                                       expiresAt=self.expires_at))

    def renewal(self, token='dummy-token-2', expires_in=8 * 3600, **changes):
        credentials = dict(profileId=self.profile, accessToken=token, accountIdentity=self.identity,
                           expiresAt=int(time.time()) + expires_in)
        return dict(type='auth_update', available=True, credentials=dict(credentials, **changes))


class ClaudeAuthTests(unittest.TestCase):
    def test_environment_isolates_accounts_without_touching_tokens(self):
        env = {'PATH': 'tools', 'ANTHROPIC_API_KEY': 'private', 'ANTHROPIC_BASE_URL': 'proxy',
               'OPENAI_API_KEY': 'private', 'CODEX_EXTERNAL_KEY': 'private', 'CLAUDE_CONFIG_DIR': 'old',
               'CLAUDE_CODE_OAUTH_TOKEN': 'private', 'CLAUDE_CODE_USE_BEDROCK': '1',
               'CLAUDE_CODE_RESUME_INTERRUPTED_TURN': '1', 'CLAUDECODE': '1', 'CLAUDE_CODE_SIMPLE': '1'}
        env['DEEPSEEK_API_KEY'] = 'private'
        result = scrub_environment(Path('profile'), env)
        self.assertEqual(result, {'PATH': 'tools', 'CLAUDE_CONFIG_DIR': 'profile', 'DISABLE_AUTOUPDATER': '1'})

    def test_auth_status_allowlist_drops_secrets_and_masks_email(self):
        result = sanitize_status({'loggedIn': True, 'authMethod': 'claude.ai', 'email': 'person@example.com',
                                  'accessToken': 'PRIVATE', 'orgId': 'sensitive', 'subscriptionType': 'max'})
        self.assertEqual(len(result.pop('account_identity')), 64)
        self.assertEqual(result, {'logged_in': True, 'method': 'claude.ai', 'email': 'p***@example.com',
                                  'subscription_type': 'max'})
        self.assertIsNone(mask_email('invalid'))
        with self.assertRaises(ClaudeError):
            sanitize_status({'loggedIn': 'true'})

    def test_configuration_path_never_uses_repository_or_inherited_claude_home(self):
        profile = str(uuid4())
        with tempfile.TemporaryDirectory() as base:
            result = config_dir(profile, {'LOCALAPPDATA': base, 'CLAUDE_CONFIG_DIR': 'private'})
            self.assertEqual(result, Path(base).resolve() / 'codex-multi-provider/claude-profiles' / profile)
        with self.assertRaises(ValueError):
            config_dir('../escape', {})

    def test_git_bash_install_override_is_preserved_while_provider_controls_are_removed(self):
        result = scrub_environment(Path('profile'), {'CLAUDE_CODE_GIT_BASH_PATH':'C:/Tools/Git/bin/bash.exe',
                                                     'CLAUDE_CODE_USE_BEDROCK':'1'})
        self.assertEqual(result['CLAUDE_CODE_GIT_BASH_PATH'], 'C:/Tools/Git/bin/bash.exe')
        self.assertNotIn('CLAUDE_CODE_USE_BEDROCK', result)

    def test_shell_wrappers_are_rejected(self):
        with tempfile.TemporaryDirectory() as base:
            command = Path(base) / 'claude.cmd'
            command.write_text('@exit /b 0')
            with self.assertRaises(ClaudeError):
                discover_cli(str(command), {'PATH': '', 'USERPROFILE': base})

    def test_account_identity_distinguishes_equal_masked_emails_without_exposing_them(self):
        first = sanitize_status({'loggedIn': True, 'authMethod':'oauth', 'email':'person1@example.com', 'orgId':'one'})
        second = sanitize_status({'loggedIn': True, 'authMethod':'oauth', 'email':'person2@example.com', 'orgId':'one'})
        self.assertEqual(first['email'], second['email'])
        self.assertNotEqual(first['account_identity'], second['account_identity'])
        self.assertNotIn('person1', str(first))
        self.assertNotIn('account_identity', sanitize_status({'loggedIn':True,'authMethod':'oauth'}))


class ClaudeProtocolTests(unittest.TestCase):
    def test_bounded_utf8_jsonl(self):
        self.assertEqual(read_message(io.BytesIO('{"type":"hello","text":"한글"}\n'.encode()))['text'], '한글')
        with self.assertRaises(ProtocolError):
            read_message(io.BytesIO(b'x' * (MAX_LINE_BYTES + 1)))
        with self.assertRaises(ProtocolError):
            read_message(io.BytesIO(b'[]\n'))

    def test_unknown_fields_and_subagent_text_do_not_leak_into_output(self):
        self.assertEqual(normalize({'type': 'mystery', 'secret': 'private'}), [])
        self.assertEqual(normalize({'type': 'assistant', 'parent_tool_use_id': 'nested',
                         'message': {'content': [{'type': 'text', 'text': 'nested text'}]}}), [])
        init = normalize({'type': 'system', 'subtype': 'init', 'apiKey': 'private', 'model': 'opus'})
        self.assertNotIn('private', str(init))

    def test_compaction_boundary_does_not_fabricate_a_summary(self):
        event = normalize({'type': 'system', 'subtype': 'compact_boundary'})[0]
        self.assertFalse(event['summary_available'])
        self.assertNotIn('summary', event)

    def test_malformed_compact_summary_text_is_ignored(self):
        message = {'type':'user','isCompactSummary':True,
                   'message':{'content':[{'type':'text','text':123}, {'type':'text','text':None}]}}
        self.assertFalse(any(event['kind'] == 'compact' for event in normalize(message)))
        message['message']['content'].append({'type':'text','text':'Valid summary.'})
        self.assertEqual(normalize(message)[0]['summary'], 'Valid summary.')

    def test_oversized_compact_hint_preserves_boundary_without_truncated_summary(self):
        for text in ('한' * 7000, '\ud800'):
            event = normalize({'type':'user','isCompactSummary':True,'message':{'content':text}})[0]
            self.assertEqual(event['kind'], 'compact')
            self.assertFalse(event['summary_available'])
            self.assertNotIn('summary', event)

    def test_unexpected_nested_shapes_are_ignored(self):
        self.assertEqual(normalize({'type':'assistant','message':42}), [])
        self.assertEqual(normalize({'type':'stream_event','event':'bad'}), [])
        with self.assertRaises(ProtocolError):
            read_message(io.BytesIO('{"type":"hello"}\n'.encode('utf-16')))

    def test_plan_and_direct_model_approval_calls_fail_closed(self):
        bridge = PermissionBridge.__new__(PermissionBridge)
        bridge.mode = 'plan'
        self.assertEqual(bridge.ask({'tool_name':'Bash','input':{'command':'delete'}})['behavior'], 'deny')
        self.assertEqual(bridge.ask({'tool_name':'ExitPlanMode','input':{}})['behavior'], 'deny')
        self.assertEqual(bridge.ask({'tool_name':'mcp__codex_bridge__approve','input':{}})['behavior'], 'deny')


class ClaudeRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'repository'
        self.root.mkdir()
        self.local = self.base / 'outside-repository'
        self.profile = str(uuid4())
        self.fake = self.base / 'fake_claude.py'
        self.fake.write_text(FAKE, encoding='utf-8')
        self.environment = patch.dict(os.environ, {'LOCALAPPDATA': str(self.local)})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def snapshot(self, turns):
        return {'turn_ids': turns, 'fingerprint': digest(turns),
                'turn_fingerprints': {turn: digest(turn) for turn in turns}}

    def run_fake(self, scenario='success', prior=None, turn='turn-1', permission='none', mode=None,
                 mutate_hello=None, commit=True, profile=None, account=None):
        incoming = Incoming()
        events = []
        hello = dict(type='hello', protocol=1, thread_id='same-task', turn_id=turn,
                     claude_profile_id=profile or self.profile, cwd=str(self.root), trusted_cwd=True,
                     snapshot=self.snapshot(prior or []))
        if mutate_hello:
            hello.update(mutate_hello)
        incoming.send(hello)
        def on_output(message):
            events.append(message)
            if message['type'] == 'ready':
                chosen = mode or ('resume' if message['session'] else 'fresh')
                incoming.send(dict(type='run', mode=chosen, blocks=[{'kind':'request','text':'안녕하세요'}],
                                   permission_mode='acceptEdits', permission_prompts=permission))
            elif message['type'] == 'permission_request':
                incoming.send(dict(type='permission_decision', id=message['id'], behavior='allow', remember='once'))
            elif message['type'] == 'agent_request':
                incoming.send(dict(type='agent_response', id=message['id'], result={'native_result':message['name']}))
            elif scenario == 'wait' and message.get('kind') == 'text_delta':
                incoming.send({'type': 'interrupt'})
            elif scenario == 'compact_cancel' and message.get('kind') == 'notice' and 'Generating a portable' in message.get('message',''):
                incoming.send({'type': 'interrupt'})
            elif message['type'] == 'done':
                if message['status'] == 'success' and commit:
                    incoming.send(dict(type='commit', snapshot=self.snapshot((prior or []) + [turn])))
                else:
                    incoming.send(None)
        output = Outgoing(on_output)
        with patch.dict(os.environ, {'CLAUDE_FIXTURE_SCENARIO': scenario}):
            code = serve(self.root, profile or self.profile, incoming, output,
                         _test_command=[sys.executable, str(self.fake)],
                         _test_auth={'logged_in': True, 'method':'claude.ai', 'cli_version':'2.1.282',
                                     'account_identity':digest(account or profile or self.profile)},
                         _test_settings={})
        self.assertNotIn('PRIVATE_', output.getvalue().decode())
        return code, events

    def run_borrowed(self, scenario, expires_in, updates=(), announce=True, turn='turn-1', prior=(),
                     auth_resume=False):
        """Run with a lent dummy token; `updates` answer each auth_refresh in order."""
        context = BorrowedContext(self.root, [sys.executable, str(self.fake)], self.profile,
                                  int(time.time()) + expires_in)
        incoming, events, replies = Incoming(), [], list(updates)
        hello = dict(type='hello', protocol=1, thread_id='borrowed-task', turn_id=turn,
                     claude_profile_id=self.profile, cwd=str(self.root), trusted_cwd=True,
                     snapshot=self.snapshot(list(prior)))
        if announce:
            hello['auth_refresh'] = 1
        if auth_resume:
            hello['auth_resume'] = 1
        incoming.send(hello)
        def on_output(message):
            events.append(message)
            if message['type'] == 'ready':
                incoming.send(dict(type='run', mode='resume' if message['session'] else 'fresh',
                                   blocks=[{'kind':'request','text':'fixture request'}],
                                   permission_mode='acceptEdits', permission_prompts='none'))
            elif message['type'] == 'auth_refresh':
                reply = replies.pop(0)
                incoming.send(reply(context) if callable(reply) else reply)
            elif scenario == 'wait' and message.get('kind') == 'text_delta':
                incoming.send(dict(type='auth_update', available=False))
            elif message['type'] == 'done':
                incoming.send(dict(type='commit', snapshot=self.snapshot(list(prior) + [turn]))
                              if message['status'] == 'success' else None)
        output = Outgoing(on_output)
        trace = self.base / ('trace-' + str(uuid4()) + '.jsonl')
        with patch.dict(os.environ, {'CLAUDE_FIXTURE_SCENARIO': scenario, 'CLAUDE_FIXTURE_TRACE': str(trace)}), \
             patch('manager_core.claude_runner.AUTH_REFRESH_RETRY_SECONDS', .2):
            code = serve(self.root, self.profile, incoming, output, execution_context=context)
        # A lent token never reaches native events, notices or the session ledger.
        self.assertNotIn('dummy-token', output.getvalue().decode())
        for path in context.ledger_directory.rglob('*'):
            if path.is_file():
                self.assertNotIn(b'dummy-token', path.read_bytes())
        launches = [json.loads(line) for line in trace.read_text(encoding='utf-8').splitlines()] if trace.exists() else []
        return code, events, launches

    def test_expired_borrowed_token_is_renewed_and_the_same_session_resumes_once(self):
        code, events, launches = self.run_borrowed('auth_expired', 45, [lambda context: context.renewal()])
        self.assertEqual(code, 0, events)
        done = next(event for event in events if event['type'] == 'done')
        self.assertEqual((done['status'], done['result_text']), ('success', '안녕하세요'))
        self.assertEqual(events[-1]['type'], 'committed')
        self.assertEqual(sum(event['type'] == 'auth_refresh' for event in events), 1)
        self.assertEqual([(launch['token'], launch['resume']) for launch in launches],
                         [('dummy-token-1', False), ('dummy-token-2', True)])
        self.assertEqual(launches[0]['session'], launches[1]['session'])
        self.assertEqual(launches[1]['session'], done['session_id'])
        continuation = launches[1]['request']['message']['content'][0]['text']
        self.assertIn('stopped because the Claude login expired', continuation)
        self.assertNotIn('fixture request', json.dumps(launches[1]['request']))
        # The CLI's API-error text is a notice, never shared assistant history.
        self.assertFalse(any('Failed to authenticate' in event.get('text', '') for event in events), events)
        notices = [event['message'] for event in events if event.get('kind') == 'notice']
        self.assertIn('Claude could not authenticate its request.', notices)
        self.assertTrue(any('renewing it and resuming' in notice for notice in notices), notices)
        # Usage covers both launches; the session cost stays cumulative.
        self.assertEqual(done['usage']['input_tokens'], 11)
        self.assertAlmostEqual(done['turn_cost_usd'], 0.02)
        ledger = next(self.root.glob('remote-state/sessions/*/*/*.json'))
        self.assertFalse(json.loads(ledger.read_text(encoding='utf-8'))['dirty'])

    def test_second_auth_failure_ends_the_turn_without_a_third_launch(self):
        code, events, launches = self.run_borrowed('auth_always', 45, [lambda context: context.renewal()])
        done = next(event for event in events if event['type'] == 'done')
        self.assertEqual(done['status'], 'error')
        # The API reports the renewed token as expired too; it is still not renewed again.
        self.assertEqual(done['error']['code'], 'claude_auth_expired')
        self.assertEqual(len(launches), 2)
        self.assertEqual(sum(event['type'] == 'auth_refresh' for event in events), 1)

    def test_rejected_or_unannounced_renewal_reports_a_specific_auth_error(self):
        for scenario, expires_in, announce, expected in (('auth_rejected', 3600, True, 'claude_auth_rejected'),
                                                         ('auth_expired', 45, False, 'claude_auth_expired')):
            with self.subTest(scenario=scenario, announce=announce):
                code, events, launches = self.run_borrowed(scenario, expires_in, announce=announce)
                done = next(event for event in events if event['type'] == 'done')
                self.assertEqual((done['status'], done['error']['code']), ('error', expected))
                self.assertEqual(done['error']['message'], AUTH_FAILURES[expected])
                self.assertFalse(any(event['type'] == 'auth_refresh' for event in events))
                self.assertEqual(len(launches), 1)

    def test_local_login_failure_asks_to_sign_in_again(self):
        code, events = self.run_fake('auth_always')
        done = next(event for event in events if event['type'] == 'done')
        self.assertEqual(done['error']['code'], 'claude_login_required')
        self.assertFalse(any(event['type'] == 'auth_refresh' for event in events))
        self.assertFalse(any('Failed to authenticate' in event.get('text', '') for event in events), events)

    def test_other_api_errors_become_notices_with_the_generic_error(self):
        code, events = self.run_fake('api_error')
        done = next(event for event in events if event['type'] == 'done')
        self.assertEqual(done['error']['code'], 'claude_error')
        self.assertFalse(any(event.get('kind') == 'text' for event in events), events)
        self.assertIn('Claude API request failed: API Error: 529 Overloaded',
                      [event.get('message') for event in events if event.get('kind') == 'notice'])

    def test_a_token_that_is_not_newer_is_requested_once_more(self):
        stale = lambda context: context.renewal(expiresAt=context.expires_at)
        code, events, launches = self.run_borrowed('auth_expired', 45, [stale, stale])
        done = next(event for event in events if event['type'] == 'done')
        self.assertEqual(done['error']['code'], 'claude_auth_expired')
        self.assertEqual(sum(event['type'] == 'auth_refresh' for event in events), 2)
        self.assertEqual(len(launches), 1)
        code, events, launches = self.run_borrowed('auth_expired', 45, [
            dict(type='auth_update', available=False), lambda context: context.renewal()])
        self.assertEqual(code, 0, events)
        self.assertEqual(sum(event['type'] == 'auth_refresh' for event in events), 2)
        self.assertEqual([launch['token'] for launch in launches], ['dummy-token-1', 'dummy-token-2'])

    def test_renewal_for_another_login_or_malformed_token_is_refused(self):
        for change in (dict(accountIdentity=digest('another-account')), dict(profileId=str(uuid4())),
                       dict(token='dummy token'), dict(expires_in=120)):
            with self.subTest(change=change):
                code, events, launches = self.run_borrowed(
                    'auth_expired', 45, [lambda context: context.renewal(**change)] * 2)
                done = next(event for event in events if event['type'] == 'done')
                self.assertEqual(done['error']['code'], 'claude_auth_expired')
                self.assertEqual(len(launches), 1)

    def test_interrupt_while_waiting_for_a_renewal_stops_the_turn(self):
        code, events, launches = self.run_borrowed('auth_expired', 45, [{'type': 'interrupt'}])
        done = next(event for event in events if event['type'] == 'done')
        self.assertEqual(done['status'], 'interrupted')
        self.assertNotIn('error', done)
        self.assertEqual(len(launches), 1)

    def test_unsolicited_auth_update_is_a_protocol_error(self):
        code, events, launches = self.run_borrowed('wait', 3600)
        done = next(event for event in events if event['type'] == 'done')
        self.assertEqual(done['error']['code'], 'protocol')

    def test_checkpoint_after_a_renewal_uses_the_renewed_login(self):
        code, events, launches = self.run_borrowed('auth_compact', 45, [lambda context: context.renewal()])
        self.assertEqual(code, 0, events)
        self.assertTrue(any(event.get('deliberate_full_context_summary') for event in events), events)
        self.assertEqual([(launch['token'], launch['checkpoint']) for launch in launches],
                         [('dummy-token-1', False), ('dummy-token-2', False), ('dummy-token-2', True)])

    def test_the_api_expiry_reason_outweighs_a_skewed_lender_clock(self):
        # By this host's clock the lent token has an hour left; the API says it expired.
        code, events, launches = self.run_borrowed('auth_expired', 3600, [lambda context: context.renewal()])
        self.assertEqual(code, 0, events)
        self.assertEqual(next(event for event in events if event['type'] == 'done')['status'], 'success')
        self.assertEqual([launch['token'] for launch in launches], ['dummy-token-1', 'dummy-token-2'])
        self.assertTrue(token_expired({'api_error_code': 'token_expired'}, 'Failed to authenticate.'))
        self.assertTrue(token_expired({}, 'Failed to authenticate. API Error: 401 OAuth token has expired. Retry.'))
        self.assertFalse(token_expired({}, 'Failed to authenticate. API Error: 401 Invalid bearer token'))
        self.assertFalse(token_expired({'api_error_code': 'token_revoked'}, None))

    def test_open_tool_calls_of_the_stopped_process_are_closed_before_it_resumes(self):
        code, events, launches = self.run_borrowed('auth_tool', 45, [lambda context: context.renewal()])
        self.assertEqual(code, 0, events)
        kinds = [(event.get('kind') or event['type'], event.get('id')) for event in events]
        closed = next(event for event in events if event.get('kind') == 'tool_end')
        self.assertEqual(closed, dict(type='event', kind='tool_end', id='bg-tool-1', output=AUTH_TOOL_STOPPED,
                                      is_error=True))
        self.assertLess(kinds.index(('tool_start', 'bg-tool-1')), kinds.index(('tool_end', 'bg-tool-1')))
        self.assertLess(kinds.index(('tool_end', 'bg-tool-1')), kinds.index(('auth_refresh', None)))
        self.assertEqual(next(event for event in events if event['type'] == 'done')['status'], 'success')

    def test_a_resumed_launch_reports_that_it_waits_for_background_work(self):
        code, events, launches = self.run_borrowed('auth_background', 45, [lambda context: context.renewal()])
        self.assertEqual(code, 0, events)
        done = next(event for event in events if event['type'] == 'done')
        self.assertEqual((done['status'], done['result_text']), ('success', 'Agent finished.'))
        self.assertEqual(sum('waiting for its background workflow' in event.get('message', '') for event in events), 1)

    def test_an_expired_login_is_renewed_once_for_the_portable_checkpoint(self):
        code, events, launches = self.run_borrowed('auth_checkpoint', 3600, [lambda context: context.renewal()])
        self.assertEqual(code, 0, events)
        self.assertTrue(any(event.get('deliberate_full_context_summary') for event in events), events)
        self.assertEqual(sum(event['type'] == 'auth_refresh' for event in events), 1)
        self.assertEqual([(launch['token'], launch['checkpoint']) for launch in launches],
                         [('dummy-token-1', False), ('dummy-token-1', True), ('dummy-token-2', True)])
        self.assertFalse(self.borrowed_record()['dirty'])
        # A host that cannot renew keeps the failed checkpoint's fresh-session rule.
        code, events, launches = self.run_borrowed('auth_checkpoint', 3600, announce=False)
        self.assertEqual(next(event for event in events if event['type'] == 'done')['status'], 'success')
        self.assertFalse(any(event['type'] == 'auth_refresh' for event in events))
        self.assertEqual(len(launches), 2)
        self.assertTrue(self.borrowed_record()['dirty'])

    def test_a_second_renewal_request_waits_out_the_host_cooldown_from_the_first(self):
        borrowed = BorrowedLogin(dict(profileId=self.profile, accountIdentity=digest('remote-account'),
                                      expiresAt=int(time.time()) + 45))
        renewal = dict(type='auth_update', available=True, credentials=dict(
            profileId=self.profile, accessToken='dummy-token-2', accountIdentity=digest('remote-account'),
            expiresAt=int(time.time()) + 8 * 3600))
        for delay, low, high in ((0, .8, 1.6), (1.2, 1.1, 1.9)):
            with self.subTest(delay=delay):
                events, sent = queue.Queue(), []
                def emit(message, delay=delay):
                    sent.append(time.monotonic())
                    reply = dict(type='auth_update', available=False) if len(sent) == 1 else renewal
                    if len(sent) == 1 and delay:
                        threading.Timer(delay, events.put, args=(('host', reply),)).start()
                    else:
                        events.put(('host', reply))
                with patch('manager_core.claude_runner.AUTH_REFRESH_RETRY_SECONDS', 1.0):
                    token, outcome = renew_login(borrowed, events, emit, None, None)
                self.assertEqual((outcome, len(sent)), ('renewed', 2))
                self.assertIsNotNone(token)
                # A quick refusal still waits for the window; a slow one is retried at once.
                self.assertTrue(low <= sent[1] - sent[0] < high, sent[1] - sent[0])
                borrowed.expires_at = int(time.time()) + 45

    def borrowed_record(self):
        return json.loads(next(self.root.glob('remote-state/sessions/*/*/*.json')).read_text(encoding='utf-8'))

    def test_a_clean_login_stop_resumes_the_same_session_with_a_note(self):
        code, events, stopped = self.run_borrowed('auth_expired', 45, announce=False, auth_resume=True)
        done = next(event for event in events if event['type'] == 'done')
        self.assertEqual((done['status'], done['error']['code']), ('error', 'claude_auth_expired'))
        record = self.borrowed_record()
        self.assertTrue(record['dirty'])
        self.assertEqual(record['auth_stop'], {'turn_id': 'turn-1', 'turn_fingerprints': {}})
        self.assertAlmostEqual(record['cost_total_usd'], 0.01)
        code, events, resumed = self.run_borrowed('success', 3600, turn='turn-2', prior=['turn-1'], auth_resume=True)
        self.assertEqual(code, 0, events)
        self.assertEqual(events[0]['session']['auth_stop']['turn_id'], 'turn-1')
        self.assertEqual([(launch['resume'], launch['session']) for launch in resumed],
                         [(True, stopped[0]['session'])])
        content = resumed[0]['request']['message']['content']
        self.assertEqual([block['text'] for block in content], [AUTH_STOP_NOTE, 'fixture request'])
        done = next(event for event in events if event['type'] == 'done')
        self.assertAlmostEqual(done['turn_cost_usd'], 0.01)
        # The commit covers the stopped turn by its fingerprint and clears the marker.
        record = self.borrowed_record()
        self.assertEqual((record['dirty'], record['covered'], 'auth_stop' in record), (False, ['turn-1', 'turn-2'], False))
        self.assertEqual(record['turn_fingerprints'], {'turn-1': digest('turn-1'), 'turn-2': digest('turn-2')})
        code, events, later = self.run_borrowed('success', 3600, turn='turn-3', prior=['turn-1', 'turn-2'],
                                                auth_resume=True)
        self.assertEqual(events[0]['session']['covered'], ['turn-1', 'turn-2'])
        self.assertNotEqual(later[0]['request']['message']['content'][0]['text'], AUTH_STOP_NOTE)

    def test_only_a_clean_login_stop_on_an_announcing_host_can_be_resumed(self):
        for scenario, options in (('auth_expired', dict(announce=False)),
                                  ('auth_no_result', dict(announce=False, auth_resume=True)),
                                  ('auth_expired', dict(updates=[{'type': 'interrupt'}], auth_resume=True))):
            with self.subTest(scenario=scenario, options=options):
                self.run_borrowed(scenario, 45, **options)
                record = self.borrowed_record()
                self.assertTrue(record['dirty'])
                self.assertNotIn('auth_stop', record)
        # A rejected token stops cleanly too; an older runtime still gets a fresh session.
        code, events, stopped = self.run_borrowed('auth_rejected', 3600, auth_resume=True)
        self.assertEqual(next(event for event in events if event['type'] == 'done')['error']['code'],
                         'claude_auth_rejected')
        self.assertEqual(self.borrowed_record()['auth_stop']['turn_id'], 'turn-1')
        code, events, fresh = self.run_borrowed('success', 3600, turn='turn-2', prior=['turn-1'])
        self.assertIsNone(events[0]['session'])
        self.assertFalse(fresh[0]['resume'])
        self.assertNotEqual(fresh[0]['session'], stopped[0]['session'])

    def test_ledger_resumes_a_login_stop_only_when_nothing_it_saw_changed(self):
        settings = dict(run_settings({}), account_identity='account-a')
        ledger = SessionLedger(self.root, self.profile, 'task', settings, self.root)
        stop = dict(turn_id='stopped', turn_fingerprints={'a': digest('a'), 'b': digest('b')})
        record = dict(id=str(uuid4()), covered=['a'], fingerprint=None, turn_fingerprints={'a': digest('a')},
                      dirty=True, settings=settings, auth_stop=stop)
        later = self.snapshot(['a', 'b', 'stopped', 'c'])
        ledger.save(record)
        self.assertEqual(ledger.load(later, 'next'), record)
        edited = self.snapshot(['a', 'b', 'stopped', 'c'])
        edited['turn_fingerprints']['b'] = digest('edited after the stop')
        for snapshot, current in ((later, None), (later, 'stopped'), (self.snapshot(['a', 'b', 'c']), 'next'),
                                  (self.snapshot(['b', 'stopped']), 'next'), (edited, 'next')):
            with self.subTest(turns=snapshot['turn_ids'], current=current):
                self.assertIsNone(ledger.load(snapshot, current))
        for change in (dict(auth_stop=None), dict(auth_stop='stopped'), dict(auth_stop=dict(stop, turn_id=1)),
                       dict(auth_stop=dict(stop, turn_fingerprints=['a', 'b'])),
                       dict(auth_stop=dict(stop, turn_fingerprints={'b': digest('b')})),
                       dict(turn_fingerprints={'a': digest('old answer')}),
                       dict(settings=dict(settings, account_identity='account-b')), dict(settings=None)):
            with self.subTest(change=change):
                ledger.save(dict(record, **change))
                self.assertIsNone(ledger.load(later, 'next'))

    def test_rate_limit_event_during_checkpoint_keeps_the_runner_alive(self):
        code, events = self.run_fake('compact_rate_limit')
        self.assertEqual(code, 0, events)
        self.assertEqual(events[-1]['type'], 'committed')
        self.assertTrue(any(event.get('deliberate_full_context_summary') for event in events), events)
        self.assertTrue(any(event.get('kind') == 'rate_limit' and event.get('status') == 'allowed_warning'
                            for event in events), events)

    def test_delegation_mcp_roundtrip_is_available_without_host_approval_mode(self):
        from manager_core.claude_delegation import TOOLS
        catalog = [{'name': name, 'description': 'Native fixture tool',
                    'inputSchema': {'type': 'object'}} for name in sorted(TOOLS)]
        code, events = self.run_fake(scenario='delegation', mutate_hello={'delegation_tools':catalog})
        self.assertEqual(code, 0, events)
        self.assertEqual([(event['name'], event['arguments']) for event in events if event['type'] == 'agent_request'],
                         [(name, {'fixture':name}) for name in sorted(TOOLS)])
        self.assertEqual(events[-1]['type'], 'committed')
        # Changing the admitted role/tool surface invalidates the official session cache.
        code, changed = self.run_fake(prior=['turn-1'], turn='turn-2')
        self.assertEqual(code, 0, changed)
        self.assertIsNone(next(event['session'] for event in changed if event['type'] == 'ready'))

    def test_portable_checkpoint_has_no_delegation_tools_or_extra_agent_calls(self):
        catalog = [{'name':'spawn_agent','description':'Native fixture tool','inputSchema':{'type':'object'}}]
        code, events = self.run_fake(scenario='compact',mutate_hello={'delegation_tools':catalog})
        self.assertEqual(code,0,events)
        self.assertFalse(any(event['type']=='agent_request' for event in events))
        self.assertTrue(any(event.get('deliberate_full_context_summary') for event in events),events)

    def test_legacy_commands_retain_builtin_delegation_without_a_native_catalog(self):
        selected=run_settings({})
        command=build_command(['claude'],str(uuid4()),False,selected,'auto','none',self.local,self.root,[])
        self.assertNotIn('--disallowedTools',command)
        self.assertNotIn('Agent',json.loads(command[command.index('--settings')+1])['permissions']['deny'])
        self.assertNotIn('Task',json.loads(command[command.index('--settings')+1])['permissions']['deny'])

    def test_managed_leaf_and_empty_preset_block_builtin_agents_without_exposing_mcp(self):
        code,events=self.run_fake(scenario='managed_leaf',mutate_hello={'delegation_tools':[],'managed_delegation':True})
        self.assertEqual(code,0,events)
        self.assertEqual(events[-1]['type'],'committed')
        self.assertFalse(any(event['type']=='agent_request' for event in events))
        code,events=self.run_fake(mutate_hello={'managed_delegation':'true'})
        self.assertEqual(code,1,events)
        self.assertEqual(events[-1]['type'],'refused')

    def test_stream_success_commits_utf8_and_resumes_only_same_account(self):
        code, first = self.run_fake()
        self.assertEqual(code, 0, first)
        done = next(e for e in first if e['type'] == 'done')
        self.assertEqual(done['status'], 'success')
        self.assertEqual(done['result_text'], '안녕하세요')
        self.assertEqual(first[-1]['type'], 'committed')
        code, second = self.run_fake(prior=['turn-1'], turn='turn-2')
        ready = second[0]
        self.assertEqual(ready['session']['id'], done['session_id'])
        self.assertEqual(ready['session']['covered'], ['turn-1'])
        code, other = self.run_fake(prior=['turn-1','turn-2'], turn='turn-3', profile=str(uuid4()))
        self.assertIsNone(other[0]['session'])

    def test_model_and_effort_switches_resume_one_session_with_exact_cli_arguments(self):
        trace = self.base / 'cli-arguments.jsonl'
        self.fake.write_text(FAKE.replace('request = json.loads(sys.stdin.readline())',
            'request = json.loads(sys.stdin.readline())\n' +
            f'with open({str(trace)!r}, "a", encoding="utf-8") as trace:\n' +
            '    trace.write(json.dumps({"session":session,"resume":"--resume" in args,'
            '"model":args[args.index("--model")+1],"effort":args[args.index("--effort")+1]})+"\\n")'), encoding='utf-8')
        turns, session = [], None
        selections = [('cc-opus', 'high'), ('cc-claude-opus-5-5', 'ultracode'),
                      ('cc-sonnet', 'max'), ('cc-sonnet', 'low')]
        for index, (model, effort) in enumerate(selections):
            turn = f'turn-{index + 1}'
            code, events = self.run_fake(prior=turns, turn=turn, mutate_hello={'model': model, 'effort': effort})
            self.assertEqual(code, 0, events)
            done = next(event for event in events if event['type'] == 'done')
            if session is None:
                session = done['session_id']
            self.assertEqual(done['session_id'], session)
            if turns:
                self.assertEqual(events[0]['session']['covered'], turns)
            turns.append(turn)
        calls = [json.loads(line) for line in trace.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(calls, [dict(session=session, resume=index > 0, model=model.removeprefix('cc-'), effort=effort)
                                 for index, (model, effort) in enumerate(selections)])
        ledgers = list((self.root / 'work/control-center/claude/sessions').glob('*/*/*.json'))
        self.assertEqual(len(ledgers), 1)
        record = json.loads(ledgers[0].read_text(encoding='utf-8'))
        self.assertEqual((record['model'], record['effort'], record['dirty']), ('sonnet', 'low', False))
        self.assertEqual(record['settings']['model'], 'sonnet')

    def test_ultracode_waits_for_its_background_workflow_and_reports_the_final_answer(self):
        for scenario in ('workflow', 'workflow_queued'):
            with self.subTest(scenario=scenario):
                code, events = self.run_fake(scenario, profile=str(uuid4()),
                                             mutate_hello={'model': 'cc-opus', 'effort': 'ultracode'})
                self.assertEqual(code, 0, events)
                done = next(event for event in events if event['type'] == 'done')
                self.assertEqual(done['status'], 'success')
                self.assertEqual(done['result_text'], 'Final answer from the workflow.')
                self.assertEqual((done['usage']['input_tokens'], done['usage']['output_tokens']), (30, 10))
                notices = [event['message'] for event in events if event.get('kind') == 'subagent']
                self.assertEqual(notices, ['Claude started a background workflow: review',
                                           'Claude background task completed: 3 findings'])
                self.assertEqual(events[-1]['type'], 'committed')

    def test_workflow_is_preapproved_only_for_ultracode_without_an_approval_surface(self):
        def permissions(effort, prompts):
            command = build_command(['claude'], str(uuid4()), False, run_settings({'reasoning_effort': effort}),
                                    'auto', prompts, self.local, self.root, [], mcp_path=self.base / 'mcp.json')
            return json.loads(command[command.index('--settings') + 1])['permissions']
        self.assertEqual(permissions('ultracode', 'none').get('allow'), ['Workflow'])
        self.assertNotIn('allow', permissions('ultracode', 'host'))
        self.assertNotIn('allow', permissions('xhigh', 'none'))

    def test_inactive_ultracode_is_reported_and_the_turn_still_completes(self):
        code, events = self.run_fake('ultracode_inactive', profile=str(uuid4()),
                                     mutate_hello={'model': 'cc-opus', 'effort': 'ultracode'})
        self.assertEqual(code, 0, events)
        self.assertTrue(any('Ultracode is not active' in event.get('message', '') for event in events), events)
        self.assertEqual(next(event for event in events if event['type'] == 'done')['status'], 'success')

    def test_model_switch_retains_compaction_and_interruption_invalidates_every_model(self):
        code, first = self.run_fake('compact')
        self.assertEqual(code, 0, first)
        session = next(event for event in first if event['type'] == 'done')['session_id']
        code, second = self.run_fake('wait', prior=['turn-1'], turn='turn-2',
                                     mutate_hello={'model': 'cc-sonnet', 'effort': 'ultracode'})
        self.assertEqual(second[0]['session']['id'], session)
        self.assertGreater(second[0]['session']['compactions'], 0)
        code, third = self.run_fake(prior=['turn-1', 'turn-2'], turn='turn-3',
                                    mutate_hello={'model': 'cc-opus', 'effort': 'high'})
        self.assertIsNone(third[0]['session'])
        self.assertNotEqual(next(event for event in third if event['type'] == 'done')['session_id'], session)

    def test_rate_limit_event_records_only_the_active_cli_account(self):
        with patch('manager_core.claude_usage.record_event') as record:
            code, events = self.run_fake('usage')
        self.assertEqual(code, 0, events)
        record.assert_called_once()
        self.assertEqual(record.call_args.args[1:], (self.profile, digest(self.profile), {
            'type': 'rate_limit_event', 'rate_limit_info': {
                'rateLimitType': 'five_hour', 'utilization': 0.25, 'resetsAt': 2000000000}}))

    def test_cancellation_marks_dirty_and_next_turn_starts_fresh(self):
        code, first = self.run_fake('wait')
        self.assertEqual(next(e for e in first if e['type'] == 'done')['status'], 'interrupted')
        code, second = self.run_fake(prior=['turn-1'], turn='turn-2')
        self.assertIsNone(second[0]['session'])

    def test_private_instruction_files_are_removed_on_success_cancel_and_error(self):
        for scenario in ('success', 'wait', 'control'):
            created = []
            def create(prefix):
                temporary = private_temporary_directory(prefix)
                created.append(Path(temporary.name))
                return temporary
            with self.subTest(scenario=scenario), patch(
                    'manager_core.claude_runner.private_temporary_directory', side_effect=create):
                self.run_fake(scenario, turn=scenario, mutate_hello={'system_prompt':'Private project marker.'})
                self.assertTrue(created)
                self.assertTrue(all(not path.exists() for path in created))

    def test_same_profile_changed_account_isolates_and_returning_account_resumes(self):
        code, first = self.run_fake(account='account-one')
        original = next(e['session_id'] for e in first if e['type'] == 'done')
        code, changed = self.run_fake(prior=['turn-1'], turn='turn-2', account='account-two')
        self.assertIsNone(changed[0]['session'])
        code, returned = self.run_fake(prior=['turn-1','turn-2'], turn='turn-3', account='account-one')
        self.assertEqual(returned[0]['session']['id'], original)
        self.assertEqual(returned[0]['session']['covered'], ['turn-1'])

    def test_missing_canonical_commit_never_advances_coverage(self):
        code, events = self.run_fake(commit=False)
        self.assertEqual(code, 1)
        self.assertEqual(events[-1]['code'], 'commit_missing')
        code, later = self.run_fake(prior=['turn-1'], turn='turn-2')
        self.assertIsNone(later[0]['session'])

    def test_untrusted_directory_is_refused_before_a_process_starts(self):
        with patch('manager_core.claude_runner.subprocess.Popen') as popen:
            code, events = self.run_fake(mutate_hello={'trusted_cwd': False})
        self.assertEqual(events[0]['code'], 'untrusted_cwd')
        popen.assert_not_called()

    def test_permission_mcp_roundtrip_uses_native_decision(self):
        code, events = self.run_fake('permission', permission='host')
        self.assertEqual(code, 0, events)
        self.assertTrue(any(event['type'] == 'permission_request' for event in events))
        result = next(event for event in events if event.get('kind') == 'tool_end')
        self.assertEqual(result['output'], 'allow')

    def test_unexpected_control_transport_and_oversized_messages_fail_closed(self):
        for scenario, expected in [('control', 'permissions'), ('oversize', 'protocol')]:
            with self.subTest(scenario=scenario):
                code, events = self.run_fake(scenario)
                done = next(event for event in events if event['type'] == 'done')
                self.assertEqual(done['status'], 'error')
                self.assertEqual(done['error']['code'], expected)

    def test_compaction_extracts_only_verified_current_session_summary(self):
        code, events = self.run_fake('compact')
        self.assertEqual(code, 0, events)
        summaries = [e for e in events if e.get('kind') == 'compact' and e.get('summary_available')]
        self.assertEqual(summaries[0]['summary'], 'Verified compact summary: changed README.')
        directory = config_dir(self.profile)
        (directory / '.credentials.json').write_text('DO NOT READ')
        with patch('pathlib.Path.open', wraps=Path.open, autospec=True) as opened:
            # Invalid IDs are rejected before any file is opened.
            with self.assertRaises(ValueError):
                compact_summary(directory, '../.credentials')
            opened.assert_not_called()

    def test_deliberate_checkpoint_has_proven_prior_coverage_and_aggregates_usage(self):
        code, events = self.run_fake('compact', prior=['previous'])
        self.assertEqual(code, 0, events)
        checkpoint = next(e for e in events if e.get('deliberate_full_context_summary'))
        self.assertEqual(checkpoint['covered_turn_fingerprints'], {'previous':digest('previous')})
        self.assertNotIn('turn-1', checkpoint['covered_turn_fingerprints'])
        done = next(e for e in events if e['type'] == 'done')
        self.assertEqual(done['result_text'], '안녕하세요')
        self.assertEqual(done['usage']['input_tokens'], 13)
        self.assertEqual(done['usage']['cache_read_input_tokens'], 150)
        self.assertEqual(done['checkpoint_usage']['input_tokens'], 3)
        self.assertAlmostEqual(done['turn_cost_usd'], 0.03)

    def test_oversized_optional_hint_still_generates_deliberate_checkpoint(self):
        self.fake.write_text(FAKE.replace("'Verified compact summary: changed README.'", repr('한' * 7000)), encoding='utf-8')
        code, events = self.run_fake('compact', prior=['previous'])
        self.assertEqual(code, 0, events)
        summaries = [event for event in events if event.get('summary_available')]
        self.assertEqual(len(summaries), 1)
        self.assertTrue(summaries[0]['deliberate_full_context_summary'])

    def test_malformed_optional_compaction_records_are_skipped(self):
        directory = config_dir(self.profile)
        session = str(uuid4())
        transcript = directory / 'projects' / 'fixture' / (session + '.jsonl')
        transcript.parent.mkdir(parents=True)
        records = [
            {'isCompactSummary':True, 'message':{'content':'Valid earlier summary.'}},
            {'isCompactSummary':True, 'message':['not an object']},
            {'isCompactSummary':True, 'message':{'content':[{'type':'text','text':None}]}},
            {'isCompactSummary':True, 'message':{'content':[{'type':'text','text':{'invalid':1}}]}},
        ]
        transcript.write_text('\n'.join(json.dumps(record) for record in records)+'\n', encoding='utf-8')
        self.assertEqual(compact_summary(directory, session), 'Valid earlier summary.')
        with transcript.open('a', encoding='utf-8') as output:
            output.write(json.dumps({'isCompactSummary':True,'message':{'content':'한' * 7000}})+'\n')
        self.assertIsNone(compact_summary(directory, session))

    def test_failed_checkpoint_preserves_answer_and_forces_next_session_fresh(self):
        code, events = self.run_fake('compact_failure')
        self.assertEqual(code, 0, events)
        self.assertFalse(any(e.get('deliberate_full_context_summary') for e in events))
        done = next(e for e in events if e['type'] == 'done')
        self.assertEqual(done['status'], 'success')
        self.assertEqual(done['result_text'], '안녕하세요')
        code, later = self.run_fake(prior=['turn-1'], turn='turn-2')
        self.assertIsNone(later[0]['session'])

    def test_interrupted_checkpoint_retains_primary_text_without_clean_ledger(self):
        code, events = self.run_fake('compact_cancel')
        done = next(e for e in events if e['type'] == 'done')
        self.assertEqual(done['status'], 'interrupted')
        self.assertTrue(any(e.get('kind') == 'text' and e.get('text') == '안녕하세요' for e in events))
        self.assertFalse(any(e.get('deliberate_full_context_summary') for e in events))
        self.assertIsNone(done['turn_cost_usd'])
        self.assertFalse(done['usage_complete'])
        code, later = self.run_fake(prior=['turn-1'], turn='turn-2')
        self.assertIsNone(later[0]['session'])

    def test_checkpoint_compaction_does_not_start_recursive_summary(self):
        code, events = self.run_fake('compact_nested')
        self.assertEqual(code, 0, events)
        self.assertEqual(sum(bool(e.get('deliberate_full_context_summary')) for e in events), 1)

    def test_ledger_rejects_changed_output_rollback_dirty_and_stable_settings_change(self):
        settings = run_settings({})
        ledger = SessionLedger(self.root, self.profile, 'task', settings, self.root)
        snapshot = self.snapshot(['a'])
        record = dict(id=str(uuid4()), covered=['a'], fingerprint=snapshot['fingerprint'],
                      turn_fingerprints=snapshot['turn_fingerprints'], dirty=False)
        ledger.save(record)
        self.assertIsNotNone(ledger.load(self.snapshot(['a','b'])))
        self.assertIsNone(ledger.load(self.snapshot([])))
        changed = self.snapshot(['a'])
        changed['turn_fingerprints']['a'] = digest('edited assistant output')
        self.assertIsNone(ledger.load(changed))
        self.assertIsNone(SessionLedger(self.root, self.profile, 'fork', settings, self.root).load(snapshot))
        self.assertIsNotNone(SessionLedger(self.root, self.profile, 'task', run_settings({'model':'sonnet', 'effort':'ultracode'}), self.root).load(snapshot))
        self.assertIsNone(SessionLedger(self.root, self.profile, 'task', run_settings({'context_window':200000}), self.root).load(snapshot))
        record['dirty'] = True
        ledger.save(record)
        self.assertIsNone(ledger.load(snapshot))

    def test_legacy_ledger_migration_uses_latest_compatible_settings_and_keeps_boundaries(self):
        selected = dict(run_settings({}), account_identity='account-a', instructions='project-a',
                        plugins=['plugin-a'], cli_version='2.1.282', system_prompt_hash='prompt-a')
        snapshot = self.snapshot(['a'])
        ledger = SessionLedger(self.root, self.profile, 'task', selected, self.root)
        record = dict(id=str(uuid4()), covered=['a'], fingerprint=snapshot['fingerprint'],
                      turn_fingerprints=snapshot['turn_fingerprints'], dirty=False, settings=selected,
                      compactions=2, cost_total_usd=.25)
        ledger.directory.mkdir(parents=True)
        old = ledger.directory / (digest(dict(settings=selected, cwd=str(self.root))) + '.json')
        old.write_text(json.dumps(record), encoding='utf-8')
        os.utime(old, ns=(1000000000, 1000000000))
        switched = dict(selected, model='sonnet', effort='ultracode')
        migrated = SessionLedger(self.root, self.profile, 'task', switched, self.root)
        self.assertEqual(migrated.load(snapshot), record)
        for key, value in [('account_identity', 'account-b'), ('instructions', 'project-b'),
                           ('plugins', ['plugin-b']), ('cli_version', 'other'), ('system_prompt_hash', 'prompt-b'),
                           ('context_window', 200000), ('auto_compact_percent', 90)]:
            with self.subTest(key=key):
                self.assertIsNone(SessionLedger(self.root, self.profile, 'task', dict(switched, **{key:value}), self.root).load(snapshot))
        self.assertIsNone(SessionLedger(self.root, self.profile, 'task', switched, self.base).load(snapshot))
        self.assertIsNone(SessionLedger(self.root, str(uuid4()), 'task', switched, self.root).load(snapshot))
        self.assertIsNone(SessionLedger(self.root, self.profile, 'fork', switched, self.root).load(snapshot))
        newer = ledger.directory / (digest(dict(settings=switched, cwd=str(self.root))) + '.json')
        newer_record = dict(record, id=str(uuid4()), settings=switched, dirty=True)
        newer.write_text(json.dumps(newer_record), encoding='utf-8')
        os.utime(newer, ns=(1000000000, 1000000000))
        self.assertIsNone(migrated.load(snapshot))
        os.utime(newer, ns=(2000000000, 2000000000))
        self.assertIsNone(migrated.load(snapshot))
        newer_record.update(dirty=False, turn_fingerprints={'a': 'edited-output'})
        newer.write_text(json.dumps(newer_record), encoding='utf-8')
        self.assertIsNone(migrated.load(snapshot))
        migrated.save(dict(record, dirty=True))
        newer.write_text(json.dumps(record), encoding='utf-8')
        self.assertIsNone(migrated.load(snapshot))

    def test_session_lock_excludes_other_bucket_concurrent_run(self):
        path = self.root / 'run.lock'
        with session_lock(path):
            with self.assertRaises(ClaudeError):
                with session_lock(path):
                    pass

    def test_compaction_threshold_never_silently_changes_requested_size(self):
        self.assertIsNone(run_settings({})['autocompact_tokens'])
        self.assertIsNone(run_settings({'auto_compact_percent':None})['autocompact_tokens'])
        self.assertEqual(run_settings({'context_window':200000, 'auto_compact_percent':85})['autocompact_tokens'], 170000)
        with self.assertRaises(ClaudeError):
            run_settings({'context_window':100000,'auto_compact_percent':85})

    def test_cli_receives_native_compaction_and_ultracode_without_max_alias(self):
        selected = run_settings({'reasoning_effort': 'ultracode'})
        command = build_command(['claude'], str(uuid4()), False, selected, 'plan', 'none',
                                self.local, self.root, [])
        self.assertEqual(command[command.index('--effort') + 1], 'ultracode')
        self.assertEqual(command[command.index('--autocompact') + 1], 'auto')
        self.assertIsNone(selected['context_window'])
        selected = run_settings({'context_window': 200000, 'auto_compact_percent': 85})
        command = build_command(['claude'], str(uuid4()), False, selected, 'plan', 'none',
                                self.local, self.root, [])
        self.assertEqual(command[command.index('--autocompact') + 1], '170000')

    def test_custom_compaction_on_auto_context_uses_recognized_model_window(self):
        self.assertEqual(run_settings({'model': 'opus', 'auto_compact_percent': 90})['autocompact_tokens'], 900000)
        self.assertEqual(run_settings({'model': 'cc-claude-opus-5-5', 'auto_compact_percent': 90})['autocompact_tokens'], 900000)
        self.assertEqual(automatic_context_window('claude-opus-4-6'), 200000)
        self.assertEqual(automatic_context_window('claude-custom'), 200000)
        for model in ('cc-arbitrary', 'claude-sonnet-5-5', 'claude-custom'):
            with self.subTest(model=model), self.assertRaises(ClaudeError):
                run_settings({'model': model})

    def test_effective_project_instructions_are_bounded_by_utf8_bytes(self):
        self.assertEqual(system_prompt_text('한글'), '한글')
        with self.assertRaises(ClaudeError):
            system_prompt_text('한' * 27000)
        with self.assertRaises(ClaudeError):
            system_prompt_text(None)


if __name__ == '__main__':
    unittest.main()
