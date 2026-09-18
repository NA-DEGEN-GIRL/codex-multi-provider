"""Real hook state machine with simulated windows and SSH lifecycle responses."""
from copy import deepcopy
import json
import unittest
from uuid import uuid4

import test_manager_update_hooks as fixtures
from manager_core.ssh_inventory import SshInventory
from manager_core.updates import UpdateError


class RemoteFixture:
    def __init__(self, profile):
        self.calls = []
        self.busy = False
        self.uncertain = False
        self.exited = False
        self.process = {'pid': 1234, 'process_start': '567', 'revision': 'a' * 64}
        self.binding = {'profile_id': profile['id'], 'alias': 'remote-dev', 'revision': 'a' * 64}

    def bindings(self, profile, coverage):
        return [deepcopy(self.binding)]

    def snapshot(self, profile, coverage):
        self.calls.append('inspect')
        return [{'binding': deepcopy(self.binding), 'process': None if self.exited else deepcopy(self.process),
                 'idle': not self.busy, 'exited': self.exited}]

    def stop(self, entry):
        self.calls.append('stop')
        if self.uncertain:
            raise UpdateError('remote_timeout', 'fixture timeout')
        self.exited = True
        return {'binding': entry['binding'], 'process': None, 'idle': True, 'exited': True}

    def prepare_and_start(self, profile, entries, save):
        self.calls.append('prepare_and_start')
        for entry in entries:
            entry['state'] = 'started'
            save()
        self.exited = False

    def reconcile(self, entry):
        self.calls.append('reconcile')
        if self.exited:
            entry['state'] = 'closed'

    def publish_started(self, profile, entries):
        pass


class RemoteHookTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.HookFixtures()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.hooks, self.store = self.fixture.hooks, self.fixture.store
        self.profile = self.fixture.profile
        self.remote = RemoteFixture(self.profile)
        self.hooks.remote_maintenance = self.remote
        self.hooks.host_inventory = lambda p: {'complete': True, 'generation': p['generation'],
                                                'hosts': ['local', 'remote-dev']}

    def acquire(self):
        snapshot = self.hooks.snapshot_instances(profile_ids=[self.profile['id']])
        lease = self.hooks.acquire_maintenance(snapshot, transaction_id=str(uuid4()), profile_scope=[self.profile['id']])
        return lease, snapshot[0]

    def test_remote_busy_defers_without_closing_windows_or_holding_gate(self):
        self.remote.busy = True
        snapshot = self.hooks.snapshot_instances()[0]
        self.assertFalse(snapshot['idle_verified'])
        self.assertEqual(snapshot['update_blocker'], 'runtime_not_idle')
        self.assertEqual(self.fixture.closes, [])
        self.hooks.guard_launch(self.profile['id'])

    def test_close_then_new_policy_start_and_window_restore_keeps_peer_available(self):
        peer = self.store.add_profile('peer')
        lease, snapshot = self.acquire()
        self.assertTrue(self.hooks.close_instance(snapshot))
        self.assertTrue(self.remote.exited)
        self.assertEqual(self.fixture.closes, [self.profile['id']])
        self.hooks.guard_launch(peer['id'])
        self.fixture.admin.owner = None
        self.assertTrue(self.hooks.restore_instance({'profile_id': self.profile['id']})['verified'])
        self.hooks.release_maintenance(lease)
        self.assertEqual([c for c in self.remote.calls if c != 'inspect'], ['stop', 'prepare_and_start'])
        self.hooks.guard_launch(self.profile['id'])

    def test_uncertain_remote_stop_retains_gate_and_does_not_close_or_start_window(self):
        lease, snapshot = self.acquire()
        self.remote.uncertain = True
        with self.assertRaises(UpdateError):
            self.hooks.close_instance(snapshot)
        with self.assertRaises(UpdateError):
            self.hooks.release_maintenance(lease)
        self.assertEqual(self.fixture.closes, [])
        with self.assertRaises(UpdateError):
            self.hooks.restore_instance({'profile_id': self.profile['id']})
        self.assertEqual(self.remote.calls.count('stop'), 1)
        self.assertNotIn('prepare_and_start', self.remote.calls)
        # Later exit evidence releases the gate without replaying stop.
        self.remote.exited = True
        self.hooks.release_maintenance(lease)
        self.assertEqual(self.remote.calls.count('stop'), 1)
        self.hooks.guard_launch(self.profile['id'])

    def test_closed_window_still_requires_remote_idle_and_verified_stop(self):
        self.fixture.instances.close(self.profile)
        self.remote.busy = True
        snapshot = self.hooks.snapshot_instances()[0]
        self.assertTrue(snapshot['remote_maintenance_required'])
        self.assertFalse(snapshot['idle_verified'])
        self.remote.busy = False
        lease, snapshot = self.acquire()
        self.assertTrue(self.hooks.close_instance(snapshot))
        self.assertEqual(self.fixture.closes, [])
        self.assertTrue(self.remote.exited)
        result = self.hooks.restore_instance({'profile_id': self.profile['id'], 'remote_only': True})
        self.assertTrue(result['remote_runtime_restored'])
        self.assertFalse(result['profile_reopened'])
        self.assertEqual(self.fixture.instances.show_calls, [])
        self.hooks.release_maintenance(lease)

    def test_only_restored_generation_can_reconnect_during_maintenance(self):
        inventory = SshInventory(self.fixture.root)
        inventory.prepare(self.profile['id'], self.profile['generation'])
        lease, _ = self.acquire()
        new_profile = dict(self.profile, generation=str(uuid4()))
        self.hooks._restoration.transaction_id = lease['transaction_id']
        try:
            self.hooks.authorize_restoration_generation(new_profile)
        finally:
            self.hooks._restoration.transaction_id = None
        inventory.prepare(new_profile['id'], new_profile['generation'])
        with inventory.execution(new_profile['id'], new_profile['generation'],
                                 {'operation': 'native-proxy', 'alias': 'remote-dev', 'revision': 'a' * 64}):
            pass
        with self.assertRaises(UpdateError):
            with inventory.execution(self.profile['id'], self.profile['generation'], {'operation': 'native-start'}):
                self.fail('old connection must remain fenced')
        with self.assertRaises(UpdateError):
            with inventory.execution(new_profile['id'], new_profile['generation'], {'operation': 'passthrough'}):
                self.fail('unclassified commands are not restoration')


if __name__ == '__main__':
    unittest.main()
