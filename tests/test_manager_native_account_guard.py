import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.native_account_guard import NativeAccountGuard
from manager_core.proxy_auth import account_fingerprint


class NativeAccountGuardTests(unittest.TestCase):
    def test_shared_login_allows_authentication_and_history_but_binds_work_to_one_account(self):
        for registered in (False,True):
            with self.subTest(registered=registered),tempfile.TemporaryDirectory() as directory:
                from manager_core.proxy_auth import account_fingerprint
                env={'CODEX_HOME':directory,'CODEX_MANAGER_NATIVE_LOGIN':'1'}
                if registered:env['CODEX_MANAGER_EXPECTED_ACCOUNT_FINGERPRINT']=account_fingerprint('first')
                guard=NativeAccountGuard(env)
                self.assertTrue(guard.enabled)
                for method in ('initialize','account/login/start','account/login/cancel','account/read',
                        'thread/list','thread/read','configRequirements/read','config/batchWrite'):
                    self.assertFalse(guard.blocks({'method':method}),method)
                self.assertTrue(guard.blocks({'method':'turn/start'}))
                def save(account):
                    payload=base64.urlsafe_b64encode(json.dumps({'exp':100}).encode()).decode().rstrip('=')
                    (Path(directory)/'auth.json').write_text(json.dumps({'tokens':{
                        'account_id':account,'access_token':'fixture.'+payload+'.signature'}}))
                save('first')
                self.assertFalse(guard.blocks({'method':'turn/start'}))
                save('other')
                for method in ('thread/resume','turn/start','thread/name/set','command/exec'):
                    self.assertTrue(guard.blocks({'method':method}),method)
                self.assertFalse(guard.blocks({'method':'account/logout'}))
                self.assertFalse(guard.blocks({'method':'account/login/start'}))
                save('first')
                self.assertFalse(guard.blocks({'method':'turn/start'}))

    def test_identity_change_blocks_new_work_but_preserves_drain_and_records(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            guard = NativeAccountGuard({'CODEX_HOME': directory,
                'CODEX_MANAGER_EXPECTED_ACCOUNT_FINGERPRINT': account_fingerprint('first')})
            def save(account):
                # The expired access credential must remain refreshable by the
                # native runtime when its identity is still the expected one.
                payload = base64.urlsafe_b64encode(json.dumps({'exp': 100,
                    'https://api.openai.com/auth': {'chatgpt_account_id': account}}).encode()).decode().rstrip('=')
                (home / 'auth.json').write_text(json.dumps({'tokens': {
                    'account_id': account, 'access_token': 'fixture.' + payload + '.signature'}}))
            save('first')
            self.assertTrue(guard.matches())
            self.assertFalse(guard.blocks({'id': 1, 'method': 'turn/start'}))
            self.assertTrue(guard.blocks({'id': 2, 'method': 'account/login/start'}))
            save('second')
            before = (home / 'auth.json').read_bytes()
            self.assertFalse(guard.matches())
            for method in ('thread/resume', 'turn/start', 'command/exec', 'unknown/future'):
                self.assertTrue(guard.blocks({'id': 3, 'method': method}))
            for message in ({'method': 'thread/read'}, {'method': 'turn/interrupt'},
                            {'method': 'initialize'}, {'id': 9, 'result': {}}):
                self.assertFalse(guard.blocks(message))
            self.assertEqual((home / 'auth.json').read_bytes(), before)
            (home / 'auth.json').unlink()
            self.assertFalse(guard.matches())

    def test_borrowed_auth_and_unbound_login_keep_their_existing_paths(self):
        for environment in ({}, {'CODEX_MANAGER_AUTH_SOURCE': 'source',
                                  'CODEX_MANAGER_EXPECTED_ACCOUNT_FINGERPRINT': 'expected'}):
            guard = NativeAccountGuard(environment)
            self.assertFalse(guard.enabled)
            self.assertFalse(guard.blocks({'method': 'turn/start'}))


if __name__ == '__main__':
    unittest.main()
