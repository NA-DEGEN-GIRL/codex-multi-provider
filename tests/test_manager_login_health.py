import base64
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core import current_account, login_health
from manager_core.instances import Instances
from manager_core.native_login import NativeLogin
from manager_core.store import Store


class LoginRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'original'
        self.source.mkdir()
        self.store = Store(self.root)
        self.auth(self.source, '03')
        self.profile = current_account.register(self.store, '03', self.source)
        self.instances = Instances(self.root, self.store, None)

    def auth(self, home, account, expired=False):
        payload = base64.urlsafe_b64encode(json.dumps({'exp': time.time() + (-10 if expired else 3600)}).encode()).decode().rstrip('=')
        (home / 'auth.json').write_text(json.dumps({'tokens': {'access_token': 'header.' + payload + '.signature',
            'account_id': account, 'refresh_token': 'secret-fixture'}}))

    def test_original_account_switch_is_detected_before_any_window_is_launched(self):
        self.auth(self.source, 'new-account')
        before = self.store.read()
        with patch('manager_core.instances.subprocess.Popen') as launch, patch.object(self.instances, 'prepare') as prepare:
            for _ in range(3):
                with self.assertRaisesRegex(RuntimeError, '03.*등록된 계정'):
                    self.instances.show(self.profile['id'])
            launch.assert_not_called()
            prepare.assert_not_called()
        self.assertEqual(self.store.read(), before)
        observed = self.instances.observe(self.profile)
        self.assertEqual(observed['login_health']['reason'], 'account_mismatch')
        self.assertNotIn('secret-fixture', json.dumps(observed))

    def test_other_profile_is_not_blocked_and_restored_source_can_be_used_again(self):
        self.auth(self.source, 'new-account')
        other = dict(self.profile, source_home=str(self.root / 'other'))
        Path(other['source_home']).mkdir()
        self.auth(Path(other['source_home']), '03')
        self.assertFalse(login_health.inspect(other)['blocks_launch'])
        self.auth(self.source, '03')
        self.assertFalse(login_health.inspect(self.profile)['blocks_launch'])

    def test_source_repair_uses_private_login_preserves_identity_and_rejects_wrong_account(self):
        self.auth(self.source, 'new-account')
        original = (self.source / 'auth.json').read_bytes()
        login = NativeLogin(self.root, self.store, self.instances)
        with patch.object(self.instances, 'prepare'):
            prepared = login.prepare(self.profile['id'])
        self.assertEqual(prepared['auth_mode'], 'native')
        self.assertEqual(prepared['account_fingerprint'], self.profile['account_fingerprint'])
        self.assertEqual(prepared['login_state'], 'awaiting_user')
        home = Path(prepared['home'])
        self.assertFalse((home / 'auth.json').exists())
        self.auth(home, 'wrong')
        self.assertEqual(login.status(prepared['id'])['state'], 'account_changed')
        self.auth(home, '03')
        self.assertEqual(login.status(prepared['id'])['state'], 'credential_saved')
        self.assertEqual((self.source / 'auth.json').read_bytes(), original)

    def test_borrowed_expiry_blocks_start_but_native_oauth_can_refresh_itself(self):
        self.auth(self.source, '03', expired=True)
        self.assertEqual(login_health.inspect(self.profile)['reason'], 'expired_access_token')
        native = dict(self.profile, auth_mode='native', home=str(self.source))
        self.assertFalse(login_health.inspect(native)['blocks_launch'])
        packaged = dict(self.profile, runtime_channel='packaged')
        self.assertFalse(login_health.inspect(packaged)['blocks_launch'])

    def test_show_existing_window_does_not_revalidate_launch_credentials(self):
        self.auth(self.source, '03', expired=True)
        with patch.object(self.instances, 'observe', return_value={'status':'running', 'executable_path':'unused'}), \
                patch('manager_core.instances.subprocess.Popen') as spawn:
            result = self.instances.show(self.profile['id'], reopen_existing=False)
        self.assertEqual(result['state'], 'existing')
        spawn.assert_not_called()

    def test_borrowed_account_status_does_not_require_another_login(self):
        login = NativeLogin(self.root, self.store, self.instances)
        before = (self.source / 'auth.json').read_bytes()
        result = login.status(self.profile['id'])
        self.assertEqual(result['state'], 'credential_saved')
        self.assertFalse(result['server_verified'])
        with patch.object(self.instances,'observe',return_value={'status':'running'}), \
                patch.object(self.instances,'show',return_value={'state':'existing'}) as show:
            self.assertEqual(login.open(self.profile['id'])['state'], 'existing')
            show.assert_called_once_with(self.profile['id'])
        self.assertEqual(self.store.profile(self.profile['id'])['auth_mode'], 'source')
        self.assertEqual((self.source / 'auth.json').read_bytes(), before)


if __name__ == '__main__': unittest.main()
