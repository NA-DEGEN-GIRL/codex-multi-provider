"""Offline connection-bound account borrowing; no real credentials or providers."""
import copy
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.execution_preset_auth import CAPABILITY, METHOD, ExecutionPresetAuthProxy
from manager_core.proxy_auth import AuthProxyResult, AuthTokens, account_fingerprint


class ParentAuth:
    state, bound, account_fingerprint = 'ready', True, 'parent-only'

    def __init__(self): self.seen = []
    def process(self, direction, message):
        self.seen.append((direction, message))
        return AuthProxyResult(**{'runtime' if direction == 'frontend' else 'frontend': [message]})
    def poll(self): return AuthProxyResult()


class Store:
    def __init__(self, root, profiles): self.root, self.profiles = root, profiles
    def profile(self, key): return copy.deepcopy(self.profiles[key])


class BorrowingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.owner, self.peer, self.generation = (str(uuid4()) for _ in range(3))
        self.fingerprint = account_fingerprint('selected-account')
        self.authority = dict(schema_version=1, profile_id=self.owner, host_id='ssh:fixture',
                              host_identity='a' * 64, revision='b' * 64, roles={'approved': dict(
                                  model_provider='openai', auth_source='manager_proxy', profile_id=self.peer,
                                  expected_account_fingerprint=self.fingerprint)})
        binding = dict(alias='fixture', host_identity='a' * 64, revision='b' * 64, prepared=True)
        self.store = Store(Path(temporary.name), {
            self.owner: dict(id=self.owner, generation=self.generation, remote_bindings=[binding]),
            self.peer: dict(id=self.peer, auth_mode='native', home=str(Path(temporary.name) / 'peer'),
                            account_fingerprint=self.fingerprint)})
        self.parent, self.reads, self.token = ParentAuth(), [], 'first-token'
        def read(home):
            self.reads.append(home)
            return AuthTokens(self.token, 'selected-account')
        self.broker = ExecutionPresetAuthProxy(self.parent, self.store, self.authority, self.generation, token_reader=read)
        self.addCleanup(self.broker.close)
        self.initialize()

    def initialize(self, version=1):
        result = self.broker.process('frontend', {'id':'init', 'method':'initialize', 'params':{}})
        self.assertEqual(result.runtime[0]['params']['capabilities']['extensions'][CAPABILITY], {'profileId':self.owner})
        self.broker.process('runtime', {'id':'init', 'result':{'executionPresetsVersion':version}})

    def params(self):
        return dict(ownerProfileId=self.owner, roleId='approved', profileId=self.peer, kind='openai',
                    expectedAccountFingerprint=self.fingerprint, expectedAccountIdentity=None)

    def request(self, params=None, request_id=1):
        immediate = self.broker.process('runtime', {'id':request_id, 'method':METHOD,
                                                  'params':self.params() if params is None else params})
        self.assertEqual(immediate.frontend, [])
        if immediate.runtime: return immediate.runtime[0]
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            result = self.broker.poll()
            if result.runtime: return result.runtime[0]
            time.sleep(.005)
        self.fail('credential fixture did not finish')

    def test_selected_account_refresh_and_parent_isolation(self):
        self.assertEqual(self.request()['result']['accessToken'], 'first-token')
        self.token = 'renewed-token'
        self.assertEqual(self.request(request_id=2)['result']['accessToken'], 'renewed-token')
        self.assertEqual(self.reads, [self.store.profiles[self.peer]['home']] * 2)
        self.assertEqual((self.parent.state, self.parent.account_fingerprint), ('ready', 'parent-only'))
        self.assertNotIn('first-token', repr(self.parent.seen))
        self.assertEqual(self.broker.process('frontend', {'id':1, 'result':{'accessToken':'spoof'}}).runtime, [])

    def test_unknown_or_modified_binding_never_reads_a_source(self):
        changes = [dict(roleId='other'), dict(profileId=self.owner), dict(ownerProfileId=self.peer),
                   dict(expectedAccountFingerprint='c' * 64), dict(path='untrusted'), dict(kind='claude')]
        for index, change in enumerate(changes):
            params = self.params(); params.update(change)
            self.assertIn('error', self.request(params, index))
        self.assertEqual(self.reads, [])
        self.authority['roles']['other'] = self.authority['roles']['approved']
        params = self.params(); params['roleId'] = 'other'
        self.assertIn('error', self.request(params, 30), 'published mutable metadata cannot expand the pinned authority')

    def test_drift_missing_runtime_proof_and_disconnect_fail_closed(self):
        self.broker.execution_presets_version = 0
        self.assertIn('error', self.request())
        self.broker.execution_presets_version = 1
        self.store.profiles[self.owner]['remote_bindings'][0]['revision'] = 'c' * 64
        self.assertIn('error', self.request(request_id=2))
        self.store.profiles[self.owner]['remote_bindings'][0]['revision'] = 'b' * 64
        self.store.profiles[self.peer]['account_fingerprint'] = 'c' * 64
        self.assertIn('error', self.request(request_id=3))
        self.broker.close()
        self.assertIn('error', self.request(request_id=4))
        self.assertEqual(self.reads, [])

    def test_slow_read_does_not_block_stream_and_closed_connection_drops_result(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def read(_):
            entered.set(); release.wait(2)
            return AuthTokens('delayed-token', 'selected-account')
        self.broker._reader = read
        self.assertEqual(self.broker.process('runtime', {'id':1, 'method':METHOD, 'params':self.params()}).runtime, [])
        self.assertTrue(entered.wait(1))
        message = {'id':2,'method':'thread/loaded/list','params':{}}
        self.assertEqual(self.broker.process('frontend', message).runtime, [message])
        self.broker.close(); release.set()
        self.assertEqual(self.broker.poll().runtime, [])

    def test_binding_change_after_read_but_before_transmission_discards_token(self):
        self.broker.process('runtime', {'id':1, 'method':METHOD, 'params':self.params()})
        self.broker._pending[1][0].result(timeout=2)
        self.store.profiles[self.owner]['generation'] = str(uuid4())
        outcome = self.broker.poll()
        self.assertIn('error', outcome.runtime[0])
        self.assertNotIn('first-token', str(outcome.runtime))

    def test_claude_main_uses_explicit_prepared_identity_and_no_parent_tokens(self):
        identity = 'd' * 64
        self.store.profiles[self.peer].update(auth_mode='claude_code', claude_account_identity=identity)
        self.broker.authority['main_auth'] = dict(kind='claude', auth_source='manager_proxy',
                                                profile_id=self.peer, expected_account_identity=identity)
        calls = []
        def read(root, profile, expected):
            calls.append((root, profile, expected))
            return dict(accessToken='claude-token', expiresAt=2000000000, accountIdentity=identity)
        self.broker._claude_reader = read
        params = dict(ownerProfileId=self.owner, roleId=None, profileId=self.peer, kind='claude',
                      expectedAccountFingerprint=None, expectedAccountIdentity=identity)
        self.assertEqual(self.request(params)['result'], dict(kind='claude', accessToken='claude-token',
                         chatgptAccountId=None, expiresAt=2000000000, accountIdentity=identity))
        self.assertEqual(calls, [(self.store.root,self.peer,identity)])
        self.assertEqual(self.reads, [])


if __name__ == '__main__': unittest.main()
