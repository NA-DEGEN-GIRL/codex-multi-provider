"""Offline connection-bound account borrowing; no real credentials or providers."""
from concurrent.futures import ThreadPoolExecutor
import copy
import logging
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.claude_auth import ClaudeError
from manager_core.execution_preset_auth import CAPABILITY, METHOD, ExecutionPresetAuthProxy
from manager_core.proxy_auth import AuthProxyResult, AuthTokens, account_fingerprint

REFUSED = {'code': -32042, 'message': 'The selected managed account is unavailable or changed.'}


def capture_logs(test):
    """Collect every log record emitted during the test, at any level."""
    records, root = [], logging.getLogger()
    handler = logging.Handler(logging.DEBUG)
    handler.emit = records.append
    previous = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    test.addCleanup(root.setLevel, previous)
    test.addCleanup(root.removeHandler, handler)
    return records


class CountingLock:
    """Counts entries so a test knows every waiting request joined the read."""
    def __init__(self): self.lock, self.entries = threading.Lock(), 0
    def __enter__(self):
        self.lock.acquire()
        self.entries += 1
    def __exit__(self, *_): self.lock.release()


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
        self.broker._pending[1].future.result(timeout=2)
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

    def claude_params(self):
        identity = 'd' * 64
        self.store.profiles[self.peer].update(auth_mode='claude_code', claude_account_identity=identity)
        self.broker.authority['main_auth'] = dict(kind='claude', auth_source='manager_proxy',
                                                profile_id=self.peer, expected_account_identity=identity)
        return dict(ownerProfileId=self.owner, roleId=None, profileId=self.peer, kind='claude',
                    expectedAccountFingerprint=None, expectedAccountIdentity=identity)

    def send(self, request_ids, params):
        for request_id in request_ids:
            message = {'id':request_id, 'method':METHOD, 'params':params}
            self.assertEqual(self.broker.process('runtime', message).runtime, [])

    def wait_for(self, condition):
        deadline = time.monotonic() + 2
        while not condition():
            if time.monotonic() > deadline: self.fail('fixture did not reach the expected state')
            time.sleep(.005)

    def answers(self, count):
        found = []
        def collect():
            found.extend(self.broker.poll().runtime)
            return len(found) >= count
        self.wait_for(collect)
        self.assertEqual(len(found), count)
        return sorted(found, key=lambda item: item['id'])

    def assert_secret_free(self, logs, *outputs):
        text = repr([record.getMessage() for record in logs]) + repr(outputs)
        self.assertNotIn('dummy-token', text)

    def shared_claude_read(self, outcome):
        """Every request waits inside one blocked read until the test releases it."""
        lock, entered, release, calls = CountingLock(), threading.Event(), threading.Event(), []
        self.addCleanup(release.set)
        self.broker._reads_lock = lock
        def read(root, profile, expected):
            calls.append(profile)
            entered.set()
            release.wait(5)
            return outcome(len(calls), expected)
        self.broker._claude_reader = read
        return lock, entered, release, calls

    def test_concurrent_turns_of_one_claude_login_share_one_read(self):
        logs, params = capture_logs(self), self.claude_params()
        lock, entered, release, calls = self.shared_claude_read(lambda count, identity: dict(
            accessToken=f'dummy-token-{count}', expiresAt=2000000000, accountIdentity=identity))
        self.send(range(1, 7), params)
        self.assertTrue(entered.wait(2))
        # The first request reads; the other five join it instead of queueing
        # their own read (and their own renewal) behind it.
        self.wait_for(lambda: lock.entries >= 6)
        release.set()
        answers = self.answers(6)
        self.assertEqual([answer['id'] for answer in answers], list(range(1, 7)))
        self.assertEqual({answer['result']['accessToken'] for answer in answers}, {'dummy-token-1'})
        self.assertEqual(calls, [self.peer])
        self.assertEqual(self.broker._claude_reads, {})
        # A finished read is never reused: the next turn reads the login again.
        self.assertEqual(self.request(params, 7)['result']['accessToken'], 'dummy-token-2')
        self.assert_secret_free(logs)

    def test_failed_shared_claude_read_refuses_every_waiting_turn(self):
        logs, params = capture_logs(self), self.claude_params()
        def refuse(count, identity):
            raise ClaudeError('claude_account_unavailable', 'The selected Claude account needs a current local login.')
        lock, entered, release, calls = self.shared_claude_read(refuse)
        self.send(range(1, 4), params)
        self.assertTrue(entered.wait(2))
        self.wait_for(lambda: lock.entries >= 3)
        release.set()
        answers = self.answers(3)
        self.assertEqual(answers, [{'id':request_id, 'error':REFUSED} for request_id in range(1, 4)])
        self.assertEqual(len(calls), 1)
        self.assert_secret_free(logs, answers)

    def queued_behind_slow_read(self):
        """One worker: request 1 blocks its read and request 2 waits for the worker."""
        now = [100.0]
        clock = patch('manager_core.execution_preset_auth._clock', lambda: now[0])
        clock.start()
        self.addCleanup(clock.stop)
        self.broker._workers.shutdown()
        self.broker._workers = ThreadPoolExecutor(max_workers=1)
        entered, release, reads = threading.Event(), threading.Event(), []
        self.addCleanup(release.set)
        def read(home):
            reads.append(home)
            if len(reads) == 1:
                entered.set()
                release.wait(5)
            return AuthTokens(f'dummy-token-{len(reads)}', 'selected-account')
        self.broker._reader = read
        self.send((1, 2), self.params())
        self.assertTrue(entered.wait(2))
        return now, release, reads

    def test_read_deadline_runs_from_its_start_not_from_queueing(self):
        logs = capture_logs(self)
        now, release, reads = self.queued_behind_slow_read()
        now[0] = 125.5
        # Request 1 has been reading for 25.5 s. Request 2 arrived at the same
        # time but only waited for the worker, so it is not refused yet.
        refused = self.broker.poll().runtime
        self.assertEqual(refused, [{'id':1, 'error':REFUSED}])
        release.set()
        self.assertEqual(self.answers(1), [{'id':2, 'result':dict(
            kind='openai', accessToken='dummy-token-2', chatgptAccountId='selected-account',
            expiresAt=None, accountIdentity=None)}])
        self.assert_secret_free(logs, refused)

    def test_request_waiting_for_a_worker_is_still_bounded(self):
        logs = capture_logs(self)
        now, release, reads = self.queued_behind_slow_read()
        now[0] = 127.9
        refused = self.broker.poll().runtime
        self.assertEqual(refused, [{'id':1, 'error':REFUSED}])
        # The runtime stops waiting 30 s after asking; a request that never got
        # a worker is refused before that and its read never starts.
        now[0] = 128.0
        refused += self.broker.poll().runtime
        self.assertEqual(refused, [{'id':request_id, 'error':REFUSED} for request_id in (1, 2)])
        release.set()
        self.broker._workers.shutdown(wait=True)
        self.assertEqual(len(reads), 1)
        self.assertEqual(self.broker.poll().runtime, [])
        self.assert_secret_free(logs, refused)


if __name__ == '__main__': unittest.main()
