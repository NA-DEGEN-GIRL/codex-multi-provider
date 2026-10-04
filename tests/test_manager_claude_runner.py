"""Offline CLI adapter tests. These never log in or call an Anthropic model."""
import io
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
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
                                       private_temporary_directory, build_command)


FAKE = r'''
import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
session = args[args.index('--resume') + 1] if '--resume' in args else args[args.index('--session-id') + 1]
scenario = os.environ.get('CLAUDE_FIXTURE_SCENARIO', 'success')
def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)
request = json.loads(sys.stdin.readline())
if 'CODEX_PORTABLE_CHECKPOINT_V1' in json.dumps(request):
    assert '--resume' in args and '--plugin-dir' not in args and '--permission-prompt-tool' not in args
    assert args[args.index('--tools')+1] == '' and args[args.index('--disallowedTools')+1] == '*'
    assert args[args.index('--max-turns')+1] == '1' and args[args.index('--permission-prompts')+1] == 'none'
    assert '--safe-mode' in args and '--strict-mcp-config' in args
    assert json.loads(args[args.index('--mcp-config')+1]) == {'mcpServers':{}}
    emit({'type':'system','subtype':'init','session_id':session,'model':'fixture','mcp_servers':[]})
    if scenario == 'compact_nested':
        emit({'type':'system','subtype':'compact_boundary','compact_metadata':{'trigger':'auto'}})
    if scenario != 'compact_cancel':
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
        if scenario in ('compact','compact_failure','compact_cancel','compact_nested'):
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
