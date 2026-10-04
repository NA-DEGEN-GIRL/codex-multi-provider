"""Offline SSH Claude account, packaging and runner boundaries; no provider calls."""
import json
import os
from pathlib import Path, PurePosixPath
import sys
import tempfile
import time
import tomllib
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.claude_auth import ClaudeError
from manager_core.claude_borrowed_auth import read_access_token
from manager_core.claude_profiles import render_for_host, render_execution_role_for_host
from manager_core.providers import ProviderRegistry
from manager_core.store import Store, atomic_json
from manager_core.claude_runner import digest, serve
from remote_helpers.claude_remote import RemoteExecution


class BorrowedClaudeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root)
        self.profile = self.store.add_profile('Claude fixture', claude_settings={})
        self.identity = 'a' * 64
        self.store.mutate(lambda state: self.store.profile(self.profile['id'], state).update(
            claude_account_identity=self.identity))
        self.directory = self.root / 'official-auth'
        self.directory.mkdir()
        self.expires = int(time.time()) + 3600
        self.credentials = dict(claudeAiOauth=dict(accessToken='synthetic-access',
            refreshToken='PRIVATE_REFRESH', expiresAt=self.expires * 1000))
        atomic_json(self.directory / '.credentials.json', self.credentials)
        self.status = dict(logged_in=True, account_identity=self.identity)
        self.directory_patch = patch('manager_core.claude_borrowed_auth.config_dir', return_value=self.directory)
        self.status_patch = patch('manager_core.claude_borrowed_auth.auth_status', return_value=self.status)
        self.directory_patch.start()
        self.status_mock = self.status_patch.start()
        self.addCleanup(self.directory_patch.stop)
        self.addCleanup(self.status_patch.stop)

    def read(self):
        return read_access_token(self.root, self.profile['id'], self.identity)

    def test_only_current_access_token_crosses_boundary_and_source_is_unchanged(self):
        before = (self.directory / '.credentials.json').read_bytes()
        self.assertEqual(self.read(), dict(accessToken='synthetic-access', expiresAt=self.expires,
                                         accountIdentity=self.identity))
        self.assertEqual((self.directory / '.credentials.json').read_bytes(), before)
        self.assertEqual(self.status_mock.call_count, 2)

    def test_switched_account_and_expired_token_are_refused(self):
        for status, expires in ((dict(logged_in=True, account_identity='b' * 64), self.expires),
                                (self.status, int(time.time()) + 60)):
            with self.subTest(status=status, expires=expires):
                self.status_mock.return_value = status
                self.credentials['claudeAiOauth']['expiresAt'] = expires * 1000
                atomic_json(self.directory / '.credentials.json', self.credentials)
                with self.assertRaisesRegex(ClaudeError, 'current local login'):
                    self.read()

    def test_credential_rotation_during_status_check_fails_closed(self):
        def status(*args):
            if self.status_mock.call_count == 2:
                self.credentials['claudeAiOauth']['accessToken'] = 'rotated-access'
                atomic_json(self.directory / '.credentials.json', self.credentials)
            return self.status
        self.status_mock.side_effect = status
        with self.assertRaises(ClaudeError):
            self.read()


class RemoteClaudeRenderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root)
        self.profile = self.store.add_profile('Claude fixture', claude_settings={})
        self.profile['claude_account_identity'] = 'a' * 64
        self.registry = ProviderRegistry(self.root)
        self.home = PurePosixPath('/home/remote/.local/share/codex-control-center/profiles') / str(uuid4()) / 'codex'
        self.host = dict(host_id='ssh:fixture', remote_python='/usr/bin/python3.12',
                         host_identity='b' * 64,
                         remote_cli=dict(path='/home/remote/.local/bin/claude', version='2.1.282'))

    def test_main_and_multiple_roles_use_posix_paths_and_stable_account_binding(self):
        main = render_for_host(self.registry, self.home, self.profile, **self.host)
        role = render_execution_role_for_host(self.registry, self.home, self.profile,
                                              'sonnet', 'ultracode', **self.host)
        second = render_execution_role_for_host(self.registry, self.home, self.profile,
                                                'opus', 'max', **self.host)
        binding = 'claude-bindings/' + self.profile['id'] + '.json'
        self.assertEqual(main['files'][binding], role['files'][binding])
        self.assertEqual(role['files'][binding], second['files'][binding])
        provider = tomllib.loads(main['files']['config.toml'])['model_providers']['claude_code']
        self.assertEqual(provider['agent']['command'], self.host['remote_python'])
        self.assertEqual(provider['agent']['args'], ['-X', 'utf8', str(self.home.parent / 'launch.py'),
                         'claude-runner', '--binding', str(self.home / binding)])
        self.assertNotIn('\\', main['files']['config.toml'])
        self.assertEqual(role['auth_metadata'], main['main_auth'])
        self.assertIn('claude_remote.py', main['helper_files'])
        for name, source in main['helper_files'].items():
            compile(source, name, 'exec')

    def test_unprepared_cli_and_windows_paths_are_refused(self):
        for remote_cli in (None, dict(path='C:/claude.exe', version='2.1.282'),
                           dict(path='/usr/bin/claude', version='2.1.100')):
            with self.subTest(remote_cli=remote_cli), self.assertRaises(ValueError):
                render_for_host(self.registry, self.home, self.profile,
                                **{**self.host, 'remote_cli': remote_cli})


class RemoteClaudeContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.owner, self.target = str(uuid4()), str(uuid4())
        self.profile = self.base / 'profiles' / self.owner
        self.revision, self.host, self.identity = 'c' * 64, 'd' * 64, 'e' * 64
        definition = self.profile / 'definitions' / self.revision
        self.binding_path = self.profile / 'codex' / 'claude-bindings' / (self.target + '.json')
        atomic_json(self.profile / 'definitions' / (self.revision + '.json'), dict(
            revision=self.revision, profile_id=self.owner, definition=str(definition), host_identity=self.host))
        self.binding = dict(schema_version=1, owner_profile_id=self.owner, target_profile_id=self.target,
            host_identity=self.host, expected_account_identity=self.identity, settings={},
            cli_path=sys.executable, cli_version='2.1.282')
        atomic_json(definition / 'claude-bindings' / (self.target + '.json'), self.binding)
        self.auth = dict(profileId=self.target, accountIdentity=self.identity,
                         accessToken='synthetic-access', expiresAt=int(time.time()) + 3600)

    def context(self, **auth):
        return RemoteExecution(self.profile, self.revision, self.binding_path,
                               {**self.auth, **auth}, expected_host=self.host)

    def test_runtime_uses_private_remote_paths_and_reinjects_only_approved_token(self):
        context = self.context()
        with patch('remote_helpers.claude_remote.cli_version', return_value='2.1.282'), \
             patch('remote_helpers.claude_remote.prepare_shared_skills', return_value=None), \
             patch.dict(os.environ, {'ANTHROPIC_API_KEY':'PRIVATE_AMBIENT',
                                    'CODEX_MANAGER_CLAUDE_AUTH':'PRIVATE_BROKER'}):
            actual = context.prepare(self.base)
        self.assertEqual(actual['environment']['CLAUDE_CODE_OAUTH_TOKEN'], 'synthetic-access')
        self.assertNotIn('ANTHROPIC_API_KEY', actual['environment'])
        self.assertNotIn('CODEX_MANAGER_CLAUDE_AUTH', actual['environment'])
        self.assertEqual(actual['configuration_directory'], self.base / 'claude/accounts' / self.target)
        self.assertEqual(context.ledger_directory, self.base / 'claude/state' / self.owner)
        self.assertFalse((actual['configuration_directory'] / '.credentials.json').exists())

    def test_wrong_account_expiry_host_and_revision_cannot_launch(self):
        for auth in (dict(profileId=str(uuid4())), dict(accountIdentity='f' * 64),
                     dict(expiresAt=int(time.time()) - 1)):
            with self.subTest(auth=auth), self.assertRaises(ValueError):
                self.context(**auth)
        with self.assertRaises(ValueError):
            RemoteExecution(self.profile, self.revision, self.binding_path, self.auth,
                            expected_host='f' * 64)

    def test_cli_change_requires_repreparation(self):
        with patch('remote_helpers.claude_remote.cli_version', return_value='2.1.283'), \
             self.assertRaisesRegex(ClaudeError, 'changed'):
            self.context().prepare(self.base)

    def test_production_context_runs_fake_cli_and_resumes_same_session_without_auth_files(self):
        from test_manager_claude_runner import FAKE, Incoming, Outgoing
        fake = self.base / 'fake_claude.py'
        fake.write_text("import os\nassert os.environ['CLAUDE_CODE_OAUTH_TOKEN']=='synthetic-access'\n"
                        "assert 'CODEX_MANAGER_CLAUDE_AUTH' not in os.environ\n" + FAKE, encoding='utf-8')
        context = self.context()
        original = context.prepare
        def prepare(cwd):
            result = original(cwd)
            result['cli'] = [sys.executable, str(fake)]
            return result
        def snapshot(turns):
            return dict(turn_ids=turns, fingerprint=digest(turns),
                        turn_fingerprints={turn:digest(turn) for turn in turns})
        sessions = []
        for turn, prior in (('first', []), ('second', ['first'])):
            incoming, events = Incoming(), []
            incoming.send(dict(type='hello', protocol=1, thread_id='remote-task', turn_id=turn,
                claude_profile_id=self.target, cwd=str(self.base), trusted_cwd=True,
                snapshot=snapshot(prior), managed_delegation=True))
            def on_output(message):
                events.append(message)
                if message['type'] == 'ready':
                    self.assertEqual(bool(message['session']), bool(prior))
                    incoming.send(dict(type='run', mode='resume' if prior else 'fresh',
                        blocks=[dict(kind='request', text='fixture')],
                        permission_mode='acceptEdits', permission_prompts='none'))
                elif message['type'] == 'done':
                    incoming.send(dict(type='commit', snapshot=snapshot([*prior, turn])))
            outgoing = Outgoing(on_output)
            with patch.object(context, 'prepare', side_effect=prepare), \
                 patch('remote_helpers.claude_remote.cli_version', return_value='2.1.282'), \
                 patch('remote_helpers.claude_remote.prepare_shared_skills', return_value=None), \
                 patch.dict(os.environ, {'CLAUDE_FIXTURE_SCENARIO':'managed_leaf'}):
                self.assertEqual(serve(context.root, self.target, incoming, outgoing,
                                       execution_context=context), 0, events)
            self.assertEqual(events[-1]['type'], 'committed')
            sessions.append(events[-1]['session_id'])
            self.assertNotIn('synthetic-access', outgoing.getvalue().decode())
        self.assertEqual(sessions[0], sessions[1])
        self.assertFalse(list(self.base.rglob('.credentials.json')))
        self.assertEqual(len(list(context.ledger_directory.rglob('*.json'))), 1)


if __name__ == '__main__':
    unittest.main()
