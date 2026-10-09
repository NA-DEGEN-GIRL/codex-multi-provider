"""Offline SSH Claude account, packaging and runner boundaries; no provider calls."""
import io
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_manager_execution_preset_auth import capture_logs
from manager_core.claude_auth import ClaudeError
from manager_core.claude_borrowed_auth import read_access_token
from manager_core.claude_profiles import render_for_host, render_execution_role_for_host
from manager_core.providers import ProviderRegistry
from manager_core.store import Store, atomic_json
from manager_core.claude_protocol import read_message
from manager_core.claude_runner import digest, serve
from remote_helpers import claude_remote
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
        # Never start the official CLI in tests; a renewal is simulated per test.
        self.refresh_patch = patch('manager_core.claude_borrowed_auth._refresh_with_cli')
        self.refresh_mock = self.refresh_patch.start()
        self.addCleanup(self.refresh_patch.stop)

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

    def test_expired_token_is_renewed_once_by_the_official_cli_then_read(self):
        renewed = int(time.time()) + 8 * 3600
        self.credentials['claudeAiOauth']['expiresAt'] = (int(time.time()) - 3600) * 1000
        atomic_json(self.directory / '.credentials.json', self.credentials)
        def renew(profile_id):
            self.assertEqual(profile_id, self.profile['id'])
            self.credentials['claudeAiOauth'].update(accessToken='renewed-access', expiresAt=renewed * 1000)
            atomic_json(self.directory / '.credentials.json', self.credentials)
        self.refresh_mock.side_effect = renew
        self.assertEqual(self.read(), dict(accessToken='renewed-access', expiresAt=renewed,
                                         accountIdentity=self.identity))
        self.assertEqual(self.refresh_mock.call_count, 1)
        # A current token never starts the CLI.
        self.read()
        self.assertEqual(self.refresh_mock.call_count, 1)

    def test_failed_renewal_still_refuses_and_is_not_retried_within_cooldown(self):
        from manager_core import claude_borrowed_auth as borrowed
        self.refresh_patch.stop()
        calls = []
        self.credentials['claudeAiOauth']['expiresAt'] = (int(time.time()) - 3600) * 1000
        atomic_json(self.directory / '.credentials.json', self.credentials)
        with patch('manager_core.claude_usage_terminal.query', side_effect=lambda *a, **k: calls.append(a)),              patch.dict(borrowed._REFRESH_ATTEMPTS, clear=True):
            for _ in range(2):
                with self.assertRaisesRegex(ClaudeError, 'current local login'):
                    self.read()
        self.assertEqual(len(calls), 1)
        self.refresh_patch.start()

    def write(self, token, expires):
        self.credentials['claudeAiOauth'].update(accessToken=token, expiresAt=expires * 1000)
        atomic_json(self.directory / '.credentials.json', self.credentials)

    def test_token_inside_the_cli_renewal_window_is_renewed_before_it_is_lent(self):
        logs = capture_logs(self)
        renewed = int(time.time()) + 8 * 3600
        # Four minutes left: the official CLI renews below five, and a token lent
        # as it is would expire during the remote turn that borrowed it.
        self.write('dummy-token-1', int(time.time()) + 240)
        self.refresh_mock.side_effect = lambda profile_id: self.write('dummy-token-2', renewed)
        self.assertEqual(self.read(), dict(accessToken='dummy-token-2', expiresAt=renewed,
                                         accountIdentity=self.identity))
        self.assertEqual(self.refresh_mock.call_count, 1)
        # Above the floor the token is lent unchanged, and it still clears the
        # runtime's 120 s and the SSH helper's 30 s checks.
        self.refresh_mock.reset_mock()
        expires = int(time.time()) + 290
        self.write('dummy-token-3', expires)
        self.assertEqual(self.read(), dict(accessToken='dummy-token-3', expiresAt=expires,
                                         accountIdentity=self.identity))
        self.refresh_mock.assert_not_called()
        self.assertGreater(expires, int(time.time()) + 120)
        # Inside the window with a renewal the CLI declined: refused, not lent.
        self.refresh_mock.side_effect = None
        self.write('dummy-token-4', int(time.time()) + 240)
        with self.assertRaises(ClaudeError) as caught:
            self.read()
        self.assertNotIn('dummy-token', repr((caught.exception, caught.exception.args)))
        self.assertNotIn('dummy-token', repr([record.getMessage() for record in logs]))

    def test_turn_that_waited_for_another_renewal_reads_the_renewed_token(self):
        from manager_core import claude_borrowed_auth as borrowed
        logs = capture_logs(self)
        renewed = int(time.time()) + 8 * 3600
        self.write('dummy-token-1', int(time.time()) - 60)
        entered, release, queries, results = threading.Event(), threading.Event(), [], {}
        self.addCleanup(release.set)
        def query(*args, **kwargs):
            queries.append(args)
            entered.set()
            release.wait(5)
            self.write('dummy-token-2', renewed)
            return None, None
        self.refresh_patch.stop()
        renew = borrowed._refresh_with_cli
        def read(name):
            results[name] = self.read()
        with patch('manager_core.claude_usage_terminal.query', side_effect=query), \
                patch.dict(borrowed._REFRESH_ATTEMPTS, clear=True), \
                patch.object(borrowed, '_refresh_with_cli', side_effect=renew) as refresh:
            first = threading.Thread(target=read, args=('first',))
            first.start()
            self.assertTrue(entered.wait(2))
            # The second turn also found the expired token and asks for a
            # renewal while the first one is still running.
            second = threading.Thread(target=read, args=('second',))
            second.start()
            deadline = time.monotonic() + 2
            while refresh.call_count < 2 and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertEqual(refresh.call_count, 2)
            release.set()
            first.join(5)
            second.join(5)
        self.refresh_patch.start()
        # The cooldown skipped a second CLI run; the re-read found its result.
        self.assertEqual(len(queries), 1)
        expected = dict(accessToken='dummy-token-2', expiresAt=renewed, accountIdentity=self.identity)
        self.assertEqual(results, dict(first=expected, second=expected))
        self.assertNotIn('dummy-token', repr([record.getMessage() for record in logs]))

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
        # Helpers prepared from this authority accept a credential's source (rev 121 stage 2).
        self.assertEqual(main['main_auth']['credential_sources'], 1)
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
        # The token travels apart from the CLI environment; the runner hands it to each launch by pipe.
        self.assertEqual(actual['lent_token'], 'synthetic-access')
        self.assertNotIn('synthetic-access', json.dumps(actual['environment']))
        # The runner's login metadata names the lent login's owner and expiry, never its token.
        self.assertEqual(actual['borrowed_auth'], dict(profileId=self.target, accountIdentity=self.identity,
                                                       expiresAt=self.auth['expiresAt']))
        self.assertNotIn('ANTHROPIC_API_KEY', actual['environment'])
        self.assertNotIn('CODEX_MANAGER_CLAUDE_AUTH', actual['environment'])
        self.assertEqual(actual['configuration_directory'], self.base / 'claude/accounts' / self.target)
        self.assertEqual(context.ledger_directory, self.base / 'claude/state' / self.owner)
        self.assertFalse((actual['configuration_directory'] / '.credentials.json').exists())

    def test_wrong_account_expiry_host_and_revision_cannot_launch(self):
        for auth in (dict(profileId=str(uuid4())), dict(accountIdentity='f' * 64),
                     dict(expiresAt=int(time.time()) - 1), dict(accessToken='synthetic access')):
            with self.subTest(auth=auth), self.assertRaises(ValueError):
                self.context(**auth)
        with self.assertRaises(ValueError):
            RemoteExecution(self.profile, self.revision, self.binding_path, self.auth,
                            expected_host='f' * 64)

    def test_cli_change_requires_repreparation(self):
        with patch('remote_helpers.claude_remote.cli_version', return_value='2.1.283'), \
             self.assertRaisesRegex(ClaudeError, 'changed'):
            self.context().prepare(self.base)

    def test_a_credential_names_its_source_only_in_the_six_key_form(self):
        saved = '44444444-4444-4444-8444-444444444444'
        year = int(time.time()) + 365 * 86400
        accepted = [dict(), dict(credentialSource='windowsLogin', credentialId=None),
                    dict(credentialSource='longLivedToken', credentialId=saved, expiresAt=year)]
        for auth in accepted:
            with self.subTest(auth=auth), \
                 patch('remote_helpers.claude_remote.cli_version', return_value='2.1.282'), \
                 patch('remote_helpers.claude_remote.prepare_shared_skills', return_value=None):
                actual = self.context(**auth).prepare(self.base)
                # The source travels with the owner and expiry, never the token.
                expected = dict(profileId=self.target, accountIdentity=self.identity,
                                expiresAt=auth.get('expiresAt', self.auth['expiresAt']))
                expected.update({key: auth[key] for key in ('credentialSource', 'credentialId') if key in auth})
                self.assertEqual(actual['borrowed_auth'], expected)
        refused = [dict(credentialSource='windowsLogin'), dict(credentialId=None),
                   dict(credentialSource='windowsLogin', credentialId=saved),
                   dict(credentialSource='longLivedToken', credentialId=None),
                   dict(credentialSource='longLivedToken', credentialId=saved.replace('4', 'A')),
                   dict(credentialSource='longLivedToken', credentialId='../' + saved),
                   dict(credentialSource='elsewhere', credentialId=None),
                   dict(credentialSource='windowsLogin', credentialId=None, extra=1)]
        for auth in refused:
            with self.subTest(auth=auth), self.assertRaises(ValueError) as caught:
                self.context(**auth)
            self.assertNotIn('synthetic-access', str(caught.exception))

    def test_production_context_runs_fake_cli_and_resumes_same_session_without_auth_files(self):
        from test_manager_claude_runner import FAKE, Incoming, Outgoing
        fake = self.base / 'fake_claude.py'
        fake.write_text("import os\nassert 'CODEX_MANAGER_CLAUDE_AUTH' not in os.environ\n" + FAKE, encoding='utf-8')
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
                 patch.dict(os.environ, {'CLAUDE_FIXTURE_SCENARIO':'managed_leaf',
                                         'CLAUDE_FIXTURE_TOKEN':'synthetic-access'}):
                self.assertEqual(serve(context.root, self.target, incoming, outgoing,
                                       execution_context=context), 0, events)
            self.assertEqual(events[-1]['type'], 'committed')
            sessions.append(events[-1]['session_id'])
            self.assertNotIn('synthetic-access', outgoing.getvalue().decode())
        self.assertEqual(sessions[0], sessions[1])
        self.assertFalse(list(self.base.rglob('.credentials.json')))
        self.assertEqual(len(list(context.ledger_directory.rglob('*.json'))), 1)


