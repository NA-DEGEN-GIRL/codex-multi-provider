import base64
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core import current_account
from manager_core.store import Store


class CurrentAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'original'
        self.source.mkdir()
        self.store = Store(self.root)
        self.write_auth('original-account')

    def write_auth(self, account):
        payload = base64.urlsafe_b64encode(json.dumps({'exp': time.time() + 3600}).encode()).decode().rstrip('=')
        (self.source / 'auth.json').write_text(json.dumps({'tokens': {'access_token': 'header.' + payload + '.signature',
            'account_id': account, 'refresh_token': 'private-refresh'}}))

    def test_registration_is_idempotent_and_never_copies_or_changes_login(self):
        before = (self.source / 'auth.json').read_bytes()
        profile = current_account.register(self.store, '03', self.source)
        again = current_account.register(self.store, '03', self.source)
        self.assertEqual(profile, again)
        self.assertEqual(len(self.store.read()['profiles']), 1)
        self.assertEqual(profile['auth_mode'], 'source')
        self.assertFalse((Path(profile['home']) / 'auth.json').exists())
        self.assertEqual((self.source / 'auth.json').read_bytes(), before)
        self.assertNotIn('private-refresh', json.dumps(self.store.read()))
        self.assertNotIn('access_token', json.dumps(self.store.read()))

    def test_alias_collision_does_not_overwrite_other_account(self):
        self.store.add_profile('03')
        before = self.store.read()
        with self.assertRaises(ValueError):
            current_account.register(self.store, '03', self.source)
        self.assertEqual(self.store.read(), before)

    def test_source_account_change_is_rejected(self):
        profile = current_account.register(self.store, '03', self.source)
        self.write_auth('different-account')
        with self.assertRaises(RuntimeError):
            current_account.status(self.store, profile['id'])

    def test_verification_uses_only_disposable_probe_and_records_usage(self):
        profile = current_account.register(self.store, '03', self.source)
        before = (self.source / 'auth.json').read_bytes()
        with patch('desktop_launch.find_app', return_value={}), patch('manager_core.login_probe.verification_runtime', return_value='fixture'), \
             patch('manager_core.login_probe.verify', return_value={'usage': {'windows': []}, 'quota_read': True}):
            result = current_account.status(self.store, profile['id'], verify_server=True, root=self.root)
        self.assertTrue(result['server_verified'])
        self.assertEqual(self.store.profile(profile['id'])['login_state'], 'signed_in')
        self.assertEqual((self.source / 'auth.json').read_bytes(), before)

    def test_server_check_reuses_the_service_package_lookup(self):
        profile = current_account.register(self.store, '03', self.source)
        with patch('desktop_launch.cached_app', return_value={'Version': 'fixture'}) as lookup, \
             patch('desktop_launch.find_app', side_effect=AssertionError('uncached package lookup')), \
             patch('manager_core.login_probe.verification_runtime', return_value='fixture') as runtime, \
             patch('manager_core.login_probe.verify', return_value={'quota_read': True}):
            current_account.status(self.store, profile['id'], verify_server=True, root=self.root)
        lookup.assert_called_once_with()
        self.assertEqual(runtime.call_args.args[1], {'Version': 'fixture'})

    def test_usage_refresh_includes_current_login_reference(self):
        from manager_core.native_login import NativeLogin
        profile = current_account.register(self.store, '03', self.source)
        login = NativeLogin(self.root, self.store, None)
        with patch('manager_core.current_account.status', return_value={'state': 'signed_in', 'quota_read': True}) as verify:
            result = login.refresh_all()
        self.assertEqual(result['results'], [{'id': profile['id'], 'state': 'signed_in', 'refreshed': True}])
        self.assertEqual(result['refreshed'], 1)
        self.assertEqual(verify.call_args.kwargs['verify_server'], True)

    def test_source_login_can_sync_ssh_alias_without_copying_credentials(self):
        from manager_core.remote_accounts import RemoteAccounts
        profile = current_account.register(self.store, '03', self.source)
        before = (self.source / 'auth.json').read_bytes()
        remote, native_login = Mock(), Mock()
        remote._run.return_value = Mock(returncode=0, stdout=b'{"state":"synced","identity_matched":true,"changed":false}')
        manager = RemoteAccounts(Path(__file__).resolve().parents[1], self.store, remote, native_login)
        with patch('desktop_launch.find_app', return_value={}), patch('manager_core.login_probe.verification_runtime', return_value='fixture'), \
             patch('manager_core.login_probe.verify', return_value={'quota_read': True}):
            result = manager.sync(profile['id'], 'remote-dev')
        self.assertEqual(result['state'], 'synced')
        native_login.status.assert_not_called()
        request = json.loads(remote._run.call_args.kwargs['input'])
        self.assertEqual(request, {'action': 'sync', 'alias': '03', 'account_fingerprint': profile['account_fingerprint']})
        self.assertEqual((self.source / 'auth.json').read_bytes(), before)
        self.assertFalse((Path(profile['home']) / 'auth.json').exists())
        self.assertEqual(self.store.profile(profile['id'])['ssh_alias_sync']['remote-dev'], result)

    def test_changed_source_login_cannot_change_remote_alias(self):
        from manager_core.remote_accounts import RemoteAccounts
        profile = current_account.register(self.store, '03', self.source)
        self.write_auth('different-account')
        remote = Mock()
        manager = RemoteAccounts(self.root, self.store, remote, Mock())
        with self.assertRaises(RuntimeError):
            manager.sync(profile['id'], 'remote-dev')
        remote._run.assert_not_called()

    def test_changed_native_identity_cannot_change_remote_alias(self):
        from manager_core.remote_accounts import RemoteAccounts
        profile = self.store.add_profile('04')
        self.store.mutate(lambda data: self.store.profile(profile['id'], data).update(auth_mode='native', account_fingerprint='expected'))
        remote, login = Mock(), Mock()
        login.status.return_value = dict(state='signed_in', server_verified=True, account_fingerprint='different')
        with self.assertRaises(RuntimeError):
            RemoteAccounts(self.root, self.store, remote, login).sync(profile['id'], 'remote-dev')
        remote._run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
