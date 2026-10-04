"""Execution preset metadata and rendering use synthetic accounts/providers only."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.execution_presets import ExecutionPresets, PresetError, _task_identity
from manager_core.providers import ProviderRegistry
from manager_core.store import Store, atomic_json


class ExecutionPresetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root)
        self.owner = self.store.add_profile('계정 06')
        self.peer = self.store.add_profile('다른 GPT 계정')
        self.claude = self.store.add_profile('Claude 계정', claude_settings={})
        self.store.mutate(lambda data: [profile.update(auth_mode='native', login_state='signed_in', account_fingerprint='1' * 64)
                                       for profile in data['profiles'] if profile['id'] in (self.owner['id'], self.peer['id'])])
        self.providers = ProviderRegistry(self.root)
        self.presets = ExecutionPresets(self.store, self.providers)

    def payload(self, **changes):
        return dict(name='GPT + Claude 검토', main={}, roles=[
            dict(name='구현 담당', profile_id=self.owner['id'], model='gpt-6-astra', effort='high'),
            dict(name='검토 담당', profile_id=self.claude['id'], model='claude-opus-5-5', effort='ultracode'),
        ], **changes)

    def save(self):
        return self.presets.save(self.owner['id'], self.payload())

    def test_empty_list_exposes_safe_account_choices_without_creating_registry(self):
        with patch('manager_core.proxy_auth.read_existing_tokens', side_effect=AssertionError('No credential reads')):
            result = self.presets.list(self.owner['id'])
        self.assertEqual(result['presets'], [])
        self.assertEqual(result['owner']['id'], self.owner['id'])
        account = next(item for item in result['accounts'] if item['id'] == self.claude['id'])
        self.assertEqual(account['kind'], 'claude_code')
        self.assertTrue(any(model['id'] == 'claude-opus-5-5' for model in account['models']))
        self.assertNotIn('home', json.dumps(result))
        self.assertFalse(self.presets.path.exists())

    def test_edit_keeps_immutable_revision_and_requires_compare_and_swap(self):
        first = self.save()
        second_value = self.payload()
        second_value.update(id=first['id'], name='다른 조합')
        with self.assertRaises(PresetError):
            self.presets.save(self.owner['id'], second_value)
        second = self.presets.save(self.owner['id'], second_value, expected_revision=1)
        self.assertEqual(second['revision'], 2)
        self.assertEqual(self.presets.get(self.owner['id'], first['id'], 1), first)
        self.assertEqual(second['main'], {})
        self.assertEqual(self.store.profile(self.owner['id'])['policy'], self.owner['policy'])

    def test_default_revision_and_first_task_binding_do_not_follow_later_edits(self):
        first = self.save()
        self.presets.set_default(self.owner['id'], first['id'], 1)
        bound = self.presets.get_for_task(self.owner['id'], 'local', 'one', bind_default=True)
        value = self.payload()
        value.update(id=first['id'], name='수정')
        self.presets.save(self.owner['id'], value, expected_revision=1)
        self.presets.set_default(self.owner['id'], first['id'], 2)
        self.assertEqual(self.presets.get_for_task(self.owner['id'], 'local', 'one')['revision'], 1)
        self.assertEqual(self.presets.get_for_task(self.owner['id'], 'local', 'two', bind_default=True)['revision'], 2)
        self.assertEqual(bound['preset'], first)

    def test_explicit_clear_survives_default_binding_and_process_reopen(self):
        first = self.save()
        self.presets.set_default(self.owner['id'], first['id'], 1)
        self.presets.bind(self.owner['id'], 'local', 'one', None)
        reopened = ExecutionPresets(self.store)
        cleared = reopened.get_for_task(self.owner['id'], 'local', 'one', bind_default=True)
        self.assertTrue(cleared['cleared'])
        self.assertIsNone(cleared['preset_id'])
        self.assertIsNotNone(reopened.get_for_task(self.owner['id'], 'local', 'two', bind_default=True)['preset_id'])

    def test_host_and_owner_are_part_of_task_binding_identity(self):
        first = self.save()
        self.presets.bind(self.owner['id'], 'local', 'same-task', first['id'], 1)
        self.assertIsNone(self.presets.get_for_task(self.owner['id'], 'remote:host', 'same-task'))
        self.assertIsNone(self.presets.get_for_task(self.peer['id'], 'local', 'same-task'))
        with self.assertRaises(PresetError):
            self.presets.bind(self.peer['id'], 'local', 'same-task', first['id'], 1)

    def test_delete_preserves_bound_revision_but_blocks_new_selection(self):
        first = self.save()
        self.presets.bind(self.owner['id'], 'local', 'existing', first['id'], 1)
        self.presets.set_default(self.owner['id'], first['id'], 1)
        self.presets.delete(self.owner['id'], first['id'], expected_revision=1)
        self.assertEqual(self.presets.list(self.owner['id'])['presets'], [])
        self.assertIsNone(self.presets.list(self.owner['id'])['default'])
        self.assertEqual(self.presets.get_for_task(self.owner['id'], 'local', 'existing')['preset'], first)
        with self.assertRaises(PresetError):
            self.presets.bind(self.owner['id'], 'local', 'new', first['id'], 1)

    def test_invalid_account_model_or_caller_paths_do_not_mutate_registry(self):
        self.save()
        before = self.presets.path.read_bytes()
        values = []
        for patch_value in ({'model': 'gpt-unknown'}, {'profile_id': 'invalid'}, {'auth_codex_home': 'C:/other'}, {'effort': 'ultracode'}):
            value = self.payload()
            value['roles'][0].update(patch_value)
            values.append(value)
        values += [dict(name='invalid', main=[], roles=[]), dict(name='invalid', roles=[dict(name='role', model='gpt-6-astra')])]
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.presets.save(self.owner['id'], value)
        self.assertEqual(self.presets.path.read_bytes(), before)

    def test_registered_http_and_local_models_are_revision_bound_choices(self):
        local = self.providers.save(dict(name='Local fixture', base_url='http://127.0.0.1:43210/v1',
            protocol='responses', deployment='local', auth_type='none', execution_scope='local'),
            dict(name='Local test', wire_model_id='local-test', reasoning_effort='low',
                 capabilities={'verified': True, 'context_window': 32768}))
        remote = self.providers.save(dict(name='HTTP fixture', base_url='https://example.invalid/v1', protocol='responses'),
            dict(name='HTTP test', wire_model_id='http-test', reasoning_effort='high',
                 capabilities={'verified': True, 'context_window': 32768}))
        value = dict(name='기존 모델 조합', roles=[dict(name='로컬', model_id=local['model']['id']),
                                                   dict(name='HTTP', model_id=remote['model']['id'])])
        saved = self.presets.save(self.owner['id'], value)
        self.assertEqual([role['kind'] for role in saved['roles']], ['local', 'external'])
        self.assertTrue(all(role['model_revision'] == 1 and role['provider_revision'] == 1 for role in saved['roles']))
        result = self.presets.list(self.owner['id'])
        self.assertEqual(len(result['models']), 2)
        self.assertFalse(next(model for model in result['models'] if model['kind'] == 'external')['credentials_ready'])

    def base_config(self, profile=None):
        profile = profile or self.owner
        options = dict(claude_profile=dict(id=profile['id'], settings=profile['claude_settings'])) if profile.get('auth_mode') == 'claude_code' else {}
        return self.providers.render_for_host(Path(profile['home']), False, [], **options)['files']['config.toml']

    def prepare(self, profile=None):
        profile = profile or self.owner
        self.providers.generate(profile['home'], False, [], **(
            dict(claude_profile=dict(id=profile['id'], settings=profile['claude_settings']))
            if profile.get('auth_mode') == 'claude_code' else {}))
        return self.presets.prepare_runtime(profile['id'])

    def test_mixed_roles_have_explicit_accounts_and_preserve_legacy_flags(self):
        record = self.save()
        original = self.base_config()
        with patch('manager_core.proxy_auth.read_existing_tokens', side_effect=AssertionError('No credential reads')):
            rendered = self.presets.render_native_registry(self.owner['id'], original)
        config = tomllib.loads(rendered['files']['config.toml'])
        self.assertEqual(config['features'], tomllib.loads(original)['features'])
        manifest = rendered['manifest']
        self.assertEqual(manifest['presets'][0]['main'], {})
        self.assertEqual(manifest['task_bindings'], {})
        self.assertIsNone(manifest['default_preset'])
        for role in record['roles']:
            role_id = self.presets.role_id(role)
            metadata = manifest['roles'][role_id]
            role_config = tomllib.loads(rendered['files']['agents/' + role_id + '.toml'])
            self.assertEqual(role_config['model_provider'], metadata['model_provider'])
            self.assertEqual(role_config['model'], metadata['model'])
            self.assertFalse(role_config['features']['multi_agent_v2']['enabled'])
            self.assertIn(metadata['model_provider'] + '/' + metadata['model'], config['subagent_model_allowlist'])
            if role['kind'] == 'codex':
                self.assertEqual(metadata['auth_codex_home'], str(Path(self.owner['home']).resolve()))
                self.assertEqual(metadata['expected_account_fingerprint'], '1' * 64)
            else:
                provider = config['model_providers'][metadata['model_provider']]
                self.assertEqual(provider['agent']['profile_id'], self.claude['id'])
                self.assertIn(self.claude['id'], provider['agent']['args'])

    def test_claude_parent_explicit_source_gpt_child_and_distinct_claude_accounts(self):
        source = self.root / 'source-account'
        self.store.mutate(lambda data: self.store.profile(self.peer['id'], data).update(auth_mode='source', source_home=str(source)))
        other = self.store.add_profile('다른 Claude', claude_settings={})
        self.presets.save(self.claude['id'], dict(name='혼합', roles=[
            dict(name='GPT', profile_id=self.peer['id']),
            dict(name='첫 Claude', profile_id=self.claude['id']),
            dict(name='두번째 Claude', profile_id=other['id'])]))
        rendered = self.presets.render_native_registry(self.claude['id'], self.base_config(self.claude))
        roles = rendered['manifest']['roles'].values()
        gpt = next(role for role in roles if role['kind'] == 'codex')
        self.assertEqual(gpt['model_provider'], 'openai')
        self.assertEqual(gpt['auth_codex_home'], str(source.resolve()))
        self.assertEqual(len({role['model_provider'] for role in roles if role['kind'] == 'claude_code'}), 2)
        config = tomllib.loads(rendered['files']['config.toml'])
        self.assertEqual(config['model_provider'], 'claude_code')
        self.assertFalse(config['features']['multi_agent_v2']['enabled'])

    def test_prepare_writes_only_runtime_files_and_ignores_stale_running_status(self):
        first = self.save()
        self.presets.bind(self.owner['id'], 'local', 'bound', first['id'], 1)
        self.presets.bind(self.owner['id'], 'local', 'cleared', None)
        self.presets.bind(self.owner['id'], 'remote:other', 'remote-only', first['id'], 1)
        self.store.mutate(lambda data: self.store.profile(self.owner['id'], data).update(status='running'))
        with patch('manager_core.proxy_auth.read_existing_tokens', side_effect=AssertionError('No credential reads')):
            result = self.prepare()
        self.assertFalse(result['runtime_prepare_required'])
        path = self.presets.runtime_path(self.owner['id'])
        manifest = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(manifest['task_bindings'], {'bound': {'id': first['id'], 'revision': 1}, 'cleared': None})
        self.assertFalse((path.parent / 'auth.json').exists())
        self.assertEqual(self.prepare()['manifest'], manifest)

    def test_hot_publish_reuses_identical_roles_without_rewriting_config(self):
        self.save()
        self.prepare()
        config = Path(self.owner['home']) / 'config.toml'
        before = config.read_bytes()
        value = self.payload()
        value['name'] = '두번째 이름'
        second = self.presets.save(self.owner['id'], value)
        self.presets.set_default(self.owner['id'], second['id'], 1)
        self.presets.bind(self.owner['id'], 'local', 'task', None)
        result = self.presets.publish_registry(self.owner['id'])
        self.assertFalse(result['runtime_prepare_required'])
        self.assertEqual(config.read_bytes(), before)
        manifest = json.loads(self.presets.runtime_path(self.owner['id']).read_text(encoding='utf-8'))
        self.assertEqual(manifest['default_preset'], dict(id=second['id'], revision=1))
        self.assertIsNone(manifest['task_bindings']['task'])

    def test_new_role_and_account_drift_require_prepare_without_silent_selection_fallback(self):
        first = self.save()
        self.prepare()
        value = self.payload()
        value['roles'][0]['effort'] = 'low'
        second = self.presets.save(self.owner['id'], value)
        self.presets.bind(self.owner['id'], 'local', 'new-role', second['id'], 1)
        result = self.presets.publish_registry(self.owner['id'])
        self.assertEqual(result['unprepared_presets'], [dict(id=second['id'], revision=1)])
        manifest = json.loads(self.presets.runtime_path(self.owner['id']).read_text(encoding='utf-8'))
        self.assertEqual(manifest['task_bindings']['new-role'], dict(id=second['id'], revision=1))
        self.assertEqual([preset['id'] for preset in manifest['presets']], [first['id']])
        self.store.mutate(lambda data: self.store.profile(self.owner['id'], data).update(account_fingerprint='2' * 64))
        result = self.presets.publish_registry(self.owner['id'])
        self.assertEqual(len(result['unprepared_presets']), 2)
        replacement = self.presets.save(self.owner['id'], self.payload())
        self.assertNotEqual(self.presets.role_id(first['roles'][0]), self.presets.role_id(replacement['roles'][0]))

    def test_native_role_missing_identity_is_not_prepared(self):
        self.store.mutate(lambda data: self.store.profile(self.owner['id'], data).pop('account_fingerprint'))
        record = self.save()
        result = self.presets.render_native_registry(self.owner['id'], self.base_config())
        self.assertTrue(result['runtime_prepare_required'])
        self.assertEqual(result['unprepared_presets'], [dict(id=record['id'], revision=1)])
        self.assertEqual(result['manifest']['presets'], [])

    def test_deleted_revision_remains_prepared_for_native_only_durable_references(self):
        first = self.save()
        self.prepare()
        ids = [self.presets.role_id(role) for role in first['roles']]
        self.presets.delete(self.owner['id'], first['id'], expected_revision=1)
        result = self.prepare()
        self.assertEqual(result['manifest']['presets'][0]['id'], first['id'])
        self.assertEqual(self.presets.list(self.owner['id'])['presets'], [])
        for role_id in ids:
            self.assertTrue((Path(self.owner['home']) / 'agents' / (role_id + '.toml')).exists())

    def test_inherited_default_history_survives_edit_and_delete_without_manager_binding(self):
        first = self.save()
        self.presets.set_default(self.owner['id'], first['id'], 1)
        self.prepare()
        # Native task creation remembers revision 1 internally, without bind().
        changed = self.payload()
        changed.update(id=first['id'], name='다음 작업용 수정')
        changed['roles'][0]['effort'] = 'low'
        self.presets.save(self.owner['id'], changed, expected_revision=1)
        self.presets.set_default(self.owner['id'], first['id'], 2)
        self.prepare()
        self.presets.delete(self.owner['id'], first['id'], expected_revision=2)
        self.assertFalse(self.presets.publish_registry(self.owner['id'])['runtime_prepare_required'])
        for manifest in (json.loads(self.presets.runtime_path(self.owner['id']).read_text(encoding='utf-8')),
                         self.prepare()['manifest']):
            self.assertEqual(manifest['task_bindings'], {})
            self.assertIsNone(manifest['default_preset'])
            self.assertEqual({(preset['id'], preset['revision']) for preset in manifest['presets']},
                             {(first['id'], 1), (first['id'], 2)})

    def test_external_role_keeps_registered_revisions_and_environment_model_ids(self):
        registered = self.providers.save(dict(name='Local fixture', base_url='http://127.0.0.1:43210/v1',
            protocol='responses', deployment='local', auth_type='none', execution_scope='local'),
            dict(name='Local test', wire_model_id='local-test', reasoning_effort='low',
                 capabilities={'verified': True, 'context_window': 32768}))
        saved = self.presets.save(self.owner['id'], dict(name='로컬 검토', roles=[dict(name='검토', model_id=registered['model']['id'])]))
        result = self.prepare()
        self.assertEqual(result['environment_model_ids'], [registered['model']['id']])
        self.assertEqual(self.presets.environment_model_ids(self.owner['id']), [registered['model']['id']])
        old_role = result['manifest']['roles'][self.presets.role_id(saved['roles'][0])]
        changed = dict(registered['model'], wire_model_id='local-test-new')
        self.providers.save(registered['provider'], changed)
        self.assertFalse(self.presets.publish_registry(self.owner['id'])['runtime_prepare_required'])
        self.assertEqual(self.prepare()['manifest']['roles'][self.presets.role_id(saved['roles'][0])], old_role)

    def test_changed_provider_cannot_pair_current_key_with_historical_endpoint(self):
        registered = self.providers.save(dict(name='HTTP fixture', base_url='https://old.example.invalid/v1', protocol='responses'),
            dict(name='HTTP test', wire_model_id='http-test', reasoning_effort='high',
                 capabilities={'verified': True, 'context_window': 32768}))
        old = self.presets.save(self.owner['id'], dict(name='저장된 연결', roles=[dict(name='검토', model_id=registered['model']['id'])]))
        self.presets.bind(self.owner['id'], 'local', 'existing', old['id'], 1)
        self.presets.set_default(self.owner['id'], old['id'], 1)
        self.prepare()
        self.providers.save(dict(registered['provider'], base_url='https://new.example.invalid/v1'), registered['model'])
        # No credential is read or sent. A changed revision alone must be enough
        # to reject the historical endpoint before environment key injection.
        with self.assertRaises(ValueError):
            self.providers.execution_preset_external(old['roles'][0])
        hot = self.presets.publish_registry(self.owner['id'])
        self.assertEqual(hot['unprepared_presets'], [dict(id=old['id'], revision=1)])
        prepared = self.prepare()
        self.assertEqual(prepared['environment_model_ids'], [])
        manifest = prepared['manifest']
        self.assertEqual(manifest['presets'], [])
        self.assertEqual(manifest['roles'], {})
        self.assertEqual(manifest['default_preset'], dict(id=old['id'], revision=1))
        self.assertEqual(manifest['task_bindings']['existing'], dict(id=old['id'], revision=1))

    def test_unavailable_account_does_not_block_independent_ready_presets(self):
        unavailable = self.save()
        self.presets.bind(self.owner['id'], 'local', 'unavailable', unavailable['id'], 1)
        ready = self.presets.save(self.owner['id'], dict(name='준비된 GPT', roles=[dict(name='구현', profile_id=self.peer['id'])]))
        self.presets.bind(self.owner['id'], 'local', 'ready', ready['id'], 1)
        self.store.mutate(lambda data: self.store.profile(self.claude['id'], data).update(removed_at='fixture'))
        result = self.prepare()
        self.assertEqual(result['unprepared_presets'], [dict(id=unavailable['id'], revision=1)])
        self.assertEqual([preset['id'] for preset in result['manifest']['presets']], [ready['id']])
        self.assertEqual(result['manifest']['task_bindings'], {
            'ready': dict(id=ready['id'], revision=1), 'unavailable': dict(id=unavailable['id'], revision=1)})

    def test_revision_257_is_rejected_before_persistence_and_does_not_prune_history(self):
        first = self.presets.save(self.owner['id'], dict(name='이력 1', roles=[]))
        for revision in range(2, 257):
            self.presets.save(self.owner['id'], dict(id=first['id'], name=f'이력 {revision}', roles=[]), revision - 1)
        result = self.prepare()
        self.assertEqual(len(result['manifest']['presets']), 256)
        before = self.presets.path.read_bytes()
        runtime_before = self.presets.runtime_path(self.owner['id']).read_bytes()
        with self.assertRaisesRegex(PresetError, '256'):
            self.presets.save(self.owner['id'], dict(id=first['id'], name='이력 257', roles=[]), 256)
        self.assertEqual(self.presets.path.read_bytes(), before)
        self.assertEqual(self.presets.runtime_path(self.owner['id']).read_bytes(), runtime_before)
        self.assertEqual(self.presets.get(self.owner['id'], first['id'], 1), first)
        self.assertEqual(self.presets.get(self.owner['id'], first['id'])['revision'], 256)

    def test_unique_role_257_is_rejected_across_presets_and_revisions(self):
        for index in range(8):
            self.presets.save(self.owner['id'], dict(name=f'조합 {index}', roles=[
                dict(name=f'담당 {index}-{role}', profile_id=self.owner['id']) for role in range(32)]))
        result = self.prepare()
        self.assertEqual(len(result['manifest']['roles']), 256)
        before = self.presets.path.read_bytes()
        with self.assertRaisesRegex(PresetError, '256'):
            self.presets.save(self.owner['id'], dict(name='추가 조합', roles=[dict(name='257번째 담당', profile_id=self.owner['id'])]))
        self.assertEqual(self.presets.path.read_bytes(), before)
        # Reusing already-prepared roles consumes a preset slot, not a role slot.
        self.presets.save(self.owner['id'], dict(name='기존 담당 재사용', roles=[dict(name='담당 0-0', profile_id=self.owner['id'])]))
        self.assertFalse(self.presets.publish_registry(self.owner['id'])['runtime_prepare_required'])

    def test_oversize_bindings_preserve_last_valid_store_registry_and_config(self):
        first = self.presets.save(self.owner['id'], dict(name='작업 연결', roles=[]))
        self.prepare()
        before = self.presets.path.read_bytes()
        path = self.presets.runtime_path(self.owner['id'])
        runtime_before = path.read_bytes()
        config = path.parent / 'config.toml'
        config_before = config.read_bytes()
        candidate = self.presets._read()
        for index in range(10000):
            key, identity = _task_identity(self.owner['id'], 'local', str(UUID(int=index + 1)))
            candidate['bindings'][key] = dict(identity, preset_id=first['id'], revision=1)
        with self.assertRaisesRegex(PresetError, '1 MiB'):
            self.presets._write(candidate, self.owner['id'])
        self.assertEqual(self.presets.path.read_bytes(), before)
        # Simulate an oversized registry accepted by an earlier manager version:
        # both live publication and inactive preparation must reject it before
        # replacing any valid runtime authority or configuration file.
        atomic_json(self.presets.path, candidate)
        with self.assertRaisesRegex(PresetError, '1 MiB'):
            self.presets.publish_registry(self.owner['id'])
        with self.assertRaisesRegex(PresetError, '1 MiB'):
            self.presets.prepare_runtime(self.owner['id'])
        self.assertEqual(path.read_bytes(), runtime_before)
        self.assertEqual(config.read_bytes(), config_before)

    def test_runtime_byte_guard_uses_utf8_serialized_size_at_exact_boundary(self):
        # An ignored manifest field makes the byte boundary exact without
        # exposing fake paths or relying on a chosen number of task IDs.
        manifest = dict(schema_version=1, profile_id=self.owner['id'], presets=[], roles={},
                        default_preset=None, task_bindings={}, fixture='가')
        size = len((json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8'))
        manifest['fixture'] += 'x' * (1024 * 1024 - size)
        self.presets._validate_runtime_limits(manifest)
        written = self.root / 'native-byte-boundary.json'
        atomic_json(written, manifest)
        self.assertEqual(written.stat().st_size, 1024 * 1024)
        manifest['fixture'] += 'x'
        with self.assertRaisesRegex(PresetError, '1 MiB'):
            self.presets._validate_runtime_limits(manifest)
        self.assertEqual(written.stat().st_size, 1024 * 1024)


if __name__ == '__main__':
    unittest.main()
