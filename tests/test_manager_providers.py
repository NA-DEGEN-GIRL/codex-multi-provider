"""Provider registry policy tests; all state and credentials are synthetic."""

import copy
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import MagicMock, patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core import providers


class ProviderRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.registry = providers.ProviderRegistry(self.root)
        self.profile_home = (self.root / 'work' / 'control-center' / 'profiles'
                             / str(uuid.uuid4()) / 'codex')
        network_guard = patch.object(providers, 'build_opener',
                                     side_effect=AssertionError('Tests must never open a network connection'))
        network_guard.start()
        self.addCleanup(network_guard.stop)

    def definition(self, name='Example provider', model='example-model'):
        return ({'name': name, 'base_url': 'https://provider.example/v1',
                 'protocol': 'responses', 'adapter_id': 'native-responses',
                 'adapter_version': '1'},
                {'name': model, 'wire_model_id': model,
                 'reasoning_effort': 'low',
                 'capabilities': {'verified': True, 'context_window': 32768,
                                  'input_modalities': ['text'],
                                  'supports_parallel_tool_calls': False,
                                  'supports_reasoning_summaries': False,
                                  'supported_reasoning_levels': ['low', 'max']}})

    def add_model(self, name='Example provider', model='example-model'):
        provider, model_definition = self.definition(name, model)
        return self.registry.save(provider, model_definition)

    def snapshot(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob('*') if path.is_file()}

    def prepare_probe_key(self, provider_id):
        with patch.object(providers, '_protect_secret', return_value=b'opaque synthetic probe key'):
            self.registry.save_key(provider_id, 'synthetic-probe-key')

    def successful_probe(self, base_url, key, payload):
        self.assertTrue(base_url.startswith('https://'))
        self.assertEqual(key, 'synthetic-probe-key')
        self.assertTrue(payload['stream'])
        self.assertFalse(payload['store'])
        nonce = re.search(r'value "([0-9a-f]{32})"', payload['input'][0]['content']).group(1)
        if isinstance(payload['tool_choice'], dict):
            return {'output': [{'type': 'function_call', 'name': 'manager_probe',
                                'call_id': 'synthetic-call',
                                'arguments': json.dumps({'value': nonce})}]}
        tool_output = payload['input'][-1]
        self.assertEqual(tool_output['type'], 'function_call_output')
        self.assertEqual(tool_output['call_id'], 'synthetic-call')
        self.assertEqual(tool_output['output'], nonce)
        self.assertEqual(payload['tool_choice'], 'none')
        return {'output': [{'type': 'message', 'content': [
            {'type': 'output_text', 'text': 'verified:' + nonce}]}]}

    def verify_with_synthetic_probe(self, saved):
        self.prepare_probe_key(saved['provider']['id'])
        with patch.object(providers, '_unprotect_secret', return_value=b'synthetic-probe-key'), \
                patch.object(providers, '_responses_probe', side_effect=self.successful_probe) as probe:
            result = self.registry.verify(saved['model']['id'])
        self.assertEqual(probe.call_count, 2)
        return result

    def generated_config(self):
        return tomllib.loads((self.profile_home / 'config.toml').read_text(encoding='utf-8'))

    def selected_roles(self, config):
        roles = []
        for item in config.get('agents', {}).values():
            if isinstance(item, dict) and item.get('config_file'):
                role_path = Path(item['config_file'])
                if not role_path.is_absolute():
                    role_path = self.profile_home / role_path
                roles.append((role_path, tomllib.loads(role_path.read_text(encoding='utf-8'))))
        return roles

    def test_empty_list_does_not_write_registry_state(self):
        before = self.snapshot()
        result = self.registry.list()
        self.assertEqual(result['providers'], [])
        self.assertEqual(result['models'], [])
        self.assertIn('revision', result)
        self.assertEqual(self.snapshot(), before)

    def test_legacy_import_is_read_only_and_never_decrypts_key(self):
        legacy = self.root / 'profiles'
        legacy.mkdir()
        (legacy / 'provider.json').write_text(json.dumps({
            'base_url': 'https://legacy.example/v1', 'model': 'deepseek-flash',
            'protocol': 'responses'}), encoding='utf-8')
        (legacy / 'deepseek.dpapi').write_bytes(b'fictional encrypted legacy key')
        before = self.snapshot()
        with patch.object(providers, '_unprotect_secret') as decrypt:
            first = self.registry.list()
            second = providers.ProviderRegistry(self.root).list()
        decrypt.assert_not_called()
        self.assertEqual(first, second)
        self.assertEqual(len(first['providers']), 1)
        self.assertEqual(len(first['models']), 1)
        uuid.UUID(first['providers'][0]['id'])
        uuid.UUID(first['models'][0]['id'])
        self.assertEqual(first['models'][0]['reasoning_effort'], 'max')
        self.assertEqual(first['models'][0]['supported_reasoning_efforts'], ['none', 'low', 'high', 'max'])
        self.assertNotIn('fictional encrypted legacy key', json.dumps(first))
        self.assertEqual(self.snapshot(), before)

    def test_save_assigns_stable_ids_and_binds_model_to_provider(self):
        saved = self.add_model()
        uuid.UUID(saved['provider']['id'])
        uuid.UUID(saved['model']['id'])
        self.assertEqual(saved['model']['provider_id'], saved['provider']['id'])
        provider = copy.deepcopy(saved['provider'])
        model = copy.deepcopy(saved['model'])
        provider['name'] = 'Renamed account alias'
        model['name'] = 'Renamed model alias'
        changed = self.registry.save(provider, model)
        self.assertEqual(changed['provider']['id'], saved['provider']['id'])
        self.assertEqual(changed['model']['id'], saved['model']['id'])
        self.assertNotEqual(changed['revision'], saved['revision'])
        self.assertEqual(len(self.registry.list()['providers']), 1)
        self.assertEqual(len(self.registry.list()['models']), 1)

    def test_wire_model_alias_is_accepted(self):
        provider, model = self.definition()
        model['model'] = model.pop('wire_model_id')
        saved = self.registry.save(provider, model)
        self.assertEqual(saved['model']['wire_model_id'], 'example-model')

    def test_flash_effort_can_be_changed_and_catalog_matches(self):
        saved = self.add_model(model='deepseek-flash')
        model = copy.deepcopy(saved['model'])
        model.update(name='Renamed Flash', reasoning_effort='max')
        updated = self.registry.save(saved['provider'], model)
        self.assertTrue(updated['model']['capabilities']['verified'])
        self.registry.generate(self.profile_home, True, [model['id']])
        external_roles = [role for _, role in self.selected_roles(self.generated_config())
                          if role.get('model') == 'deepseek-flash']
        self.assertEqual(len(external_roles), 1)
        self.assertEqual(external_roles[0]['model_reasoning_effort'], 'max')
        catalog=self.registry._catalog(updated['model'])['models'][0]
        self.assertEqual([r['effort'] for r in catalog['supported_reasoning_levels']], ['none','low','high','max'])

    def test_flash_maps_compatibility_efforts_to_actual_values(self):
        provider,model=self.definition(model='deepseek-flash')
        model['reasoning_effort']='ultra'
        saved=self.registry.save(provider,model)
        self.assertEqual(saved['model']['reasoning_effort'], 'max')
        updated=self.registry.save(saved['provider'], {'id':saved['model']['id'],'reasoning_effort':'medium'})
        self.assertEqual(updated['model']['reasoning_effort'], 'high')

    def test_other_model_is_not_forced_to_max_by_display_alias(self):
        provider, model = self.definition(model='another-model')
        model['name'] = 'DeepSeek 4.1 Flash'
        saved = self.registry.save(provider, model)
        self.assertIsNone(saved['model']['forced_reasoning_effort'])
        self.registry.generate(self.profile_home, True, [saved['model']['id']])
        roles = [role for _, role in self.selected_roles(self.generated_config())
                 if role.get('model') == 'another-model']
        self.assertEqual(roles[0]['model_reasoning_effort'], 'low')

    def test_keys_are_decrypted_only_for_selected_environment(self):
        first = self.add_model('First provider', 'first-model')
        second = self.add_model('Second provider', 'second-model')
        first_secret = 'synthetic-first-api-key'
        second_secret = 'synthetic-second-api-key'
        with patch.object(providers, '_protect_secret', side_effect=lambda value: b'cipher:' + value):
            self.registry.save_key(first['provider']['id'], first_secret)
            self.registry.save_key(second['provider']['id'], second_secret)
        with patch.object(providers, '_unprotect_secret', side_effect=lambda value: value.removeprefix(b'cipher:')) as decrypt:
            visible = self.registry.list()
            decrypt.assert_not_called()
            environment = self.registry.environment([first['model']['id']])
        self.assertEqual(list(environment.values()), [first_secret])
        self.assertEqual(decrypt.call_count, 1)
        self.assertNotIn(first_secret, json.dumps(visible))
        self.assertNotIn(second_secret, json.dumps(visible))
        with patch.object(providers, '_unprotect_secret') as decrypt:
            self.assertEqual(self.registry.environment([]), {})
        decrypt.assert_not_called()

    def test_generated_configuration_does_not_include_plaintext_secrets(self):
        saved = self.add_model()
        secret = 'synthetic-secret-never-in-config'
        with patch.object(providers, '_protect_secret', return_value=b'opaque protected blob'):
            self.registry.save_key(saved['provider']['id'], secret)
        with patch.object(providers, '_unprotect_secret') as decrypt:
            result = self.registry.generate(self.profile_home, True, [saved['model']['id']])
        decrypt.assert_not_called()
        self.assertNotIn(secret, json.dumps(result))
        for path in self.profile_home.rglob('*'):
            if path.is_file():
                self.assertNotIn(secret.encode(), path.read_bytes(), str(path))

    def test_multiple_providers_have_distinct_allowlisted_roles_and_keep_gpt(self):
        first = self.add_model('First provider', 'first-model')
        second = self.add_model('Second provider', 'second-model')
        result = self.registry.generate(self.profile_home, True,
                                        [first['model']['id'], second['model']['id']])
        self.assertTrue(result['enabled'])
        config = self.generated_config()
        allowlist = config['subagent_model_provider_allowlist']
        self.assertEqual(len(allowlist), 2)
        self.assertEqual(len(set(allowlist)), 2)
        roles = [role for _, role in self.selected_roles(config)]
        external = [role for role in roles if role.get('model') in ('first-model', 'second-model')]
        self.assertEqual(len(external), 2)
        self.assertEqual({role['model_provider'] for role in external}, set(allowlist))
        self.assertTrue(any(role.get('model', '').startswith('gpt-') for role in roles))
        for provider_id in allowlist:
            self.assertEqual(config['model_providers'][provider_id]['wire_api'], 'responses')
            self.assertFalse(config['model_providers'][provider_id]['requires_openai_auth'])

    def test_turning_external_models_off_clears_the_active_allowlist(self):
        saved = self.add_model()
        self.registry.generate(self.profile_home, True, [saved['model']['id']])
        result = self.registry.generate(self.profile_home, False, [saved['model']['id']])
        config = self.generated_config()
        self.assertFalse(result['enabled'])
        self.assertEqual(config.get('subagent_model_provider_allowlist', []), [])
        selected = [role for _, role in self.selected_roles(config)]
        self.assertFalse(any(role.get('model') == 'example-model' for role in selected))
        self.assertTrue(any(role.get('model', '').startswith('gpt-') for role in selected))
        self.assertFalse(list((self.profile_home / 'agents').glob('cc_external_*.toml')))
        self.assertEqual(len(list((self.profile_home / 'manager-retired-agents').glob('cc_external_*.toml'))), 1)

    def test_external_only_retires_native_roles_without_removing_user_roles(self):
        saved = self.add_model()
        self.registry.generate(self.profile_home, True, [saved['model']['id']])
        user_role = self.profile_home / 'agents' / 'reviewer.toml'
        user_role.write_text('name = "reviewer"\ndescription = "Custom role"\n'
                             'developer_instructions = "Review only"\n', encoding='utf-8')
        before = user_role.read_bytes()
        self.registry.generate(self.profile_home, True, [saved['model']['id']], selection_mode='external_only')
        self.assertFalse(list((self.profile_home / 'agents').glob('cc_gpt_*.toml')))
        self.assertEqual(len(list((self.profile_home / 'manager-retired-agents').glob('cc_gpt_*.toml'))), 4)
        self.assertEqual(user_role.read_bytes(), before)
        first = self.snapshot()
        self.registry.generate(self.profile_home, True, [saved['model']['id']], selection_mode='external_only')
        self.assertEqual(self.snapshot(), first)

    def test_retirement_preserves_customized_manager_named_role(self):
        saved = self.add_model()
        self.registry.generate(self.profile_home, True, [saved['model']['id']])
        role = next((self.profile_home / 'agents').glob('cc_external_*.toml'))
        role.write_text('name = "custom"\ndescription = "User owned"\n'
                        'developer_instructions = "My custom instruction"\n', encoding='utf-8')
        before = role.read_bytes()
        self.registry.generate(self.profile_home, False, [])
        self.assertEqual(role.read_bytes(), before)

    def test_unverified_model_cannot_be_generated(self):
        provider, model = self.definition()
        model['capabilities']['verified'] = False
        saved = self.registry.save(provider, model)
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.registry.generate(self.profile_home, True, [saved['model']['id']])
        self.assertEqual(self.snapshot(), before)

    def test_unsupported_protocol_is_never_silently_rendered_as_responses(self):
        provider, model = self.definition()
        provider['protocol'] = 'chat_completions'
        provider['adapter_id'] = 'chat-completions'
        saved = self.registry.save(provider, model)
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.registry.generate(self.profile_home, True, [saved['model']['id']])
        self.assertEqual(self.snapshot(), before)

    def test_unknown_selected_model_fails_before_writing_profile(self):
        with self.assertRaises(ValueError):
            self.registry.generate(self.profile_home, True, [str(uuid.uuid4())])
        self.assertFalse(self.profile_home.exists())

    def test_regeneration_preserves_custom_mcp_comments_and_stable_revision(self):
        self.profile_home.mkdir(parents=True)
        user_config = ('# Personal configuration must survive\nmodel = "gpt-6-astra"\n'
                       '[mcp_servers.private_tool]\ncommand = "fictional-mcp"\n'
                       'args = ["--unchanged"]\n# Keep this trailing comment\n')
        config_path = self.profile_home / 'config.toml'
        config_path.write_text(user_config, encoding='utf-8')
        saved = self.add_model()
        first = self.registry.generate(self.profile_home, True, [saved['model']['id']])
        first_content = config_path.read_bytes()
        second = self.registry.generate(self.profile_home, True, [saved['model']['id']])
        self.assertIn(user_config, config_path.read_text(encoding='utf-8'))
        self.assertEqual(first_content, config_path.read_bytes())
        self.assertEqual(first['revision'], second['revision'])
        self.assertEqual(first['effective_revision'], second['effective_revision'])
        self.assertEqual(self.generated_config()['mcp_servers']['private_tool']['command'], 'fictional-mcp')

    def test_native_windows_setup_survives_repeated_restart_preparation(self):
        self.registry.generate(self.profile_home, False, [])
        path = self.profile_home / 'config.toml'
        settings = ('# Native settings may be inserted inside the generated block\n'
                    '[windows]\nsandbox = "elevated"\n'
                    '[desktop]\nappearanceTheme = "dark"\n'
                    '[projects."C:\\\\fixture"]\ntrust_level = "trusted"\n')
        text = path.read_text(encoding='utf-8').replace(providers._END, settings + providers._END)
        path.write_text(text, encoding='utf-8')
        expected = tomllib.loads(text)
        for _ in range(3):
            self.registry.generate(self.profile_home, False, [])
            actual = self.generated_config()
            for section in ('windows', 'desktop', 'projects'):
                self.assertEqual(actual[section], expected[section])
        self.assertIn('# Native settings may be inserted', path.read_text(encoding='utf-8'))

    def test_native_settings_between_provider_tables_survive_external_toggle(self):
        saved = self.add_model()
        first = self.registry.render_for_host(self.profile_home, True, [saved['model']['id']])['files']['config.toml']
        settings = '[windows]\nsandbox = "unelevated"\n[agents.personal]\ndescription = "keep"\n'
        text = first.replace('[agents.cc_gpt_sol]', settings + '[agents.cc_gpt_sol]')
        for enabled in (False, True, False):
            text = self.registry.render_for_host(self.profile_home, enabled, [saved['model']['id']] if enabled else [], text)['files']['config.toml']
            parsed = tomllib.loads(text)
            self.assertEqual(parsed['windows'], {'sandbox': 'unelevated'})
            self.assertEqual(parsed['agents']['personal'], {'description': 'keep'})
            self.assertEqual(bool(parsed.get('model_providers')), enabled)

    def test_generated_agent_role_files_satisfy_runtime_validation(self):
        # Standalone discovery (including desktop settings) must validate the
        # file without depending on the description in the parent config.
        saved = self.add_model()
        files = self.registry.render_for_host(self.profile_home, True, [saved['model']['id']])['files']
        roles = {name: content for name, content in files.items() if name.startswith('agents/')}
        self.assertTrue(roles)
        for name, content in roles.items():
            parsed = tomllib.loads(content)
            self.assertTrue(parsed.get('name', '').strip(), name)
            self.assertTrue(parsed.get('description', '').strip(), name)
            self.assertEqual(parsed['description'], tomllib.loads(files['config.toml'])['agents'][parsed['name']]['description'])
            self.assertTrue(parsed.get('developer_instructions', '').strip(), name)
        self.assertIn('agents/cc_gpt_astra.toml', roles)
        self.assertTrue(any(name.startswith('agents/cc_external_') for name in roles))

    def test_provider_markers_and_tables_inside_multiline_values_are_not_instructions(self):
        value = providers._BEGIN + '\n[windows]\nsandbox="fiction"\n' + providers._END
        text = 'developer_instructions = \'\'\'\n' + value + '\n\'\'\'\n'
        text += providers._BEGIN + '\n[agents.cc_gpt_astra]\ndescription="old"\n' + providers._END + '\n'
        stripped = providers._strip_provider_block(text)
        self.assertEqual(tomllib.loads(stripped), {'developer_instructions': value + '\n'})

    def test_incomplete_provider_markers_fail_without_modifying_settings(self):
        for text in (providers._BEGIN + '\n[windows]\nsandbox="elevated"\n', providers._END + '\n',
                     providers._BEGIN + '\n' + providers._BEGIN + '\n' + providers._END + '\n'):
            with self.subTest(text=text), self.assertRaises(providers.ProviderError):
                self.registry.render_for_host(self.profile_home, False, [], text)

    def test_changed_provider_archives_existing_role_revision_without_rediscovery(self):
        saved = self.add_model()
        self.registry.generate(self.profile_home, True, [saved['model']['id']])
        first_config = self.generated_config()
        old_allowlist = set(first_config['subagent_model_provider_allowlist'])
        old_roles = {path: path.read_bytes() for path, role in self.selected_roles(first_config)
                     if role.get('model') == 'example-model'}
        provider = dict(saved['provider'], base_url='https://other-provider.example/v1')
        updated = self.registry.save(provider, saved['model'])
        self.assertFalse(updated['model']['capabilities']['verified'])
        self.verify_with_synthetic_probe(updated)
        self.registry.generate(self.profile_home, True, [saved['model']['id']])
        new_allowlist = set(self.generated_config()['subagent_model_provider_allowlist'])
        self.assertTrue(old_allowlist.isdisjoint(new_allowlist))
        for path, content in old_roles.items():
            self.assertFalse(path.exists())
            archived = list((self.profile_home / 'manager-retired-agents').glob(path.stem + '.*.toml'))
            self.assertEqual(len(archived), 1)
            self.assertEqual(archived[0].read_bytes(), content)

    def test_catalog_uses_own_conservative_capabilities(self):
        saved = self.add_model(model='text-only-small-context')
        self.registry.generate(self.profile_home, True, [saved['model']['id']])
        role = next(role for _, role in self.selected_roles(self.generated_config())
                    if role.get('model') == 'text-only-small-context')
        catalog_path = Path(role['model_catalog_json'])
        if not catalog_path.is_absolute():
            catalog_path = self.profile_home / catalog_path
        catalog = json.loads(catalog_path.read_text(encoding='utf-8'))['models'][0]
        self.assertEqual(catalog['slug'], 'text-only-small-context')
        self.assertEqual(catalog['context_window'], 32768)
        self.assertEqual(catalog['input_modalities'], ['text'])
        self.assertFalse(catalog.get('supports_parallel_tool_calls', False))
        self.assertFalse(catalog.get('supports_image_detail_original', False))

    def test_verification_requires_tool_call_and_consumed_dynamic_nonce(self):
        provider, model = self.definition(model='unverified-model')
        model['capabilities']['verified'] = False
        saved = self.registry.save(provider, model)
        result = self.verify_with_synthetic_probe(saved)
        self.assertTrue(result['verified'])
        self.assertTrue(result['model']['capabilities']['verified'])
        self.assertIn('function-tool-call', result['checks'])
        self.assertIn('tool-result-follow-up', result['checks'])
        self.assertTrue(result['limitations'])
        self.assertNotIn('synthetic-probe-key', json.dumps(result))
        current = next(item for item in self.registry.list()['models'] if item['id'] == saved['model']['id'])
        self.assertTrue(current['capabilities']['verified'])
        self.assertEqual(current['capabilities']['verification_source'], 'live-responses-two-turn-probe')

    def test_failed_followup_does_not_mark_model_verified(self):
        provider, model = self.definition()
        model['capabilities']['verified'] = False
        saved = self.registry.save(provider, model)
        self.prepare_probe_key(saved['provider']['id'])
        before = self.registry.list()

        def failed_followup(base_url, key, payload):
            if isinstance(payload['tool_choice'], dict):
                return self.successful_probe(base_url, key, payload)
            return {'output': [{'type': 'message', 'content': [
                {'type': 'output_text', 'text': 'This is not the required result.'}]}]}

        with patch.object(providers, '_unprotect_secret', return_value=b'synthetic-probe-key'), \
                patch.object(providers, '_responses_probe', side_effect=failed_followup) as probe:
            with self.assertRaises(providers.ProviderError):
                self.registry.verify(saved['model']['id'])
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(self.registry.list(), before)

    def test_malformed_probe_response_has_safe_error_and_keeps_unverified(self):
        provider, model = self.definition()
        model['capabilities']['verified'] = False
        saved = self.registry.save(provider, model)
        self.prepare_probe_key(saved['provider']['id'])
        before = self.registry.list()
        for response in (None, {'output': [None]}, {'output': ['sensitive remote body']},
                         {'output': [{'type': 'function_call', 'name': 'manager_probe',
                                      'call_id': 'synthetic-call', 'arguments': 'sensitive remote body'}]}):
            with self.subTest(response=response), \
                    patch.object(providers, '_unprotect_secret', return_value=b'synthetic-probe-key'), \
                    patch.object(providers, '_responses_probe', return_value=response):
                with self.assertRaises(providers.ProviderError) as error:
                    self.registry.verify(saved['model']['id'])
                self.assertNotIn('sensitive remote body', str(error.exception))
                self.assertNotIn('synthetic-probe-key', str(error.exception))
                self.assertEqual(self.registry.list(), before)

    def test_mutation_during_probe_does_not_verify_new_model_settings(self):
        provider, model = self.definition()
        model['capabilities']['verified'] = False
        saved = self.registry.save(provider, model)
        self.prepare_probe_key(saved['provider']['id'])

        def mutating_probe(base_url, key, payload):
            response = self.successful_probe(base_url, key, payload)
            if isinstance(payload['tool_choice'], dict):
                changed = copy.deepcopy(saved['model'])
                changed['wire_model_id'] = 'different-model-after-request'
                self.registry.save(saved['provider'], changed)
            return response

        with patch.object(providers, '_unprotect_secret', return_value=b'synthetic-probe-key'), \
                patch.object(providers, '_responses_probe', side_effect=mutating_probe):
            with self.assertRaisesRegex(providers.ProviderError, 'changed'):
                self.registry.verify(saved['model']['id'])
        current = next(item for item in self.registry.list()['models'] if item['id'] == saved['model']['id'])
        self.assertEqual(current['wire_model_id'], 'different-model-after-request')
        self.assertFalse(current['capabilities']['verified'])

    def test_malformed_completed_stream_is_reported_without_raw_body(self):
        for event in (['sensitive remote body'],
                      {'type': 'response.completed', 'response': 'sensitive remote body'}):
            with self.subTest(event=event):
                response = MagicMock()
                response.headers = {'Content-Type': 'text/event-stream'}
                response.__enter__.return_value = response
                body = io.BytesIO(('data: ' + json.dumps(event) + '\n\n').encode('utf-8'))
                response.readline.side_effect = body.readline
                response.__iter__.return_value = iter(body)
                opener = MagicMock()
                opener.open.return_value = response
                with patch.object(providers, 'build_opener', return_value=opener):
                    with self.assertRaises(providers.ProviderError) as error:
                        providers._responses_probe('https://provider.example/v1',
                                                   'synthetic-probe-key', {'input': []})
                self.assertNotIn('sensitive remote body', str(error.exception))
                self.assertNotIn('synthetic-probe-key', str(error.exception))

    def test_invalid_provider_and_model_ids_do_not_modify_state(self):
        for field, value in [('provider', '../escape'), ('provider', 'not-a-uuid'),
                             ('model', '../../escape'), ('model', 'not-a-uuid')]:
            with self.subTest(field=field, value=value):
                provider, model = self.definition()
                (provider if field == 'provider' else model)['id'] = value
                before = self.snapshot()
                with self.assertRaises(ValueError):
                    self.registry.save(provider, model)
                self.assertEqual(self.snapshot(), before)

    def test_invalid_endpoint_and_wire_ids_are_rejected(self):
        for endpoint in ('file:///tmp/model', 'http://remote.example/v1',
                         'https://user:secret@provider.example/v1',
                         'https://provider.example/v1?key=secret',
                         'https://provider.example/v1#fragment'):
            with self.subTest(endpoint=endpoint):
                provider, model = self.definition()
                provider['base_url'] = endpoint
                with self.assertRaises(ValueError):
                    self.registry.save(provider, model)
        for wire_id in ('bad model', 'bad\nmodel', 'bad"model', ''):
            with self.subTest(wire_id=wire_id):
                provider, model = self.definition()
                model['wire_model_id'] = wire_id
                with self.assertRaises(ValueError):
                    self.registry.save(provider, model)

    def test_generation_cannot_escape_owned_profile_home(self):
        saved = self.add_model()
        roots = [self.root / 'outside', self.root / 'profiles' / 'runtime',
                 self.profile_home / '..' / '..' / '..' / 'escape',
                 self.root / 'work' / 'control-center' / 'profiles' / 'invalid-id' / 'codex']
        for invalid_home in roots:
            with self.subTest(path=str(invalid_home)):
                before = self.snapshot()
                with self.assertRaises(ValueError):
                    self.registry.generate(invalid_home, True, [saved['model']['id']])
                self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