class RemoteRunnerEntryTests(unittest.TestCase):
    """How the lent credential reaches the SSH runner entry point; nothing is launched."""
    credentials = dict(profileId=str(uuid4()), accessToken='dummy-token-1', expiresAt=2000000000,
                       accountIdentity='e' * 64)
    hello = dict(type='hello', protocol=1)

    def run_main(self, stdin, environment):
        # A file, not a pipe: an oversized message must not block the writer.
        source = tempfile.TemporaryFile()
        self.addCleanup(source.close)
        source.write(stdin)
        source.seek(0)
        reader = source.fileno()
        output, seen = io.BytesIO(), {}
        class Execution:
            def __init__(self, profile, revision, binding, auth):
                seen['auth'] = auth
                self.root, self.binding = Path(profile), dict(target_profile_id='target')
        def serve(root, profile_id, incoming=None, execution_context=None):
            # The runner continues with the very next message on the same stream.
            seen['next'] = read_message(incoming)
            seen['environment'] = [os.environ.get(name) for name in
                                   ('CODEX_MANAGER_CLAUDE_AUTH', 'CODEX_MANAGER_CLAUDE_AUTH_CHANNEL')]
            return 0
        with patch.dict(os.environ, dict(environment, CODEX_MANAGER_PROFILE_DIR=str(Path.cwd()))), \
             patch.object(claude_remote, 'RemoteExecution', Execution), patch.object(claude_remote, 'serve', serve), \
             patch.object(claude_remote, '_hide_process') as hide, \
             patch('sys.stdin', type('Stdin', (), {'fileno': lambda self: reader})()), \
             patch('sys.stdout', type('Stdout', (), {'buffer': output})()):
            code = claude_remote.main(['claude-runner', '--binding', 'binding.json'])
        self.assertEqual(hide.call_count, 1)
        return code, [json.loads(line) for line in output.getvalue().splitlines()], seen

    def line(self, value):
        return json.dumps(value).encode() + b'\n'

    def test_current_runtime_sends_the_credential_on_stdin_before_the_hello(self):
        code, output, seen = self.run_main(
            self.line(dict(type='auth_init', credentials=self.credentials)) + self.line(self.hello),
            {'CODEX_MANAGER_CLAUDE_AUTH_CHANNEL': 'stdin'})
        self.assertEqual(code, 0)
        self.assertEqual(output, [dict(type='auth_channel', protocol=1)])
        self.assertEqual((seen['auth'], seen['next'], seen['environment']), (self.credentials, self.hello, [None, None]))

    def test_older_runtime_still_lends_through_the_environment(self):
        code, output, seen = self.run_main(self.line(self.hello),
                                           {'CODEX_MANAGER_CLAUDE_AUTH': json.dumps(self.credentials)})
        self.assertEqual((code, output), (0, []))
        self.assertEqual((seen['auth'], seen['next'], seen['environment']), (self.credentials, self.hello, [None, None]))

    def test_malformed_or_oversized_credential_messages_are_refused_without_echo(self):
        for stdin in (self.line(dict(type='auth_init', credentials=self.credentials, extra=1)),
                      self.line(dict(type='hello', credentials=self.credentials)),
                      self.line(dict(type='auth_init', credentials=self.credentials))[:-1],
                      b'{"type":"auth_init","credentials":"' + b'x' * 70000 + b'"}\n',
                      b'not json dummy-token-1\n', b''):
            with self.subTest(stdin=stdin[:40]):
                code, output, seen = self.run_main(stdin, {'CODEX_MANAGER_CLAUDE_AUTH_CHANNEL': 'stdin'})
                self.assertEqual(code, 1)
                self.assertEqual([message['type'] for message in output], ['auth_channel', 'refused'])
                self.assertNotIn('dummy-token', json.dumps(output))
                self.assertNotIn('auth', seen)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux /proc semantics.')
    def test_runner_process_files_are_hidden_from_same_user_processes(self):
        helpers = Path(__file__).resolve().parents[1] / 'scripts'
        script = ('import os, subprocess, sys\n'
                  'sys.path[:0] = [%r, %r]\n'
                  'from remote_helpers.claude_remote import _hide_process\n'
                  'probe = "import sys; open(\'/proc/%%s/environ\' %% sys.argv[1], \'rb\').read()"\n'
                  'before = subprocess.run([sys.executable, "-c", probe, str(os.getpid())]).returncode\n'
                  '_hide_process()\n'
                  'after = subprocess.run([sys.executable, "-c", probe, str(os.getpid())], stderr=subprocess.DEVNULL).returncode\n'
                  'print(before, after)\n') % (str(helpers), str(helpers / 'remote_helpers'))
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.stdout.split(), ['0', '1'], result.stderr)


if __name__ == '__main__':
    unittest.main()
