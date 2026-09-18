"""Account-bound SSH readiness and the non-destructive post-restore wait."""
from copy import deepcopy
import json
import unittest
from unittest.mock import MagicMock
from uuid import uuid4

from manager_core.remote_readiness import RemoteReadiness
from manager_core.runtime_admin import AdminError, sanitize_result
from manager_core.ssh_runtime_control import SshRuntimeControl
from test_manager_ssh_runtime_control import Auth
import test_manager_profile_remote_retry as retry_fixtures


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.profile = {'id': str(uuid4()), 'generation': str(uuid4()), 'account_fingerprint': 'a' * 64}
        self.entry = {'binding': {'alias': 'fixture-a', 'revision': 'b' * 64}, 'reconnect_required': True}
        self.health = dict(generation=self.profile['generation'], connected=True, initialized=True,
                           streamComplete=True, accountReady=True, held=False, frontendMutationBlocked=False,
                           sshBinding={'profileId': self.profile['id'], 'hostAlias': 'fixture-a',
                                       'revision': 'b' * 64, 'authState': 'ready', 'accountFingerprint': 'a' * 64})
        self.client = MagicMock()
        self.client.request.side_effect = lambda *args, **kwargs: deepcopy(self.health)
        self.factory = MagicMock(return_value=self.client)
        self.readiness = RemoteReadiness('.', client_factory=self.factory)

    def test_only_previous_active_connections_are_checked_and_no_hash_is_exposed(self):
        result = self.readiness.inspect(self.profile, [self.entry, {'binding': {'alias': 'inactive-host'}}])
        self.assertTrue(result['ready'])
        self.assertEqual(result['connections'], [{'host_alias': 'fixture-a', 'state': 'ready'}])
        self.assertNotIn('a' * 64, json.dumps(result))
        self.factory.assert_called_once_with(self.profile, 'fixture-a')
        self.client.request.assert_called_once_with('manager/maintenance/status', {}, timeout=1)

    def test_ready_transport_cannot_hide_wrong_account_host_revision_or_generation(self):
        for key, value, expected in [('accountFingerprint', 'c' * 64, 'account_mismatch'),
                                      ('profileId', str(uuid4()), 'connection_changed'),
                                      ('hostAlias', 'another-host', 'connection_changed'),
                                      ('revision', 'c' * 64, 'connection_changed')]:
            previous = self.health['sshBinding'][key]
            self.health['sshBinding'][key] = value
            self.assertEqual(self.readiness.inspect(self.profile, [self.entry])['connections'][0]['state'], expected)
            self.health['sshBinding'][key] = previous
        self.health['generation'] = str(uuid4())
        self.assertFalse(self.readiness.inspect(self.profile, [self.entry])['ready'])

    def test_missing_or_authenticating_connection_stays_pending(self):
        for changes in ({'connected': False}, {'accountReady': False}, {'held': True}, {'sshBinding': {}}):
            before = deepcopy(self.health)
            self.health.update(changes)
            self.assertFalse(self.readiness.inspect(self.profile, [self.entry])['ready'])
            self.health = before
        self.client.request.side_effect = AdminError('unavailable')
        self.assertEqual(self.readiness.inspect(self.profile, [self.entry])['connections'][0]['state'], 'connecting')

    def test_control_metadata_is_typed_and_strips_extra_credentials(self):
        auth = Auth()
        auth.account_fingerprint = 'a' * 64
        control = SshRuntimeControl(auth, self.profile['id'], self.profile['generation'], 1, lambda frame: self.fail('no RPC send'),
                                    host_alias='fixture-a', revision='b' * 64)
        self.addCleanup(control.close)
        value = control.dispatch('manager/maintenance/status', {}, 1, None)
        value['sshBinding']['accessToken'] = 'must-not-appear'
        sanitized = sanitize_result('manager/maintenance/status', {}, value)
        self.assertEqual(sanitized['sshBinding']['accountFingerprint'], 'a' * 64)
        self.assertNotIn('must-not-appear', json.dumps(sanitized))
        value['sshBinding']['accountFingerprint'] = 'invalid'
        with self.assertRaises(AdminError):
            sanitize_result('manager/maintenance/status', {}, value)


