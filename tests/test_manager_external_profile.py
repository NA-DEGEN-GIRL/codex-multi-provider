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
        self.control.native_login=Mock();self.control.remote_accounts=Mock()
        self.control.remote_accounts.sync.side_effect=AssertionError('llm-usage must not be consulted')

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

    def test_ssh_prepare_and_rename_do_not_require_llm_usage(self):
        profile=self.store.add_profile('Account')
        self.store.mutate(lambda d:self.store.profile(profile['id'],d).update(auth_mode='native',remote_bindings=[{'alias':'fixture-host'}]))
        self.control.remote=Mock();self.control.remote.prepare.return_value={'prepared':False,'status':'fixture'}
        result=self.control.dispatch('remote.prepare',dict(profile_id=profile['id'],alias='fixture-host'))
        self.assertEqual(result['status'],'fixture');self.control.remote.prepare.assert_called_once()
        self.control.dispatch('profile.rename',dict(profile_id=profile['id'],alias='Changed'))
        self.control.remote_accounts.sync.assert_not_called()

    def test_remote_api_bridge_routes_without_chatgpt_or_llm_usage(self):
        from manager_core.proxy_auth import AuthProxy
        binding={'model':'external-test','model_provider':'provider-test','reasoning_effort':'high'}
        auth=ExternalProfile({'CODEX_MANAGER_PRIMARY_MODEL':json.dumps(binding)}).bind_auth(AuthProxy())
        self.assertFalse(auth.bound);self.assertEqual(auth.state,'ready')
        result=auth.process('frontend',{'id':1,'method':'thread/resume','params':{'threadId':'same-task','modelProvider':'openai'}})
        self.assertEqual(result.runtime[0]['params']['modelProvider'],'provider-test')
        self.assertEqual(result.frontend,[])
