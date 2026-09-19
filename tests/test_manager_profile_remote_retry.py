"""The profile.restart scheduling/worker path with real hooks and simulated hosts."""
from copy import deepcopy
import json
import unittest
from uuid import uuid4

import test_manager_update_hooks as fixtures
from test_manager_remote_restore import Fleet
from manager_core.profile_restart import ProfileRestarts
from manager_core.store import atomic_json
from manager_core.updates import UpdateError
from control_center import ControlCenter


class ProfileRemoteRetryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.HookFixtures()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.hooks, self.store, self.profile = self.fixture.hooks, self.fixture.store, self.fixture.profile
        self.fleet = Fleet(self.fixture.root, self.store, self.profile)
        self.hooks.remote_maintenance = self.fleet
        self.hooks.host_inventory = lambda p: dict(complete=True, generation=p['generation'],
                                                   hosts=['local', 'fixture-a', 'fixture-b'])
        self.manifest = self.store.directory / 'profiles' / self.profile['id'] / 'ssh-bindings.json'
        atomic_json(self.manifest, dict(schema=1, profile_id=self.profile['id'], generation=self.profile['generation'],
                                       bindings=list(self.fleet.bindings_by_alias.values())))
        self.peer = self.store.add_profile('unaffected peer')
        self.pending = []
        self.restarts = ProfileRestarts(self.store, self.fixture.instances, self.hooks, spawn=self.pending.append)
        self.not_ready_on_show = False
        self.show_count = 0
        original_show = self.fixture.instances.show

        def show(profile_id):
            before = self.store.profile(profile_id)['generation']
            value = original_show(profile_id)
            current = self.store.profile(profile_id)
            if before != current['generation']:
                self.show_count += 1
                self.fixture.admin.owner = None
                self.fixture.instances.initialized = not self.not_ready_on_show
                self.store.mutate(lambda data: self.store.profile(profile_id, data)['policy'].update(
                    launched_revision=current['policy']['desired_revision']))
                # Production Instances.environment creates a new generation's
                # SSH manifest. This window double does the corresponding copy.
                manifest = json.loads(self.manifest.read_text())
                manifest['generation'] = current['generation']
                atomic_json(self.manifest, manifest)
            return value
        self.fixture.instances.show = show

    def apply(self):
        before = deepcopy(self.fleet.calls)
        response = self.restarts.schedule(self.profile['id'])
        self.assertEqual(self.fleet.calls, before, 'UI scheduling must not block on remote requests')
        self.assertEqual(len(self.pending), 1)
        self.pending.pop()()
        return self.restarts.status()[self.profile['id']]

    def assert_completed_without_second_close(self, job):
        self.assertEqual(job['phase'], 'complete', job)
        self.assertEqual(self.fixture.closes, [self.profile['id']])
        self.assertEqual(self.fleet.calls.count(('stop', 'fixture-a')), 1)
        self.assertEqual(self.fleet.calls.count(('stop', 'fixture-b')), 1)
        self.hooks.guard_launch(self.profile['id'])
        self.hooks.guard_launch(self.peer['id'])

    def test_button_retry_reuses_both_daemons_after_lost_reply(self):
        self.fleet.lose_start = 'fixture-b'
        first = self.apply()
        self.assertEqual(first['phase'], 'attention', first)
        running = deepcopy(self.fleet.running)
        job = self.apply()
        self.assert_completed_without_second_close(job)
        self.assertEqual(self.fleet.running, running)
        self.assertEqual(self.show_count, 1)
        self.assertEqual(self.fleet.calls.count(('start', 'fixture-a')), 1)
        self.assertEqual(self.fleet.calls.count(('start', 'fixture-b')), 1)

    def test_first_select_after_update_reconciles_closed_local_older_ssh_then_opens(self):
        self.fixture.instances.close(self.profile)
        self.fixture.closes.clear()
        for process in self.fleet.running.values():
            process['revision'] = '0' * 64
        def update(data):
            self.store.profile(self.profile['id'], data)['policy']['desired_revision'] = 1
            data['ssh_inventory'] = {self.profile['id']: {'hosts': ['fixture-a', 'fixture-b']}}
        self.store.mutate(update)
        center = ControlCenter.__new__(ControlCenter)
        center.store, center.instances = self.store, self.fixture.instances
        center.restarts, center.remote_maintenance = self.restarts, self.fleet
        result = center.dispatch('profile.show', {'profile_id': self.profile['id']})
        self.assertEqual(result['state'], 'updating')
        self.assertFalse(self.fleet.calls, 'Selection must not perform synchronous SSH work')
        self.pending.pop()()
        job = self.restarts.status()[self.profile['id']]
        self.assertEqual(job['phase'], 'complete', job)
        self.assertEqual(self.show_count, 1)
        self.assertEqual(self.fleet.calls.count(('stop', 'fixture-a')), 1)
        self.assertEqual(self.fleet.calls.count(('stop', 'fixture-b')), 1)
        self.assertEqual({p['revision'] for p in self.fleet.running.values()}, {'c' * 64})
        self.hooks.guard_launch(self.profile['id'])

    def test_explicit_retry_recovers_an_absent_start_without_touching_started_peer(self):
        self.fleet.empty_start = 'fixture-b'
        first = self.apply()
        self.assertEqual(first['phase'], 'attention', first)
        running = deepcopy(self.fleet.running['fixture-a'])
        self.assertEqual(self.fleet.calls.count(('start', 'fixture-b')), 1)
        self.restarts.status()
        self.hooks.guard_launch(self.peer['id'])
        self.assertEqual(self.fleet.calls.count(('start', 'fixture-b')), 1)
        self.fleet.empty_start = None
        self.assert_completed_without_second_close(self.apply())
        self.assertEqual(self.fleet.running['fixture-a'], running)
        self.assertEqual(self.fleet.calls.count(('start', 'fixture-a')), 1)
        self.assertEqual(self.fleet.calls.count(('start', 'fixture-b')), 2)

    def test_preparation_retry_continues_without_closing_the_profile_again(self):
        self.fleet.fail_prepare = 'fixture-b'
        self.assertEqual(self.apply()['phase'], 'attention')
        self.assertFalse(any(call[0] == 'start' for call in self.fleet.calls))
        self.fleet.fail_prepare = None
        self.assert_completed_without_second_close(self.apply())
        self.assertEqual(self.fleet.calls.count(('prepare', 'fixture-a')), 1)

    def test_retry_keeps_an_already_open_window_and_servers_when_readiness_recovers(self):
        self.not_ready_on_show = True
        self.assertEqual(self.apply()['phase'], 'attention')
        before = deepcopy(self.fixture.instances.running)
        servers = deepcopy(self.fleet.running)
        self.fixture.instances.initialized = True
        self.assert_completed_without_second_close(self.apply())
        self.assertEqual(self.fixture.instances.running, before)
        self.assertEqual(self.fleet.running, servers)
        self.assertEqual(self.show_count, 1)

    def test_other_maintenance_cannot_be_replaced_by_an_old_retry(self):
        self.fleet.lose_start = 'fixture-b'
        self.assertEqual(self.apply()['phase'], 'attention')
        transaction = str(uuid4())
        self.hooks._begin_global(transaction, profile_scope=[self.profile['id']])
        before = deepcopy(self.fleet.calls)
        self.assertEqual(self.apply()['phase'], 'attention')
        self.assertEqual(self.fleet.calls, before)
        self.assertEqual(self.store.read()['profile_maintenance'][self.profile['id']]['transaction_id'], transaction)
        self.hooks.guard_launch(self.peer['id'])

    def test_changed_settings_take_a_fresh_idle_snapshot_instead_of_resuming_old_policy(self):
        self.fleet.lose_start = 'fixture-b'
        self.assertEqual(self.apply()['phase'], 'attention')
        self.store.mutate(lambda data: self.store.profile(self.profile['id'], data)['policy'].update(desired_revision=1))
        job = self.apply()
        self.assertEqual(job['phase'], 'complete', job)
        self.assertEqual({value['revision'] for value in self.fleet.running.values()}, {'c' * 64})
        self.assertEqual(self.fleet.calls.count(('stop', 'fixture-a')), 2)
        self.hooks.guard_launch(self.peer['id'])

    def test_account_readiness_failure_does_not_claim_retry_completed(self):
        self.not_ready_on_show = True
        self.assertEqual(self.apply()['phase'], 'attention')
        self.fixture.instances.initialized = True
        self.fixture.instances.auth_ready = False
        before = deepcopy(self.fleet.running)
        self.assertEqual(self.apply()['phase'], 'attention')
        self.assertEqual(self.fleet.running, before)
        self.assertEqual(self.show_count, 1)


if __name__ == '__main__':
    unittest.main()