class ConnectionWaitTests(unittest.TestCase):
    def setUp(self):
        self.case = retry_fixtures.ProfileRemoteRetryTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.store, self.hooks, self.restarts = self.case.store, self.case.hooks, self.case.restarts
        self.profile_id = self.case.profile['id']
        self.account_state = 'authenticating'
        self.account_fingerprint = 'a' * 64
        self.store.mutate(lambda data: self.store.profile(self.profile_id, data).update(account_fingerprint='a' * 64))
        instances = self.case.fixture.instances
        observe = instances.observe
        def observed(profile):
            result = observe(profile)
            if result.get('runtime_state'):
                result['runtime_state']['auth_binding']['account_fingerprint'] = 'a' * 64
            return result
        instances.observe = observed
        show = instances.show
        def shown(profile_id):
            result = show(profile_id)
            self.store.mutate(lambda data: self.store.profile(profile_id, data).update(account_fingerprint='a' * 64))
            return result
        instances.show = shown
        def coverage(profile):
            bindings = json.loads(self.case.manifest.read_text())['bindings']
            binding = next(b for b in bindings if b['alias'] == 'fixture-a')
            return dict(complete=False, maintenance_complete=True, generation=profile['generation'],
                        hosts=['local', 'fixture-a', 'fixture-b'], operations=[dict(operation='native-proxy',
                            alias='fixture-a', revision=binding['revision'], generation=profile['generation'])])
        self.hooks.host_inventory = coverage
        self.reads = []
        def factory(profile, alias):
            self.reads.append(alias)
            binding = next(b for b in json.loads(self.case.manifest.read_text())['bindings'] if b['alias'] == alias)
            value = dict(generation=profile['generation'], connected=True, initialized=True, streamComplete=True,
                         accountReady=self.account_state == 'ready', held=False, frontendMutationBlocked=False,
                         sshBinding=dict(profileId=profile['id'], hostAlias=alias, revision=binding['revision'],
                                         authState=self.account_state, accountFingerprint=self.account_fingerprint))
            client = MagicMock()
            client.request.return_value = value
            return client
        self.hooks.remote_readiness = RemoteReadiness(self.case.fixture.root, client_factory=factory)

    def begin(self):
        job = self.restarts.schedule(self.profile_id)
        self.case.pending.clear()  # Advance this test's worker deterministically.
        self.assertFalse(self.restarts.step(self.profile_id, job['id']))
        self.assertEqual(self.restarts.status()[self.profile_id]['phase'], 'connecting')
        return job

    def test_account_connection_wait_completes_without_another_restart(self):
        job = self.begin()
        processes = deepcopy(self.case.fleet.running)
        calls = deepcopy(self.case.fleet.calls)
        self.hooks.guard_launch(self.profile_id)
        self.hooks.guard_launch(self.case.peer['id'])
        self.assertFalse(self.restarts.step(self.profile_id, job['id']))
        self.assertEqual(self.case.fleet.calls, calls)
        self.account_state = 'ready'
        self.assertTrue(self.restarts.step(self.profile_id, job['id']))
        self.assertEqual(self.restarts.status()[self.profile_id]['phase'], 'complete')
        self.assertEqual(self.case.fleet.running, processes)
        self.assertEqual(self.case.show_count, 1)
        self.assertEqual(set(self.reads), {'fixture-a'})

    def test_saved_conversation_is_requested_once_before_waiting_for_ssh(self):
        navigated = []
        self.hooks.navigate = lambda profile, entry: navigated.append(entry['thread_id']) or {'state': 'request_sent'}
        thread_id = str(uuid4())
        restore = self.hooks.restore_instance
        self.hooks.restore_instance = lambda entry: restore({**entry, 'thread_id': thread_id})
        job = self.begin()
        self.assertEqual(navigated, [thread_id])
        self.account_state = 'ready'
        self.assertTrue(self.restarts.step(self.profile_id, job['id']))
        self.assertEqual(navigated, [thread_id])
        self.assertEqual(self.case.show_count, 1)

    def test_wrong_account_never_completes_or_restarts_peers(self):
        job = self.begin()
        self.account_state = 'ready'
        self.account_fingerprint = 'c' * 64
        calls = deepcopy(self.case.fleet.calls)
        self.assertFalse(self.restarts.step(self.profile_id, job['id']))
        status = self.restarts.status()[self.profile_id]
        self.assertIn('계정 불일치', status['message'])
        self.assertNotIn('c' * 64, json.dumps(status))
        self.assertEqual(self.case.fleet.calls, calls)
        self.hooks.guard_launch(self.case.peer['id'])

    def test_a_new_instance_generation_is_not_mistaken_for_the_waited_connection(self):
        job = self.begin()
        self.store.mutate(lambda data: self.store.profile(self.profile_id, data).update(generation=str(uuid4())))
        calls = deepcopy(self.case.fleet.calls)
        self.assertTrue(self.restarts.step(self.profile_id, job['id']))
        self.assertEqual(self.restarts.status()[self.profile_id]['phase'], 'attention')
        self.assertEqual(self.case.fleet.calls, calls)


if __name__ == '__main__':
    unittest.main()
