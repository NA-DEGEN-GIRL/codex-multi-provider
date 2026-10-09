"""Claude manager integration with synthetic profiles; no login or model requests."""
import json
import os
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.claude_profiles import ClaudeProfiles, DEFAULTS, settings
from manager_core.claude_skills import prepare_shared_skills
from manager_core.model_settings import render_options
from manager_core.providers import ProviderRegistry
from manager_core.store import Store


class ClaudeProfilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root)
        self.profile = self.store.add_profile('Claude test', claude_settings={})
        self.registry = ProviderRegistry(self.root)

    def test_auth_is_isolated_from_codex_credentials(self):
        profile = self.profile
        self.assertEqual('claude_code', profile['auth_mode'])
        self.assertEqual(DEFAULTS, profile['claude_settings'])
        self.assertFalse(profile.get('source_home'))
        self.assertFalse(profile.get('account_fingerprint'))
        with self.assertRaises(ValueError):
            self.store.add_profile('invalid', external_model_id='00000000-0000-4000-8000-000000000000', claude_settings={})

    def test_render_roundtrip_and_maximum_compaction(self):
        profile = dict(self.profile, claude_settings={**DEFAULTS, 'context_window': 200000, 'auto_compact_percent': 95})
        first = self.registry.generate(profile['home'], False, [], **render_options(profile))
        text = (Path(profile['home']) / 'config.toml').read_text(encoding='utf-8')
        parsed = tomllib.loads(text)
        provider = parsed['model_providers']['claude_code']
        self.assertFalse(provider['requires_openai_auth'])
        self.assertNotIn('env_key', provider)
        self.assertEqual('claude_code', provider['agent']['kind'])
        self.assertEqual(95, provider['agent']['auto_compact_percent'])
        self.assertFalse(parsed['features']['multi_agent'])
        self.assertEqual('cc-opus', parsed['model'])
        catalog = json.loads(Path(parsed['model_catalog_json']).read_text(encoding='utf-8'))
        self.assertEqual(190000, catalog['models'][0]['auto_compact_token_limit'])
        self.assertEqual(100, catalog['models'][0]['effective_context_window_percent'])
        self.registry.generate(profile['home'], False, [], **render_options(profile))
        self.assertEqual(text, (Path(profile['home']) / 'config.toml').read_text(encoding='utf-8'))
        self.assertEqual('claude_code', first['primary']['agent_kind'])

    def test_automatic_defaults_remove_old_compaction_and_pass_ultracode_unchanged(self):
        profile = dict(self.profile, claude_settings={**DEFAULTS, 'reasoning_effort': 'ultracode'})
        old = 'model_auto_compact_token_limit = 170000\n[windows]\nsandbox = "elevated"\n'
        result = self.registry.render_for_host(profile['home'], False, [], old, **render_options(profile))
        parsed = tomllib.loads(result['files']['config.toml'])
        agent = parsed['model_providers']['claude_code']['agent']
        self.assertNotIn('context_window', agent)
        self.assertNotIn('auto_compact_percent', agent)
        self.assertNotIn('model_auto_compact_token_limit', parsed)
        self.assertEqual(parsed['model_context_window'], 1000000)
        self.assertEqual(parsed['model_reasoning_effort'], 'ultracode')
        self.assertEqual(parsed['windows']['sandbox'], 'elevated')
        catalog_path = next(path for path in result['files'] if path.startswith('catalogs/'))
        model = json.loads(result['files'][catalog_path])['models'][0]
        self.assertEqual(model['default_reasoning_level'], 'ultracode')
        self.assertIsNone(model['auto_compact_token_limit'])
        self.assertIn({'effort': 'ultracode', 'description': 'ultracode'}, model['supported_reasoning_levels'])

    def test_explicit_legacy_settings_survive_partial_edits_and_can_reset_to_auto(self):
        facade = ClaudeProfiles(self.store)
        facade.configure(self.profile['id'], {'context_window': 200000, 'auto_compact_percent': 85})
        facade.configure(self.profile['id'], {'reasoning_effort': 'ultracode'})
        self.assertEqual(self.store.profile(self.profile['id'])['claude_settings'],
                         dict(model='opus', reasoning_effort='ultracode', context_window=200000, auto_compact_percent=85))
        facade.configure(self.profile['id'], {'context_window': None, 'auto_compact_percent': None})
        self.assertEqual(self.store.profile(self.profile['id'])['claude_settings'],
                         {**DEFAULTS, 'reasoning_effort': 'ultracode'})

    def test_explicit_model_default_keeps_other_claude_models_available_in_task(self):
        facade = ClaudeProfiles(self.store)
        facade.configure(self.profile['id'], {'model': 'claude-opus-5-5', 'reasoning_effort': 'ultracode'})
        profile = self.store.profile(self.profile['id'])
        result = self.registry.generate(profile['home'], False, [], **render_options(profile))
        config = tomllib.loads((Path(profile['home']) / 'config.toml').read_text(encoding='utf-8'))
        catalog = json.loads(Path(config['model_catalog_json']).read_text(encoding='utf-8'))['models']
        self.assertEqual(config['model'], 'cc-claude-opus-5-5')
        self.assertEqual([model['slug'] for model in catalog],
                         ['cc-claude-opus-5-5', 'cc-opus', 'cc-sonnet', 'cc-fable'])
        for model in catalog:
            self.assertEqual(model['default_reasoning_level'], 'ultracode')
            self.assertEqual([level['effort'] for level in model['supported_reasoning_levels']],
                             result['primary']['supported_models'][model['slug']])
            self.assertEqual(model['context_window'], 1000000)
            self.assertIsNone(model['auto_compact_token_limit'])
        before = self.store.path.read_bytes()
        with self.assertRaises(ValueError):
            facade.configure(self.profile['id'], {'model': 'claude-sonnet-5-5'})
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_other_config_preserved_when_binding_changes(self):
        existing = 'approval_policy = "never"\n[windows]\nsandbox = "elevated"\n'
        args = render_options(self.profile)
        result = self.registry.render_for_host(self.profile['home'], False, [], existing, **args)
        parsed = tomllib.loads(result['files']['config.toml'])
        self.assertEqual('never', parsed['approval_policy'])
        self.assertEqual('elevated', parsed['windows']['sandbox'])

    def test_invalid_limits_do_not_mutate_profile(self):
        before = self.store.path.read_bytes()
        for invalid in ({'context_window': True}, {'auto_compact_percent': 96},
                        {'context_window': 100000, 'auto_compact_percent': 85},
                        {'model': 'unapproved'}, {'context_window': 1000001}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                ClaudeProfiles(self.store).configure(self.profile['id'], invalid)
        self.assertEqual(before, self.store.path.read_bytes())

    def test_settings_revision_and_auth_status_allowlist(self):
        facade = ClaudeProfiles(self.store)
        facade.configure(self.profile['id'], {'effort': 'max'})
        after = self.store.profile(self.profile['id'])
        self.assertEqual('max', after['claude_settings']['reasoning_effort'])
        self.assertGreater(after['policy']['desired_revision'], self.profile['policy']['desired_revision'])
        with patch('manager_core.claude_auth.auth_status', return_value={
                'logged_in': True, 'email': 'test-private@example.invalid', 'access_token': 'do-not-save'}):
            facade.refresh(self.profile['id'])
        state = self.store.path.read_text(encoding='utf-8')
        self.assertNotIn('do-not-save', state)
        self.assertNotIn('test-private', state)
        self.assertIn('t***@example.invalid', state)

    def test_failed_status_does_not_keep_stale_logged_in_card(self):
        from manager_core.claude_auth import ClaudeError
        self.store.mutate(lambda data: self.store.profile(self.profile['id'], data).update(
            claude_status=dict(logged_in=True)))
        with patch('manager_core.claude_auth.auth_status', side_effect=ClaudeError('cli_missing', 'CLI missing.')):
            status = ClaudeProfiles(self.store).refresh(self.profile['id'])
        self.assertFalse(status['logged_in'])
        self.assertEqual('cli_missing', status['state'])

    def test_status_keeps_usage_only_for_the_same_verified_account(self):
        facade = ClaudeProfiles(self.store)
        first, second = 'a' * 64, 'b' * 64
        facade.record_status(self.profile['id'], dict(logged_in=True, account_identity=first))
        self.store.mutate(lambda data: self.store.profile(self.profile['id'], data).update(
            usage={'provider': 'claude_code', 'windows': {'five_hour': {'used_percent': 20}}}))
        facade.record_status(self.profile['id'], dict(logged_in=True, account_identity=first))
        self.assertIn('windows', self.store.profile(self.profile['id'])['usage'])
        status = facade.record_status(self.profile['id'], dict(logged_in=True, account_identity=second))
        self.assertNotIn('account_identity', status)
        self.assertEqual(self.store.profile(self.profile['id'])['usage'], {'provider': 'claude_code'})
        facade.record_status(self.profile['id'], dict(logged_in=False, account_identity=second))
        self.assertIsNone(self.store.profile(self.profile['id'])['claude_account_identity'])

    def test_login_start_invalidates_account_bound_usage(self):
        facade = ClaudeProfiles(self.store)
        facade.record_status(self.profile['id'], dict(logged_in=True, account_identity='a' * 64))
        with patch('manager_core.claude_auth.launch_login', return_value={'started': True}):
            facade.login(self.profile['id'])
        current = self.store.profile(self.profile['id'])
        self.assertIsNone(current['claude_account_identity'])
        self.assertEqual(current['usage'], {'provider': 'claude_code'})

    def test_edit_running_profile_schedules_idle_restart(self):
        from control_center import ControlCenter
        control = ControlCenter.__new__(ControlCenter)
        control.store = self.store
        control.instances = Mock()
        control.instances.prepare.return_value = {}
        control.instances.observe.return_value = {'status': 'running'}
        control.restarts = Mock()
        control.restarts.schedule.return_value = {'state': 'waiting'}
        result = control.dispatch('claude.settings', {'profile_id': self.profile['id'], 'reasoning_effort': 'max'})
        self.assertEqual('waiting', result['restart']['state'])
        control.restarts.schedule.assert_called_once_with(self.profile['id'])
        self.assertEqual('max', self.store.profile(self.profile['id'])['claude_settings']['reasoning_effort'])
        with self.assertRaisesRegex(ValueError, 'Claude Code'):
            control.dispatch('policy.set', {'profile_id': self.profile['id'], 'enabled': True, 'model_ids': []})

    def test_local_open_ignores_legacy_ssh_gate_and_remote_prepare_does_not_enroll(self):
        from control_center import ControlCenter
        control = ControlCenter.__new__(ControlCenter)
        control.store = self.store
        control.root = self.store.root
        control.instances = Mock()
        control.instances.observe.return_value = {'status': 'closed'}
        control.instances.show.return_value = {'state': 'opened'}
        control.remote_maintenance = Mock()
        control.restarts = Mock()
        self.store.mutate(lambda data: data.update(ssh_maintenance={
            self.profile['id']: {'state': 'pending'}}))
        self.assertEqual('opened', control._open_profile_locally(self.profile['id'])['state'])
        control.instances.show.assert_called_once_with(self.profile['id'], reopen_existing=False)
        control.remote_maintenance.pending_on_open.assert_not_called()
        control.restarts.open_local.assert_not_called()
        before = self.store.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'Claude SSH'):
            control._prepare_remote(self.profile, 'example', [])
        self.assertEqual(before, self.store.path.read_bytes())

    def test_claude_desktop_environment_does_not_prepare_ssh_or_inherit_api_auth(self):
        from manager_core.instances import Instances
        instance = Instances(self.root, self.store, self.registry)
        self.registry.generate(self.profile['home'], False, [], **render_options(self.profile))
        release = self.root / 'artifacts/manager/releases/fixture'
        release.mkdir(parents=True)
        proxy = release / 'Codex.ControlCenter.RuntimeProxy.exe'
        proxy.touch()
        runtime = self.root / 'runtime-fixture.exe'
        runtime.touch()
        (self.root / 'artifacts/manager/current.json').write_text(json.dumps({
            'runtime_proxy': str(proxy), 'ssh_proxy': str(release / 'ssh/ssh.exe')}), encoding='utf-8')
        ordinary_path = str(self.root / 'ordinary-bin')
        env = {'PATH': str(release / 'ssh') + os.pathsep + ordinary_path,
               'ANTHROPIC_API_KEY': 'fake-sensitive', 'OPENAI_API_KEY': 'fake-sensitive',
               'CLAUDE_CODE_GIT_BASH_PATH': 'fixture-bash'}
        with patch('desktop_launch.child_environment', return_value=env), \
                patch('manager_core.runtime_build.resolve', return_value={'runtime': str(runtime), 'capabilities': {}}), \
                patch('manager_core.workspace_seed.ensure'), \
                patch('manager_core.shared_catalog.environment', return_value={}), \
                patch('manager_core.ssh_shim.prepare_environment') as remote:
            result = instance.environment({**self.profile, 'generation': '11111111-1111-4111-8111-111111111111'})
        remote.assert_not_called()
        self.assertEqual(ordinary_path, result['PATH'])
        self.assertEqual('fixture-bash', result['CLAUDE_CODE_GIT_BASH_PATH'])
        self.assertNotIn('ANTHROPIC_API_KEY', result)
        self.assertNotIn('OPENAI_API_KEY', result)
        self.assertEqual('claude_code', json.loads(result['CODEX_MANAGER_PRIMARY_MODEL'])['agent_kind'])

    @unittest.skipUnless(sys.platform == 'win32', 'Windows to SSH config boundary')
    def test_windows_cli_is_not_published_to_linux(self):
        with self.assertRaisesRegex(ValueError, '원격'):
            self.registry.render_for_host('/home/example/.codex', False, [], **render_options(self.profile))

    def test_shared_skills_respect_disable_and_invalidate_on_change(self):
        home = Path(self.profile['home'])
        enabled = home / 'skills' / 'example' / 'SKILL.md'
        disabled = home / 'skills' / 'disabled' / 'SKILL.md'
        for path in (enabled, disabled):
            path.parent.mkdir(parents=True)
            path.write_text('---\nname: example\ndescription: Test shared skill\n---\nRead assets locally.\n', encoding='utf-8')
        (home / 'config.toml').write_text('[[skills.config]]\npath = ' + json.dumps(str(disabled)) + '\nenabled = false\n', encoding='utf-8')
        project = self.root / 'project'
        project.mkdir()
        (project / '.git').mkdir()
        first = prepare_shared_skills(self.root, self.profile['id'], project)
        self.assertEqual(first, prepare_shared_skills(self.root, self.profile['id'], project))
        entries = list(first.glob('skills/*/SKILL.md'))
        self.assertEqual(1, len(entries))
        self.assertIn(json.dumps(str(enabled.resolve()), ensure_ascii=False), entries[0].read_text(encoding='utf-8'))
        enabled.write_text(enabled.read_text(encoding='utf-8') + 'Changed instruction.\n', encoding='utf-8')
        second = prepare_shared_skills(self.root, self.profile['id'], project)
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_dir())


if __name__ == '__main__':
    unittest.main()
