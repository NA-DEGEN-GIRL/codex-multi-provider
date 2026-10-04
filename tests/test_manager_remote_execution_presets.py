"""SSH presets use synthetic accounts, fake SSH, and real immutable installers."""
from contextlib import nullcontext
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from manager_core.execution_presets import ExecutionPresets, PresetError
from manager_core.providers import ProviderRegistry
from manager_core.remote import RemoteManager, RemoteError, REMOTE_CAPABILITY_MARKERS, supports_remote_claude
from manager_core.store import Store, atomic_json
from test_manager_remote import helper, make_artifact, probe, INSPECT_COMMAND

REMOTE = helper('execution_presets')
INSTALL = helper('install')
LAUNCH = helper('launch')


class RemoteExecutionPresetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.owner = self.store.add_profile('Owner')
        self.peer = self.store.add_profile('Peer')
        self.store.mutate(lambda data: [value.update(auth_mode='native', account_fingerprint='1' * 64)
                                       for value in data['profiles']])
        self.providers = ProviderRegistry(self.root)
        self.presets = ExecutionPresets(self.store, self.providers)
        self.config_home = '/home/test user/.local/share/codex-control-center/profiles/' + self.owner['id'] + '/codex'
        self.context = dict(host_id='ssh:staging', config_home=self.config_home,
                            remote_python='/usr/bin/python3', host_identity='f' * 64)
        self.base = self.providers.render_for_host(self.config_home, False, [])['files']['config.toml']

    def save(self, **updates):
        value = dict(name='Mixed work', roles=[dict(name='Implement', profile_id=self.peer['id'], model='gpt-6-astra', effort='high')])
        value.update(updates)
        return self.presets.save(self.owner['id'], value)

    def render(self):
        return self.presets.render_for_host(self.owner['id'], self.base, **self.context)

    def test_gpt_auth_is_scoped_broker_and_generated_paths_are_posix(self):
        self.save()
        with patch('manager_core.proxy_auth.read_existing_tokens', side_effect=AssertionError('no secrets')):
            result = self.render()
        role = next(iter(result['manifest']['roles'].values()))
        self.assertEqual(role['auth_source'], 'manager_proxy')
        self.assertEqual(role['profile_id'], self.peer['id'])
        self.assertEqual(role['expected_account_fingerprint'], '1' * 64)
        self.assertNotIn('auth_codex_home', role)
        self.assertNotIn(str(self.root), json.dumps(result))
        self.assertNotIn('manager-execution-presets.json', result['files'])
        self.assertEqual(set(result['authority']), {'schema_version', 'profile_id', 'host_id', 'roles', 'environment_model_ids'})

    def test_selection_rename_delete_and_history_do_not_change_definition(self):
        saved = self.save()
        before = self.render()['files']
        self.presets.set_default(self.owner['id'], saved['id'], 1)
        self.presets.bind(self.owner['id'], 'ssh:staging', 'task', saved['id'], 1)
        self.save(id=saved['id'], name='Renamed', expected_revision=1)
        self.presets.delete(self.owner['id'], saved['id'], expected_revision=2)
        after = self.render()
        self.assertEqual(before, after['files'])
        self.assertEqual([value['revision'] for value in after['manifest']['presets']], [1, 2])
        self.assertEqual(after['manifest']['task_bindings']['task']['revision'], 1)

    def test_local_scope_api_role_is_unresolved_and_ready_api_ids_are_in_environment_union(self):
        local = self.providers.save(dict(name='Local', base_url='http://127.0.0.1:43210/v1', protocol='responses',
            deployment='local', auth_type='none', execution_scope='local'),
            dict(name='Local', wire_model_id='local', reasoning_effort='low', capabilities={'verified': True, 'context_window': 32768}))
        api = self.providers.save(dict(name='Remote', base_url='https://example.invalid/v1', protocol='responses'),
            dict(name='API', wire_model_id='api', reasoning_effort='high', capabilities={'verified': True, 'context_window': 32768}))
        rejected = self.save(name='Local only', roles=[dict(name='Local', model_id=local['model']['id'])])
        self.save(name='HTTP', roles=[dict(name='API', model_id=api['model']['id'])])
        self.presets.set_default(self.owner['id'], rejected['id'], 1)
        rendered = self.render()
        self.assertEqual(rendered['environment_model_ids'], [api['model']['id']])
        self.assertEqual(rendered['unprepared_presets'], [dict(id=rejected['id'], revision=1)])
        self.assertEqual(rendered['manifest']['default_preset']['id'], rejected['id'])
        self.assertNotIn('127.0.0.1', json.dumps(rendered['files']))

    def test_known_roles_can_hot_add_preset_but_new_or_drifted_account_role_requires_prepare(self):
        saved = self.save()
        prepared = self.render()['authority']
        second = self.save(name='Same role')
        context = {key: value for key, value in self.context.items() if key != 'host_id'}
        result = self.presets.manifest_for_prepared_host(self.owner['id'], prepared, **context)
        self.assertFalse(result['runtime_prepare_required'])
        self.assertEqual(len(result['manifest']['presets']), 2)
        new = self.save(name='New role', roles=[dict(name='New role', profile_id=self.owner['id'])])
        result = self.presets.manifest_for_prepared_host(self.owner['id'], prepared, **context)
        self.assertEqual(result['unprepared_presets'], [dict(id=new['id'], revision=1)])
        self.store.mutate(lambda data: self.store.profile(self.peer['id'], data).update(account_fingerprint='2' * 64))
        result = self.presets.manifest_for_prepared_host(self.owner['id'], prepared, **context)
        self.assertEqual(len(result['unprepared_presets']), 3)
        self.assertEqual(result['manifest']['roles'], prepared['roles'])

    def test_mixed_claude_and_gpt_roles_share_remote_helper_bundle_and_main_auth_survives(self):
        claude = self.store.add_profile('Claude', claude_settings={})
        self.store.mutate(lambda data: self.store.profile(claude['id'], data).update(claude_account_identity='a' * 64))
        roles = [dict(name='GPT implement', profile_id=self.peer['id']),
                 dict(name='Claude review', profile_id=claude['id'], model='claude-opus-5-5', effort='ultracode'),
                 dict(name='Claude alternative', profile_id=claude['id'], model='sonnet', effort='max')]
        self.save(roles=roles)
        context = dict(self.context, remote_cli=dict(path='/opt/claude/2.1.282/claude', version='2.1.282'))
        result = self.presets.render_for_host(self.owner['id'], self.base, **context)
        self.assertFalse(result['runtime_prepare_required'])
        self.assertEqual(len(result['authority']['roles']), 3)
        claude_roles = [role for role in result['authority']['roles'].values() if role['profile_id'] == claude['id']]
        self.assertTrue(all(role['auth_source'] == 'manager_proxy' and role['expected_account_identity'] == 'a' * 64
                            for role in claude_roles))
        self.assertIn('claude_remote.py', result['helper_files'])
        self.assertNotIn(str(self.root), json.dumps(result['files']))
        from manager_core.claude_profiles import render_for_host
        from pathlib import PurePosixPath
        home = PurePosixPath(self.config_home.replace(self.owner['id'], claude['id']))
        profile = self.store.profile(claude['id'])
        base = render_for_host(self.providers, home, profile, host_id='ssh:staging',
            remote_python='/usr/bin/python3', host_identity='f' * 64, remote_cli=context['remote_cli'])
        manager = RemoteManager(self.root, registry=self.providers)
        merged = manager._render_presets(profile, base, alias='staging', config_home=str(home),
            remote_python='/usr/bin/python3', host_identity='f' * 64, remote_cli=context['remote_cli'])
        authority = merged['execution_presets']['authority']
        self.assertEqual(authority['main_auth']['profile_id'], claude['id'])
        hot = self.presets.manifest_for_prepared_host(claude['id'], authority, config_home=str(home),
            remote_python='/usr/bin/python3', host_identity='f' * 64, remote_cli=context['remote_cli'])
        self.assertEqual(hot['manifest']['main_auth'], authority['main_auth'])

    def fixture(self):
        saved = self.save()
        rendered = self.render()
        profile = self.root / 'remote/profiles' / self.owner['id']
        revision = 'a' * 64
        definition = profile / 'definitions' / revision
        definition.mkdir(parents=True)
        atomic_json(definition / REMOTE.AUTHORITY, rendered['authority'])
        descriptor = dict(profile_id=self.owner['id'], revision=revision, host_identity='f' * 64, definition=str(definition))
        authority = rendered['authority']
        request = dict(schema_version=1, profile_id=self.owner['id'], host_id='ssh:staging', host_identity='f' * 64,
                       revision=revision, source_revision=1, selection={key: rendered['manifest'][key]
                         for key in ('presets', 'default_preset', 'task_bindings')})
        module = types.SimpleNamespace(_descriptor=lambda selected_profile, selected_revision: descriptor)
        self.addCleanup(patch.stopall)
        patch.dict(sys.modules, native_controller=module).start()
        patch.object(REMOTE, '_lock', lambda path: nullcontext()).start()
        return profile, revision, request, rendered

    def test_remote_publish_and_cold_start_preserve_explicit_clear_and_default_snapshot(self):
        profile, revision, request, rendered = self.fixture()
        selected = {key: request['selection']['presets'][0][key] for key in ('id', 'revision')}
        request['selection'].update(default_preset=selected, task_bindings={'task': None, 'other': selected})
        result = REMOTE.publish(profile, revision, request)
        path = REMOTE.runtime_path(profile, revision)
        value = json.loads(path.read_text())
        self.assertEqual(result['status'], 'published')
        self.assertEqual(value['task_bindings'], {'task': None, 'other': selected})
        before = path.read_bytes()
        self.assertEqual(REMOTE.runtime_path(profile, revision).read_bytes(), before)
        self.assertFalse((profile / 'codex/config.toml').exists())

    def test_host_owner_revision_and_unprepared_roles_rejected_without_writes(self):
        profile, revision, request, _ = self.fixture()
        REMOTE.publish(profile, revision, request)
        path = REMOTE.runtime_path(profile, revision)
        before = path.read_bytes()
        changes = [dict(profile_id=self.peer['id']), dict(host_id='ssh:other'), dict(host_identity='e' * 64),
                   dict(revision='b' * 64), dict(source_revision=0)]
        for changed in changes:
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                REMOTE.publish(profile, revision, dict(request, **changed))
        invalid = deepcopy(request)
        invalid['selection']['presets'][0]['role_ids'] = ['cc_preset_' + 'f' * 24]
        with self.assertRaises(ValueError):
            REMOTE.publish(profile, revision, invalid)
        self.assertEqual(path.read_bytes(), before)

    def test_same_revision_conflict_and_stale_write_fail_but_identical_retry_succeeds(self):
        profile, revision, request, _ = self.fixture()
        REMOTE.publish(profile, revision, request)
        REMOTE.publish(profile, revision, request)
        changed = deepcopy(request)
        changed['selection']['task_bindings']['task'] = None
        with self.assertRaisesRegex(ValueError, 'stale'):
            REMOTE.publish(profile, revision, changed)
        changed['source_revision'] = 2
        REMOTE.publish(profile, revision, changed)
        with self.assertRaisesRegex(ValueError, 'stale'):
            REMOTE.publish(profile, revision, request)

    def test_new_definition_registry_does_not_mutate_running_old_revision(self):
        profile, revision, request, rendered = self.fixture()
        REMOTE.publish(profile, revision, request)
        old = REMOTE.runtime_path(profile, revision).read_bytes()
        new_revision = 'b' * 64
        definition = profile / 'definitions' / new_revision
        definition.mkdir()
        atomic_json(definition / REMOTE.AUTHORITY, rendered['authority'])
        sys.modules['native_controller']._descriptor = lambda p, r: dict(profile_id=p.name, revision=r,
            host_identity='f' * 64, definition=str(p / 'definitions' / r))
        request.update(revision=new_revision, source_revision=2)
        request['selection']['task_bindings']['new'] = None
        REMOTE.publish(profile, new_revision, request)
        self.assertEqual((profile / 'execution-presets' / (revision + '.json')).read_bytes(), old)

    def test_caps_preserve_last_registry(self):
        profile, revision, request, _ = self.fixture()
        REMOTE.publish(profile, revision, request)
        path = REMOTE.runtime_path(profile, revision)
        before = path.read_bytes()
        oversized = deepcopy(request)
        oversized['source_revision'] = 2
        oversized['selection']['task_bindings'] = {str(n) + 'x' * 200: None for n in range(6000)}
        with self.assertRaises(ValueError):
            REMOTE.publish(profile, revision, oversized)
        oversized = deepcopy(request)
        oversized['selection']['presets'] *= 257
        with self.assertRaises(ValueError):
            REMOTE.publish(profile, revision, oversized)
        self.assertEqual(path.read_bytes(), before)

    def test_manager_save_preflights_ssh_binding_size_before_persistence(self):
        self.save()
        before = self.presets.path.read_bytes()
        data = self.presets._read()
        data['bindings'] = {str(n): dict(profile_id=self.owner['id'], host_id='ssh:staging',
            thread_id=str(n) + 'x' * 200, preset_id=None, revision=None) for n in range(6000)}
        with self.assertRaises(PresetError):
            self.presets._write(data, self.owner['id'])
        self.assertEqual(self.presets.path.read_bytes(), before)

    def test_exact_native_byte_limit_and_257_authority_roles_reject_without_overwrite(self):
        profile, revision, request, rendered = self.fixture()
        authority = rendered['authority']
        selection = request['selection']
        low, high = 0, 6000
        while low < high:
            count = (low + high + 1) // 2
            selection['task_bindings'] = {str(n) + 'x' * 200: None for n in range(count)}
            if len(REMOTE._bytes(dict(authority, **selection))) <= REMOTE.MAX_BYTES - 20:
                low = count
            else:
                high = count - 1
        selection['task_bindings'] = {str(n) + 'x' * 200: None for n in range(low)}
        remaining = REMOTE.MAX_BYTES - len(REMOTE._bytes(dict(authority, **selection)))
        # One pretty-printed binding adds key length + fourteen bytes.
        key = 'p' * (remaining - 14)
        self.assertTrue(1 <= len(key) <= 256)
        selection['task_bindings'][key] = None
        self.assertEqual(len(REMOTE._bytes(dict(authority, **selection))), REMOTE.MAX_BYTES)
        REMOTE.publish(profile, revision, request)
        path = REMOTE.runtime_path(profile, revision)
        before = path.read_bytes()
        self.assertEqual(len(before), REMOTE.MAX_BYTES)
        request['source_revision'] += 1
        selection['task_bindings'][key + 'p'] = selection['task_bindings'].pop(key)
        with self.assertRaisesRegex(ValueError, 'size'):
            REMOTE.publish(profile, revision, request)
        self.assertEqual(path.read_bytes(), before)
        extra = deepcopy(authority)
        sample = next(iter(extra['roles'].values()))
        extra['roles'] = {'cc_preset_' + format(n, '024x'): sample for n in range(257)}
        atomic_json(profile / 'definitions' / revision / REMOTE.AUTHORITY, extra)
        with self.assertRaisesRegex(ValueError, 'authority'):
            REMOTE.publish(profile, revision, request)
        self.assertEqual(path.read_bytes(), before)

    def test_interrupted_registry_write_recovers_from_new_selection_journal(self):
        profile, revision, request, _ = self.fixture()
        REMOTE.publish(profile, revision, request)
        request['source_revision'] = 2
        request['selection']['task_bindings'] = {'task': None}
        original = REMOTE._atomic
        def interrupted(path, value):
            if path.name == revision + '.json':
                raise OSError('simulated power loss')
            return original(path, value)
        with patch.object(REMOTE, '_atomic', interrupted), self.assertRaises(OSError):
            REMOTE.publish(profile, revision, request)
        self.assertEqual(json.loads(REMOTE.runtime_path(profile, revision).read_text())['task_bindings'], {'task': None})

    def _real_archive_case(self, *, claude=False):
        from manager_core.model_settings import render_options
        if claude:
            from manager_core.claude_profiles import settings
            self.store.mutate(lambda data: self.store.profile(self.owner['id'], data).update(
                auth_mode='claude_code', claude_settings=settings({}), claude_account_identity='d' * 64))
            self.owner = self.store.profile(self.owner['id'])
        saved = self.save()
        self.presets.set_default(self.owner['id'], saved['id'], 1)
        artifact = make_artifact(self.root)
        manifest = json.loads((artifact / 'manifest.json').read_text())
        binary = artifact / 'codex'
        binary.write_bytes(binary.read_bytes() + b'\0'.join(REMOTE_CAPABILITY_MARKERS.values()))
        manifest['files'][0]['sha256'] = hashlib.sha256(binary.read_bytes()).hexdigest()
        atomic_json(artifact / 'manifest.json', manifest)
        config = self.root / 'ssh/config'
        config.parent.mkdir()
        config.write_text('Host staging\n HostName example.invalid\n')
        helper_dir = self.root / 'scripts/remote_helpers'
        helper_dir.mkdir(parents=True)
        for name in ('install', 'launch', 'native_controller', 'common', 'managed_sources', 'ws_client', 'execution_presets'):
            (helper_dir / (name + '.py')).write_bytes((ROOT / 'scripts/remote_helpers' / (name + '.py')).read_bytes())
        remote = self.root / 'simulated-remote'
        requests = []
        def runner(args, **options):
            if args[-1] == INSPECT_COMMAND:
                return subprocess.CompletedProcess(args, 0, probe(
                    claude_path='/opt/claude/2.1.282/claude', claude_version='2.1.282 (Claude Code)'), b'')
            if args[-1].endswith('--preflight'):
                result = INSTALL.preflight(json.loads(options['input']), remote)
            elif 'stdin' in options:
                result = INSTALL.install(io.BytesIO(options['stdin'].read()), remote)
            elif args[-1].endswith('--publish-execution-presets'):
                request = json.loads(options['input'])
                requests.append(request)
                result = dict(status='published', **{key: request[key] for key in
                              ('profile_id', 'host_id', 'revision', 'source_revision')})
            else:
                result = LAUNCH.configure(remote / 'profiles' / self.owner['id'], json.loads(options['input']), expected_host='f' * 64)
            return subprocess.CompletedProcess(args, 0, json.dumps(result).encode(), b'')
        manager = RemoteManager(self.root, ssh_config=config, runner=runner, ssh_executable='ssh.exe', registry=self.providers)
        binding = manager.prepare('staging', self.owner['id'], self.owner['home'], [], **render_options(self.owner))
        self.assertEqual(binding['execution_presets_version'], 1)
        self.assertEqual(binding['model_options'], render_options(self.owner))
        definition = remote / 'profiles' / self.owner['id'] / 'definitions' / binding['revision']
        self.assertTrue((definition / REMOTE.AUTHORITY).is_file())
        self.assertFalse((definition / 'manager-execution-presets.json').exists())
        authority = manager.execution_preset_authority(self.owner, binding)
        self.assertEqual(authority['roles'], self.render()['authority']['roles'])
        self.assertEqual(requests[0]['selection']['default_preset']['id'], saved['id'])
        first = manager.settings_fingerprint(self.owner, binding)
        self.presets.bind(self.owner['id'], 'ssh:staging', 'new-task', None)
        self.assertEqual(manager.settings_fingerprint(self.owner, binding), first)
        result = manager.publish_execution_presets(self.owner, binding)
        self.assertTrue(result['published'])
        self.assertIsNone(requests[-1]['selection']['task_bindings']['new-task'])
        drifted = dict(binding, host_identity='e' * 64)
        with self.assertRaises(RemoteError):
            manager.execution_preset_authority(self.owner, drifted)

    def test_real_archive_packages_authority_and_helpers_without_mutable_selectors(self):
        self._real_archive_case()

    def test_claude_prepare_accepts_public_render_options_and_preserves_prepared_snapshot(self):
        self._real_archive_case(claude=True)

    def test_claude_prepare_rejects_cross_account_or_stale_settings_before_network(self):
        from manager_core.claude_profiles import settings
        claude = self.store.add_profile('Claude', claude_settings={})
        manager = RemoteManager(self.root)
        for snapshot in (dict(id=self.owner['id'], settings=settings({})),
                         dict(id=claude['id'], settings=settings({'model': 'sonnet'})),
                         dict(id=claude['id'], settings=settings({}), path='/untrusted')):
            with self.subTest(snapshot=snapshot), patch.object(manager, '_alias', return_value='staging'), patch.object(
                    manager, 'inspect', side_effect=AssertionError('no network before validation')), self.assertRaises(RemoteError) as raised:
                manager.prepare('staging', claude['id'], claude['home'], [], claude_profile=snapshot)
            self.assertEqual(raised.exception.code, 'profile_settings_changed')

    def test_capability_claim_requires_exact_artifact_markers_and_rechecks_changed_binary(self):
        artifact = make_artifact(self.root)
        path = artifact / 'manifest.json'
        manifest = json.loads(path.read_text())
        manifest.update({key: True for key in REMOTE_CAPABILITY_MARKERS})
        atomic_json(path, manifest)
        self.assertFalse(supports_remote_claude(self.root))
        binary = artifact / 'codex'
        binary.write_bytes(binary.read_bytes() + b'\0'.join(REMOTE_CAPABILITY_MARKERS.values()))
        manifest['files'][0]['sha256'] = hashlib.sha256(binary.read_bytes()).hexdigest()
        atomic_json(path, manifest)
        self.assertTrue(supports_remote_claude(self.root))
        with patch('manager_core.remote.RemoteManager._artifact', side_effect=AssertionError('proof should be cached')):
            self.assertTrue(supports_remote_claude(self.root))
        binary.write_bytes(binary.read_bytes() + b'changed')
        self.assertFalse(supports_remote_claude(self.root))

    def test_stripped_release_marker_requires_private_auth_and_claude_auth(self):
        artifact = make_artifact(self.root)
        path = artifact / 'manifest.json'
        manifest = json.loads(path.read_text())
        manifest.update({key: True for key in REMOTE_CAPABILITY_MARKERS})
        binary = artifact / 'codex'
        header = binary.read_bytes()
        # Literal production strings observed in the stripped Linux release;
        # do not generate this fixture from the mapping under test.
        markers = [b'CODEX_MANAGER_EXECUTION_PRESETS',
                   b'account/executionPresetAuthTokens/read', b'CODEX_MANAGER_CLAUDE_AUTH']
        manager = RemoteManager(self.root)
        for missing in (None, 0, 1, 2):
            with self.subTest(missing=missing):
                binary.write_bytes(header + b'\0'.join(marker for index, marker in enumerate(markers)
                                                       if index != missing))
                self.assertNotIn(b'managed_execution_presets', binary.read_bytes())
                manifest['files'][0]['sha256'] = hashlib.sha256(binary.read_bytes()).hexdigest()
                atomic_json(path, manifest)
                self.assertEqual(supports_remote_claude(self.root), missing is None)
                self.assertEqual(manager._execution_presets_supported(dict(directory=artifact)), missing is None)

    def test_missing_prepared_authority_requires_preparation_for_every_selection(self):
        manager = RemoteManager(self.root, registry=self.providers)
        binding = dict(profile_id=self.owner['id'], alias='staging', revision='a' * 64,
            remote_python='/usr/bin/python3', remote_launcher=self.config_home.removesuffix('/codex') + '/launch.py',
            prepared=True, host_identity='f' * 64)
        with patch.object(manager, '_run', side_effect=AssertionError('no SSH when unprepared')):
            result = manager.publish_execution_presets(self.owner, binding)
        self.assertTrue(result['runtime_prepare_required'])
        self.assertNotIn('unprepared_presets', result)
        self.assertEqual(result['execution_presets_version'], 0)


if __name__ == '__main__':
    unittest.main()
