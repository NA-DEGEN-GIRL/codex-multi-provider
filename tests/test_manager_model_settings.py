import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core import model_settings
from manager_core.providers import ProviderRegistry
from manager_core.external_profile import ExternalProfile
from manager_core.store import Store
from control_center import ControlCenter


class ModelSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.registry=ProviderRegistry(self.root)
        self.saved=self.registry.save({'name':'Fixture','base_url':'https://fixture.invalid','protocol':'responses'},
            {'wire_model_id':'deepseek-flash','reasoning_effort':'max','capabilities':{'verified':True}})
        self.mid=self.saved['model']['id']

    def test_per_profile_window_compact_and_picker_match(self):
        result=self.registry.render_for_host(self.root,False,[],primary_model_id=self.mid,
            primary_settings={'reasoning_effort':'ultra','context_window':1048576,'auto_compact_percent':85})
        config=tomllib.loads(result['files']['config.toml'])
        self.assertEqual((config['model_reasoning_effort'],config['model_context_window'],config['model_auto_compact_token_limit']),('max',1048576,891289))
        catalog=json.loads(next(v for k,v in result['files'].items() if k.startswith('catalogs/primary')))
        model=catalog['models'][0]
        self.assertEqual(model['auto_compact_token_limit'],891289)
        self.assertEqual(model['context_window'],1048576)
        self.assertEqual(model['default_reasoning_level'],'max')
        adapter=ExternalProfile({'CODEX_MANAGER_PRIMARY_MODEL':json.dumps(result['primary'])})
        request=adapter.request({'method':'turn/start','params':{'model':'deepseek-flash','effort':'medium'}})
        self.assertEqual(request['params']['effort'],'high')
        foreign=adapter.request({'method':'thread/resume','params':{'model':'gpt-6-astra','config':{'model_reasoning_effort':'low'}}})
        self.assertEqual(foreign['params']['config']['model_reasoning_effort'],'max')
        self.assertEqual(foreign['params']['config']['model_auto_compact_token_limit'],891289)
        self.assertEqual(self.registry.list()['models'][0]['settings_defaults']['auto_compact_percent'],90)

    def test_invalid_settings_do_not_write_any_files(self):
        before=self.registry.path.read_bytes()
        for value in [{'context_window':1048577},{'context_window':True},{'auto_compact_percent':91},{'auto_compact_percent':0},{'reasoning_effort':'bogus'}]:
            with self.assertRaises(ValueError):self.registry.render_for_host(self.root,False,[],primary_model_id=self.mid,primary_settings=value)
        self.assertEqual(self.registry.path.read_bytes(),before)

    def test_external_only_pool_is_explicit_and_multiple(self):
        other=self.registry.save(self.saved['provider'],{'wire_model_id':'second-model','reasoning_effort':'high','capabilities':{'verified':True,'context_window':128000,'reasoning_efforts':['low','high']}})
        mids=[self.mid,other['model']['id']]
        result=self.registry.render_for_host(self.root,True,mids,selection_mode='external_only')
        config=tomllib.loads(result['files']['config.toml'])
        self.assertEqual(config['subagent_model_selection'],'external_only')
        self.assertEqual(len(config['subagent_model_allowlist']),2)
        self.assertFalse(any(role.startswith('cc_gpt_') for role in config['agents']))
        normal=tomllib.loads(self.registry.render_for_host(self.root,True,mids,result['files']['config.toml'])['files']['config.toml'])
        self.assertEqual(normal['subagent_model_selection'],'automatic')
        self.assertIn('cc_gpt_astra',normal['agents'])
        with self.assertRaises(ValueError):self.registry.render_for_host(self.root,True,[],selection_mode='external_only')
        primary=self.registry.render_for_host(self.root,True,[other['model']['id']],
            primary_model_id=self.mid,selection_mode='external_only')
        roles=tomllib.loads(primary['files']['config.toml'])['agents']
        self.assertEqual(len(roles),1)
        self.assertNotIn(self.mid.replace('-',''),next(iter(roles)))

    def test_create_and_edit_preserve_profile_identity_and_other_profiles(self):
        control=ControlCenter.__new__(ControlCenter);control.store=Store(self.root);control.providers=self.registry
        control.instances=Mock();control.instances.prepare.return_value={};control.instances.observe.return_value={'status':'running'}
        control.restarts=Mock();control.restarts.schedule.return_value={'state':'waiting'}
        settings={'reasoning_effort':'low','context_window':1000000,'auto_compact_percent':85}
        with patch.object(self.registry,'environment',return_value={}):
            first=control.dispatch('profile.add',{'alias':'API1','kind':'external','model_id':self.mid,'settings':settings})
            second=control.dispatch('profile.add',{'alias':'API2','kind':'external','model_id':self.mid})
        control.dispatch('profile.model_settings',{'profile_id':first['id'],'settings':{**settings,'reasoning_effort':'max'}})
        self.assertEqual(control.store.profile(first['id'])['external_settings']['reasoning_effort'],'max')
        self.assertEqual(control.store.profile(second['id']),second)
        control.restarts.schedule.assert_called_once_with(first['id'])
        self.assertEqual(control.store.profile(first['id'])['external_model_id'],self.mid)


if __name__=='__main__':unittest.main()
