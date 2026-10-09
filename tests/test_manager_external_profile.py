"""Main API profiles use common records with their own provider and no OAuth."""
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from control_center import ControlCenter
from manager_core.store import Store
from manager_core.providers import ProviderRegistry
from manager_core.external_profile import ExternalProfile


class ExternalProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=Store(self.root)
        self.providers=ProviderRegistry(self.root)
        saved=self.providers.save({'name':'API fixture','base_url':'https://example.invalid/v1','protocol':'responses'},
            {'wire_model_id':'external-test','reasoning_effort':'high'})
        self.mid=saved['model']['id']
        state=self.providers._read();state['models'][0]['capabilities']['verified']=True
        self.providers.path.write_text(json.dumps(state))
        self.control=ControlCenter.__new__(ControlCenter)
        self.control.store=self.store;self.control.providers=self.providers
        self.control.native_login=Mock()

    def test_add_requires_key_before_creating_external_profile(self):
        with patch.object(self.providers,'environment',side_effect=RuntimeError('missing key')):
            with self.assertRaises(RuntimeError):self.control.dispatch('profile.add',dict(alias='API',kind='external',model_id=self.mid))
        self.assertEqual(self.store.read()['profiles'],[])
        with patch.object(self.providers,'environment',return_value={'fixture':'key'}):
            profile=self.control.dispatch('profile.add',dict(alias='API',kind='external',model_id=self.mid))
        self.assertEqual(profile['auth_mode'],'external');self.assertEqual(profile['runtime_channel'],'managed')
        self.assertIsNone(profile['source_home']);self.assertIsNone(profile['usage_account_id'])
        self.control.native_login.prepare.assert_not_called()
        result=self.providers.generate(profile['home'],False,[],primary_model_id=self.mid)
        config=tomllib.loads((Path(profile['home'])/'config.toml').read_text())
        self.assertEqual(config['model'],'external-test')
        self.assertFalse(config['model_providers'][config['model_provider']]['requires_openai_auth'])
        self.assertEqual(config['subagent_model_provider_allowlist'],[])
        self.assertFalse(any(k.startswith('cc_gpt_') for k in config.get('agents', {})))
        self.assertEqual(result['primary']['model_id'],self.mid)
        self.assertFalse((Path(profile['home'])/'auth.json').exists())

    def test_shared_task_resume_and_turn_use_explicit_api_model_without_mutating_caller(self):
        binding={'model':'external-test','model_provider':'provider-test','reasoning_effort':'high'}
        adapter=ExternalProfile({'CODEX_MANAGER_PRIMARY_MODEL':json.dumps(binding)})
        for method in ['thread/start','thread/resume','thread/fork','turn/start']:
            original={'id':1,'method':method,'params':{'threadId':'same-task','model':'gpt-6-astra',
                'collaborationMode':{'mode':'default','settings':{'model':'gpt-6-astra','reasoning_effort':'low'}}}}
            result=adapter.request(original)
            self.assertEqual(result['params']['threadId'],'same-task')
            self.assertEqual(result['params']['model'],'external-test')
            self.assertEqual(original['params']['model'],'gpt-6-astra')
            if method=='turn/start':self.assertEqual(result['params']['collaborationMode']['settings']['model'],'external-test')
            else:self.assertEqual(result['params']['modelProvider'],'provider-test')
        self.assertIs(ExternalProfile({}).request(original),original)

    def test_ssh_prepare_and_rename_need_no_external_account_tool(self):
        profile=self.store.add_profile('Account')
        self.store.mutate(lambda d:self.store.profile(profile['id'],d).update(auth_mode='native',remote_bindings=[{'alias':'fixture-host'}]))
        self.control.remote=Mock();self.control.remote.prepare.return_value={'prepared':False,'status':'fixture'}
        result=self.control.dispatch('remote.prepare',dict(profile_id=profile['id'],alias='fixture-host'))
        self.assertEqual(result['status'],'fixture');self.control.remote.prepare.assert_called_once()
        self.control.dispatch('profile.rename',dict(profile_id=profile['id'],alias='Changed'))
        self.assertEqual(self.store.profile(profile['id'])['alias'],'Changed')

    def test_claude_automatic_context_clears_foreign_overrides_and_keeps_ultracode(self):
        binding = dict(model='cc-opus', model_provider='claude_code', agent_kind='claude_code',
                       reasoning_effort='ultracode', supported_reasoning_efforts=['high', 'ultracode'],
                       context_window=None, auto_compact_percent=None)
        adapter = ExternalProfile({'CODEX_MANAGER_PRIMARY_MODEL': json.dumps(binding)})
        original = {'method': 'thread/resume', 'params': {'model': 'gpt-6-astra', 'config': {
            'model_context_window': 200000, 'model_auto_compact_token_limit': 170000, 'approval_policy': 'never'}}}
        result = adapter.request(original)
        self.assertEqual(result['params']['config'], {
            'model_provider': 'claude_code', 'model_reasoning_effort': 'ultracode', 'approval_policy': 'never'})
        self.assertEqual(original['params']['config']['model_auto_compact_token_limit'], 170000)
        binding.update(context_window=200000, auto_compact_percent=85)
        adapter = ExternalProfile({'CODEX_MANAGER_PRIMARY_MODEL': json.dumps(binding)})
        explicit = adapter.request(original)['params']['config']
        self.assertEqual(explicit['model_context_window'], 200000)
        self.assertEqual(explicit['model_auto_compact_token_limit'], 170000)

    def test_claude_custom_percentage_with_auto_context_uses_model_metadata(self):
        binding = dict(model='cc-sonnet', model_provider='claude_code', agent_kind='claude_code',
                       reasoning_effort='high', context_window=None, auto_compact_percent=90)
        adapter = ExternalProfile({'CODEX_MANAGER_PRIMARY_MODEL': json.dumps(binding)})
        config = adapter.request({'method': 'thread/start'})['params']['config']
        self.assertNotIn('model_context_window', config)
        self.assertEqual(config['model_auto_compact_token_limit'], 900000)

    def test_claude_task_model_and_effort_selection_survive_all_request_routes(self):
        binding = dict(model='cc-opus', model_provider='claude_code', agent_kind='claude_code',
                       reasoning_effort='high', context_window=None, auto_compact_percent=None)
        adapter = ExternalProfile({'CODEX_MANAGER_PRIMARY_MODEL': json.dumps(binding)})
        for method in ('thread/start', 'thread/resume', 'thread/fork', 'turn/start', 'thread/settings/update'):
            for model in ('cc-sonnet', 'cc-fable', 'cc-claude-opus-5-5'):
                with self.subTest(method=method, model=model):
                    original = {'method': method, 'params': {'threadId': 'same-task',
                        'model': model, 'effort': 'ultracode'}}
                    result = adapter.request(original)['params']
                    self.assertEqual(result['model'], model)
                    self.assertEqual(result['threadId'], 'same-task')
                    if method.startswith('thread/') and method != 'thread/settings/update':
                        self.assertEqual(result['config']['model_reasoning_effort'], 'ultracode')
                        self.assertEqual(result['modelProvider'], 'claude_code')
                    else:
                        self.assertEqual(result['effort'], 'ultracode')
                    self.assertEqual(original['params'], {'threadId': 'same-task', 'model': model, 'effort': 'ultracode'})

    def test_claude_sparse_updates_do_not_reset_task_or_other_tasks(self):
        binding = dict(model='cc-opus', model_provider='claude_code', agent_kind='claude_code', reasoning_effort='high')
        adapter = ExternalProfile({'CODEX_MANAGER_PRIMARY_MODEL': json.dumps(binding)})
        adapter.request({'method': 'thread/settings/update', 'params': {'threadId': 'one', 'model': 'cc-sonnet', 'effort': 'low'}})
        for method in ('turn/start', 'thread/settings/update'):
            for task in ('one', 'two'):
                original = {'method': method, 'params': {'threadId': task, 'approvalPolicy': 'never'}}
                self.assertEqual(adapter.request(original), original)
            result = adapter.request({'method': method, 'params': {'threadId': 'one', 'effort': 'ultracode'}})
            self.assertEqual(result['params'], {'threadId': 'one', 'effort': 'ultracode'})

    def test_claude_collaboration_and_config_models_use_native_precedence(self):
        binding = dict(model='cc-opus', model_provider='claude_code', agent_kind='claude_code', reasoning_effort='high')
        adapter = ExternalProfile({'CODEX_MANAGER_PRIMARY_MODEL': json.dumps(binding)})
        original = {'method': 'turn/start', 'params': {'model': 'cc-opus', 'effort': 'low', 'collaborationMode': {
            'mode': 'plan', 'settings': {'model': 'cc-sonnet', 'reasoning_effort': 'ultracode',
                                       'developer_instructions': 'Keep the plan.'}}}}
        result = adapter.request(original)['params']
        self.assertEqual(result['model'], 'cc-sonnet')
        self.assertEqual(result['effort'], 'ultracode')
        self.assertEqual(result['collaborationMode'], original['params']['collaborationMode'])
        self.assertEqual(original['params']['model'], 'cc-opus')
        result = adapter.request({'method': 'thread/resume', 'params': {
            'config': {'model': 'cc-claude-opus-5-5', 'model_reasoning_effort': 'low'}}})['params']
        self.assertEqual(result['model'], 'cc-claude-opus-5-5')
        self.assertEqual(result['config']['model_reasoning_effort'], 'low')

    def test_claude_unapproved_model_does_not_escape_profile(self):
        binding = dict(model='cc-opus', model_provider='claude_code', agent_kind='claude_code', reasoning_effort='high')
        adapter = ExternalProfile({'CODEX_MANAGER_PRIMARY_MODEL': json.dumps(binding)})
        for model in ('gpt-6-astra', 'cc-claude-sonnet-5-5', 'cc-arbitrary', ['cc-sonnet'], {'model': 'cc-sonnet'}):
            result = adapter.request({'method': 'turn/start', 'params': {'model': model, 'effort': 'low'}})['params']
            self.assertEqual(result, {'model': 'cc-opus', 'effort': 'high'})

    def test_claude_choices_respect_the_generated_catalog_subset(self):
        binding = dict(model='cc-opus', model_provider='claude_code', agent_kind='claude_code',
                       reasoning_effort='high', supported_models={'cc-opus': ['high'], 'cc-sonnet': ['low', 'high']})
        adapter = ExternalProfile({'CODEX_MANAGER_PRIMARY_MODEL': json.dumps(binding)})
        result = adapter.request({'method': 'turn/start', 'params': {'model': 'cc-fable', 'effort': 'low'}})
        self.assertEqual(result['params'], {'model': 'cc-opus', 'effort': 'high'})
        result = adapter.request({'method': 'turn/start', 'params': {'model': 'cc-sonnet', 'effort': 'ultracode'}})
        self.assertEqual(result['params'], {'model': 'cc-sonnet', 'effort': 'high'})

    def test_remote_api_bridge_routes_without_chatgpt_login(self):
        from manager_core.proxy_auth import AuthProxy
        binding={'model':'external-test','model_provider':'provider-test','reasoning_effort':'high'}
        auth=ExternalProfile({'CODEX_MANAGER_PRIMARY_MODEL':json.dumps(binding)}).bind_auth(AuthProxy())
        self.assertFalse(auth.bound);self.assertEqual(auth.state,'ready')
        result=auth.process('frontend',{'id':1,'method':'thread/resume','params':{'threadId':'same-task','modelProvider':'openai'}})
        self.assertEqual(result.runtime[0]['params']['modelProvider'],'provider-test')
        self.assertEqual(result.frontend,[])
