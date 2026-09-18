"""Full updater recovery with real hooks/journals and two simulated SSH hosts."""
from copy import deepcopy
import unittest
from unittest.mock import patch
from uuid import uuid4

from manager_core.updates import UpdateManager, UpdateError, _atomic_json
import test_manager_updates as packages
import test_manager_remote_restore as remotes
import test_manager_profile_remote_retry as profiles


class UpdateRemoteRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.remote_case = remotes.RemoteRestoreTests()
        self.remote_case.setUp()
        self.addCleanup(self.remote_case.doCleanups)
        self.package_case = packages.UpdateFixtures()
        self.package_case.setUp()
        self.addCleanup(self.package_case.doCleanups)
        self.hooks, self.store = self.remote_case.hooks, self.remote_case.store
        self.profile_id = self.remote_case.profile['id']
        self.fleet = self.remote_case.fleet
        callbacks = self.hooks.callbacks()
        callbacks.update(snapshot_instances=lambda: self.hooks.snapshot_instances(profile_ids=[self.profile_id]),
                         verify_compatibility=lambda _: {'compatible': True})
        self.manager = UpdateManager(self.remote_case.fixture.root, **callbacks,
            inventory=lambda: deepcopy(self.package_case.installed), processes=lambda _: [])
        self.manager._check_cache = deepcopy(self.package_case.manager._check_cache)
        self.manager._cache_time = self.package_case.manager._cache_time
        for name, value in (('_download', self.package_case.fake_download),
                            ('_validate_download', lambda *_: {'signature': {'status': 'Valid'}}),
                            ('_install', self.package_case.install)):
            mock = patch.object(self.manager, name, value)
            mock.start()
            self.addCleanup(mock.stop)

    def apply(self):
        plan = self.manager.plan(self.manager.snapshot_instances())
        self.assertEqual(plan['status'], 'ready', plan)
        try:
            return self.manager.apply(plan)
        except UpdateError as error:
            self.assertEqual(error.code, 'maintenance_release_failed')
            return self.manager.status()

    def assert_restored_once(self, result):
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(len(self.package_case.installs), 1)
        self.assertFalse(result['install_retried'])
        self.assertEqual(self.fleet.calls.count(('stop', 'fixture-a')), 1)
        self.assertEqual(self.fleet.calls.count(('stop', 'fixture-b')), 1)
        self.assertEqual(self.remote_case.fixture.instances.show_calls, [])
        self.hooks.guard_launch(self.profile_id)
        self.hooks.guard_launch(self.remote_case.peer['id'])

    def test_failed_preparation_resumes_after_global_gate_was_released(self):
        self.fleet.fail_prepare = 'fixture-b'
        first = self.apply()
        self.assertEqual(first['status'], 'failed_restore')
        self.assertEqual(self.manager.status()['maintenance_state'], 'released')
        self.fleet.fail_prepare = None
        self.assert_restored_once(self.manager.recover())
        self.assertEqual(self.fleet.calls.count(('prepare', 'fixture-a')), 1)
        self.assertEqual(self.fleet.calls.count(('start', 'fixture-a')), 1)
        self.assertEqual(self.fleet.calls.count(('start', 'fixture-b')), 1)

    def test_lost_start_reply_reuses_both_existing_daemons(self):
        self.fleet.lose_start = 'fixture-b'
        self.assertEqual(self.apply()['status'], 'failed_restore')
        running = deepcopy(self.fleet.running)
        self.assert_restored_once(self.manager.recover())
        self.assertEqual(self.fleet.running, running)
        self.assertEqual(self.fleet.calls.count(('start', 'fixture-a')), 1)
        self.assertEqual(self.fleet.calls.count(('start', 'fixture-b')), 1)

    def test_absent_start_is_explicitly_retried_after_installation_is_known(self):
        self.fleet.empty_start = 'fixture-b'
        first = self.apply()
        self.assertEqual(first['status'], 'recovery_required')
        self.assertEqual(first['install_outcome'], 'command_completed')
        running = deepcopy(self.fleet.running['fixture-a'])
        before = deepcopy(self.fleet.calls)
        self.manager.status()
        self.manager.recovery_status()
        self.assertEqual(self.fleet.calls, before)
        self.fleet.empty_start = None
        self.assert_restored_once(self.manager.recover())
        self.assertEqual(self.fleet.running['fixture-a'], running)
        self.assertEqual(self.fleet.calls.count(('start', 'fixture-a')), 1)
        self.assertEqual(self.fleet.calls.count(('start', 'fixture-b')), 2)

    def test_unknown_installer_never_triggers_remote_recovery_start(self):
        self.fleet.empty_start = 'fixture-b'
        self.apply()
        transaction = self.manager.status()
        transaction['install_outcome'] = 'unknown'
        _atomic_json(self.manager.directory / 'transaction.json', transaction)
        self.fleet.empty_start = None
        starts = [call for call in self.fleet.calls if call[0] == 'start']
        result = self.manager.recover()
        self.assertEqual(result['status'], 'recovery_required')
        self.assertEqual(result['install_outcome'], 'unknown')
        self.assertEqual([call for call in self.fleet.calls if call[0] == 'start'], starts)
        self.assertEqual(len(self.package_case.installs), 1)

    def test_incompatible_actual_package_never_starts_pending_remote(self):
        self.fleet.empty_start = 'fixture-b'
        self.apply()
        self.fleet.empty_start = None
        starts = [call for call in self.fleet.calls if call[0] == 'start']
        self.manager.verify_compatibility = lambda _: {'compatible': False}
        result = self.manager.recover()
        self.assertEqual(result['status'], 'recovery_required')
        self.assertEqual([call for call in self.fleet.calls if call[0] == 'start'], starts)

    def test_changed_policy_does_not_overwrite_started_settings_or_repeat_install(self):
        self.fleet.fail_prepare = 'fixture-b'
        self.apply()
        self.store.mutate(lambda data: self.store.profile(self.profile_id, data)['policy'].update(desired_revision=1))
        starts = [call for call in self.fleet.calls if call[0] == 'start']
        self.fleet.fail_prepare = None
        self.assertEqual(self.manager.recover()['status'], 'recovery_required')
        self.assertEqual([call for call in self.fleet.calls if call[0] == 'start'], starts)
        self.assertEqual(len(self.package_case.installs), 1)
        self.hooks.guard_launch(self.remote_case.peer['id'])

    def test_another_profile_operation_is_not_overwritten_by_recovery(self):
        self.fleet.fail_prepare = 'fixture-b'
        self.apply()
        competing = dict(transaction_id=str(uuid4()), state='held')
        self.store.mutate(lambda data: data.setdefault('profile_maintenance', {}).update({self.profile_id: competing}))
        before = deepcopy(self.fleet.calls)
        result = self.manager.recover()
        self.assertEqual(result['status'], 'recovery_required')
        self.assertEqual(self.store.read()['profile_maintenance'][self.profile_id], competing)
        self.assertEqual([c for c in self.fleet.calls[len(before):] if c[0] in ('start', 'stop')], [])


class UpdateWindowRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.case = profiles.ProfileRemoteRetryTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.hooks, self.store = self.case.hooks, self.case.store
        self.profile_id = self.case.profile['id']
        original_show = self.case.fixture.instances.show
        def show(profile_id):
            before = self.store.profile(profile_id)['generation']
            shown = original_show(profile_id)
            current = self.store.profile(profile_id)
            if before != current['generation']:
                # Production launch requires a live restoration admission gate.
                self.hooks.authorize_restoration_generation(current)
            return shown
        self.case.fixture.instances.show = show
        snapshot = self.hooks.snapshot_instances(profile_ids=[self.profile_id])
        self.lease = self.hooks.acquire_maintenance(snapshot, transaction_id=str(uuid4()))
        self.assertTrue(self.hooks.close_instance(snapshot[0]))

    def test_recovery_restores_generation_permission_after_global_release(self):
        self.case.fleet.fail_prepare = 'fixture-b'
        with self.assertRaises(OSError):
            self.hooks.restore_instance({'profile_id': self.profile_id})
        self.hooks.release_maintenance(self.lease)
        self.case.fleet.fail_prepare = None
        result = self.hooks.recover_update_instance({'profile_id': self.profile_id}, self.lease['transaction_id'])
        self.assertTrue(result['verified'], result)
        self.assertEqual(self.case.show_count, 1)
        self.assertEqual(self.case.fixture.closes, [self.profile_id])
        self.hooks.guard_launch(self.profile_id)
        self.hooks.guard_launch(self.case.peer['id'])
        gate = self.store.read()['profile_maintenance'][self.profile_id]
        self.assertEqual(gate['restoring_generations'][self.profile_id], self.store.profile(self.profile_id)['generation'])
        self.assertEqual(gate['state'], 'released')

    def test_already_open_window_and_started_daemons_survive_readiness_recovery(self):
        self.case.not_ready_on_show = True
        self.assertFalse(self.hooks.restore_instance({'profile_id': self.profile_id})['verified'])
        self.hooks.release_maintenance(self.lease)
        running = deepcopy(self.case.fleet.running)
        before = self.store.profile(self.profile_id)
        self.case.fixture.instances.initialized = True
        result = self.hooks.recover_update_instance({'profile_id': self.profile_id}, self.lease['transaction_id'])
        after = self.store.profile(self.profile_id)
        self.assertTrue(result['verified'], result)
        self.assertEqual(before['generation'], after['generation'])
        self.assertEqual(before['process_id'], after['process_id'])
        self.assertEqual(self.case.fleet.running, running)
        self.assertEqual(self.case.show_count, 1)
        self.assertEqual(self.case.fixture.closes, [self.profile_id])
        self.hooks.guard_launch(self.profile_id)


if __name__ == '__main__':
    unittest.main()
