import copy
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core import model_settings
from manager_core.local_models import apply_preset
from manager_core.providers import ProviderRegistry
from manager_core.external_profile import ExternalProfile


class LocalModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.registry = ProviderRegistry(Path(self.temp.name))

    def model(self, preset, wire='served-checkpoint'):
        result = self.registry.save(dict(name='Local fixture', base_url='http://127.0.0.1:9999/v1',
            deployment='local', auth_type='none', execution_scope='all_hosts'),
            dict(wire_model_id=wire, local_preset_id=preset))
        # Synthetic verification evidence, only in this isolated fixture store.
        state = self.registry._read()
        state['models'][0]['capabilities']['verified'] = True
        state['models'][0]['verified'] = True
        self.registry._write(state)
        return result['model']

    def test_qwen_max_maps_to_real_enum_for_both_roles(self):
        model = self.model('qwen3.8-flash-next')
        self.assertEqual(model['reasoning_effort'], 'xhigh')
        self.assertFalse(model['verified'])
        result = self.registry.render_for_host('/fixture/codex', True, [model['id']], primary_model_id=model['id'],
                                               primary_settings={'reasoning_effort': 'max'})
        config = tomllib.loads(result['files']['config.toml'])
        self.assertEqual(config['model_reasoning_effort'], 'xhigh')
        self.assertEqual(config['model_context_window'], 1000000)
        roles = [tomllib.loads(value) for name, value in result['files'].items() if name.startswith('agents/cc_external')]
        self.assertEqual(len(roles), 1)
        self.assertEqual(roles[0]['model_reasoning_effort'], 'xhigh')
        self.assertEqual(roles[0]['model_context_window'], 1000000)
        for name, value in result['files'].items():
            if name.startswith('catalogs/'):
                catalog = json.loads(value)['models'][0]
                self.assertEqual(catalog['context_window'], 1000000)
                self.assertEqual(catalog['effective_context_window_percent'], 100)
                self.assertEqual(catalog['auto_compact_token_limit'], 900000)
        binding = ExternalProfile({'CODEX_MANAGER_PRIMARY_MODEL': json.dumps(result['primary'])})
        message = binding.request({'method': 'turn/start', 'params': {'model': model['wire_model_id'], 'effort': 'max'}})
        self.assertEqual(message['params']['effort'], 'xhigh')

    def test_checkpoint_id_does_not_trigger_cloud_deepseek_aliases(self):
        model = self.model('deepseek-v4.1-flash', 'deepseek-v4.1-flash')
        self.assertEqual(model_settings.supported_efforts(model), ['max'])
        self.assertEqual(model_settings.context_limit(model), 1048576)
        self.assertEqual(model['reasoning_effort'], 'max')
        for effort in ['none', 'medium', 'high', 'ultra']:
            with self.assertRaises(ValueError):
                model_settings.normalize_effort(model, effort)

    def test_glm_full_context_not_silently_reduced_by_profile(self):
        model = self.model('glm-5.3-flash')
        self.assertEqual(model_settings.supported_efforts(model), ['low', 'high', 'max'])
        self.assertEqual(model_settings.resolve(model)['context_window'], 1048576)
        for lowered in [32768, 131072, 1000000]:
            with self.assertRaisesRegex(ValueError, '최대 컨텍스트'):
                model_settings.configured(model, {'context_window': lowered})
        invalid = copy.deepcopy(model)
        del invalid['capabilities']['published_max_context']
        with self.assertRaises(ValueError):
            model_settings.context_limit(invalid)

    def test_invalid_server_capacity_and_cloud_preset_do_not_write(self):
        model = self.model('qwen3.8-flash-next')
        before = self.registry.path.read_bytes()
        state = self.registry.list()
        provider = state['providers'][0]
        for capabilities in [{'context_window': 262144}, {'server_context_window': 262144}, {'context_window': True}]:
            with self.assertRaises(ValueError):
                self.registry.save(provider, {**model, 'capabilities': capabilities})
        with self.assertRaises(ValueError):
            apply_preset({'local_preset_id': 'qwen3.8-flash-next'}, {'deployment': 'cloud'})
        self.assertEqual(self.registry.path.read_bytes(), before)

    def test_model_change_requires_fresh_verification(self):
        self.model('qwen3.8-flash-next')
        state = self.registry.list()
        old = state['models'][0]
        self.assertTrue(old['verified'])
        changed = self.registry.save(state['providers'][0], dict(id=old['id'], wire_model_id=old['wire_model_id'],
            local_preset_id='glm-5.3-flash', reasoning_effort='max'))['model']
        self.assertFalse(changed['verified'])
        self.assertEqual(changed['capabilities']['context_window'], 1048576)

    def test_clearing_preset_cannot_make_registry_unreadable(self):
        self.model('qwen3.8-flash-next')
        state = self.registry.list()
        before = self.registry.path.read_bytes()
        for empty in (None, ''):
            with self.assertRaises(ValueError):
                self.registry.save(state['providers'][0], {**state['models'][0], 'local_preset_id': empty})
            self.assertEqual(self.registry.path.read_bytes(), before)
            self.assertEqual(self.registry.list()['models'][0]['local_preset_id'], 'qwen3.8-flash-next')

    def test_unbound_local_capabilities_rejected_before_write(self):
        with self.assertRaises(ValueError):
            self.registry.save(dict(name='Unbound', base_url='http://localhost:9999/v1', deployment='local'),
                dict(wire_model_id='unbound', reasoning_effort='max', capabilities=dict(local_model=True,
                    context_window=1048576, published_max_context=1048576, reasoning_efforts=['max'])))
        self.assertFalse(self.registry.path.exists())

    def test_provider_edit_validates_every_bound_model(self):
        self.model('qwen3.8-flash-next')
        provider = self.registry.list()['providers'][0]
        generic = self.registry.save(provider, dict(wire_model_id='generic', reasoning_effort='high',
                                                   capabilities={'context_window': 1000000}))['model']
        before = self.registry.path.read_bytes()
        with self.assertRaises(ValueError):
            self.registry.save({**provider, 'deployment': 'cloud', 'auth_type': 'api_key',
                                'base_url': 'https://fixture.invalid/v1'}, generic)
        self.assertEqual(self.registry.path.read_bytes(), before)
        self.assertEqual(len(self.registry.list()['models']), 2)

    def test_manual_local_model_requires_explicit_context(self):
        provider = dict(name='Manual', base_url='http://localhost:9999/v1', deployment='local')
        with self.assertRaises(ValueError):
            self.registry.save(provider, dict(wire_model_id='unknown'))
        self.assertFalse(self.registry.path.exists())
        model = self.registry.save(provider, dict(wire_model_id='unknown',
            capabilities={'context_window': 1000000}))['model']
        self.assertEqual(model['reasoning_effort'], 'max')
        self.assertEqual(model['capabilities']['context_window'], 1000000)


if __name__ == '__main__':
    unittest.main()
