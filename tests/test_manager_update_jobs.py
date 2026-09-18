"""Actual update journal/worker and request handling with fake package/GUI I/O."""
from copy import deepcopy
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from control_center import ControlCenter
from manager_core.update_jobs import UpdateJobs
from manager_core.updates import UpdateError
import test_manager_updates as fixtures


class UpdateConnectionTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.UpdateFixtures()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.patched_execution()
        self.manager = self.case.manager
        self.ready = False
        self.checks = []
        self.generation = str(uuid4())
        def restore(entry):
            self.case.restored.append(deepcopy(entry))
            return dict(verified=False, code='remote_account_pending', profile_id=entry['profile_id'],
                        profile_reopened=True, runtime_initialized=True, generation=self.generation,
                        message='SSH account pending')
        def check(profile_id, transaction_id, generation):
            self.assertEqual(generation, self.generation)
            self.checks.append((profile_id, transaction_id))
            return dict(verified=self.ready, message='SSH account pending')
        self.manager.restore_instance = restore
        self.manager.check_restored_connections = check

    def apply(self):
        return self.manager.apply(self.manager.plan(self.case.instances))

    def test_slow_connection_keeps_install_success_and_polls_without_reopening(self):
        first = self.apply()
        self.assertEqual(first['status'], 'connecting')
        self.assertIn('SSH account pending', first['message'])
        self.assertEqual(first['installed_after']['version'], self.case.latest['version'])
        self.assertEqual(self.manager.status()['maintenance_state'], 'released')
        self.assertEqual(self.manager.poll_restore()['status'], 'connecting')
        self.ready = True
        final = self.manager.poll_restore()
        self.assertEqual(final['status'], 'complete')
        self.assertFalse(final['restore_waiting'])
        self.assertEqual(len(self.case.installs), 1)
        self.assertEqual(len(self.case.restored), 1)
        self.assertEqual(self.case.closed, ['alpha'])
        self.assertEqual(len(self.checks), 2)
        self.assertEqual(self.manager.apply(first['plan_id'])['code'], 'plan_not_ready')

    def test_explicit_recovery_of_waiting_connection_does_not_repeat_restoration(self):
        self.apply()
        self.assertEqual(self.manager.recover()['status'], 'connecting')
        self.ready = True
        self.assertEqual(self.manager.recover()['status'], 'complete')
        self.assertEqual(len(self.case.installs), 1)
        self.assertEqual(len(self.case.restored), 1)
        self.assertFalse(self.manager.status()['install_retried'])

    def test_changed_connection_is_reported_without_install_close_or_reopen(self):
        self.apply()
        self.manager.check_restored_connections = lambda *_: (_ for _ in ()).throw(
            UpdateError('restore_generation_changed', 'changed generation'))
        result = self.manager.poll_restore()
        self.assertEqual(result['status'], 'recovery_required')
        self.assertEqual(len(self.case.installs), 1)
        self.assertEqual(len(self.case.restored), 1)
        self.assertEqual(self.case.closed, ['alpha'])

    def test_waiting_for_one_profile_does_not_delay_opening_the_other_profile(self):
        peer = deepcopy(self.case.instances[0])
        peer.update(profile_id='beta', process_id=101)
        self.case.instances.append(peer)
        self.case.live.append(dict(process_id=101, parent_process_id=0,
                                   created_at=peer['created_at'], executable=peer['executable']))
        restore = self.manager.restore_instance
        def restore_peer(entry):
            if entry['profile_id'] == 'beta':
                self.case.restored.append(entry)
                return {'verified': True}
            return restore(entry)
        self.manager.restore_instance = restore_peer
        result = self.apply()
        self.assertEqual(result['status'], 'connecting')
        self.assertEqual(result['restored_profiles'], ['beta'])
        self.assertEqual({r['profile_id'] for r in self.case.restored}, {'alpha', 'beta'})
        self.ready = True
        self.assertEqual(self.manager.poll_restore()['status'], 'complete')
        self.assertEqual(len(self.case.restored), 2)


class UpdateWorkerTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.UpdateFixtures()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.patched_execution()
        self.manager = self.case.manager
        self.threads = []
        def spawn(target):
            thread = threading.Thread(target=target)
            self.threads.append(thread)
            thread.start()
        self.jobs = UpdateJobs(self.manager, spawn=spawn, interval=.01)
        self.addCleanup(self.jobs.shutdown)

    def join(self):
        for thread in self.threads:
            thread.join(8)
            self.assertFalse(thread.is_alive(), 'Fixture worker did not finish')

    def test_download_does_not_block_requests_or_start_twice(self):
        entered, release = threading.Event(), threading.Event()
        download = self.case.fake_download
        def slow_download(latest):
            entered.set()
            if not release.wait(8):
                raise TimeoutError('Fixture release was not signaled')
            return download(latest)
        center = ControlCenter(self.case.root)
        center.updates, center.update_jobs = self.manager, self.jobs
        self.addCleanup(center.restarts.shutdown)
        try:
            with patch.object(self.manager, '_download', slow_download):
                first = center.request(dict(id=1, command='updates.apply'))
                self.assertTrue(first['ok'])
                self.assertTrue(first['result']['worker_active'])
                self.assertTrue(entered.wait(5))
                self.assertIsInstance(self.jobs._read()['worker_created'], int)
                # This goes through the same request mutex as the update button.
                second = center.request(dict(id=2, command='catalog.list'))
                self.assertTrue(second['ok'], second)
                duplicate = UpdateJobs(self.manager).schedule()
                self.assertTrue(duplicate['worker_active'])
                self.assertEqual(len(self.threads), 1)
                self.assertFalse(self.case.closed or self.case.installs)
                self.assertTrue(self.jobs.status()['worker_active'])
                release.set()
                self.join()
        finally:
            release.set()
            self.join()
        self.assertEqual(self.jobs.status()['status'], 'complete', self.jobs.status())
        self.assertFalse(self.jobs.status()['worker_active'])
        self.assertEqual(len(self.case.installs), 1)

    def test_latest_or_newer_installed_build_is_never_scheduled_for_install(self):
        for status in ('installed_newer', 'up_to_date'):
            self.jobs.last_check = dict(status=status, message='Installed version is current.')
            result = self.jobs.schedule()
            self.assertEqual(result['status'], status)
            self.assertFalse(result['worker_active'])
        self.assertFalse(self.threads or self.case.closed or self.case.installs)

    def test_old_no_update_result_is_not_replayed_as_a_failure_at_startup(self):
        self.jobs._write(dict(phase='finished', result=dict(status='blocked',code='plan_not_ready',
            blockers=[dict(code='installed_newer',message='Already current')],message='Check again')))
        self.assertEqual(self.jobs.status()['status'], 'installed_newer')
        self.assertEqual(self.jobs.status()['message'], 'Already current')
        self.assertFalse(self.case.closed or self.case.installs)

    def test_worker_waits_automatically_without_another_user_action(self):
        waiting, ready = threading.Event(), threading.Event()
        generation = str(uuid4())
        def restore(entry):
            self.case.restored.append(entry)
            return dict(verified=False, code='remote_account_pending', profile_id=entry['profile_id'],
                        profile_reopened=True, runtime_initialized=True, generation=generation)
        def check(*_):
            waiting.set()
            return dict(verified=ready.is_set(), message='Waiting')
        self.manager.restore_instance, self.manager.check_restored_connections = restore, check
        try:
            self.jobs.schedule()
            self.assertTrue(waiting.wait(5))
            self.assertTrue(self.jobs.status()['worker_active'])
            ready.set()
            self.join()
        finally:
            ready.set()
            self.jobs.shutdown()
            self.join()
        self.assertEqual(self.jobs.status()['status'], 'complete', self.jobs.status())
        self.assertEqual(len(self.case.restored), 1)
        self.assertEqual(len(self.case.installs), 1)

    def test_reading_dead_worker_is_passive_and_explicit_retry_recovers(self):
        job = dict(id=str(uuid4()), phase='running', worker_pid=999999, worker_created='unknown')
        self.jobs._write(job)
        self.jobs.owner_alive = lambda _: False
        before = self.jobs.path.read_bytes()
        self.assertEqual(self.jobs.status()['status'], 'recovery_required')
        self.assertEqual(self.jobs.path.read_bytes(), before)
        self.assertFalse(self.case.installs or self.case.closed)
        self.jobs.schedule()
        self.join()
        self.assertEqual(self.jobs.status()['status'], 'complete')

    def test_old_supervisor_cannot_start_an_untracked_background_update(self):
        center = ControlCenter(self.case.root, supervisor_protocol=6)
        center.updates, center.update_jobs = self.manager, self.jobs
        result = center.request(dict(id=1, command='updates.apply'))
        self.assertTrue(result['ok'])
        self.assertEqual(result['result']['code'], 'supervisor_update_required')
        self.assertFalse(self.threads or self.case.closed or self.case.installs)

    def test_worker_start_failure_can_be_retried_without_a_stuck_active_label(self):
        spawn = self.jobs.spawn
        self.jobs.spawn = lambda _: (_ for _ in ()).throw(RuntimeError('fixture thread unavailable'))
        with self.assertRaises(RuntimeError):
            self.jobs.schedule()
        self.assertFalse(self.jobs.status()['worker_active'])
        self.jobs.spawn = spawn
        self.jobs.schedule()
        self.join()
        self.assertEqual(self.jobs.status()['status'], 'complete')


if __name__ == '__main__':
    unittest.main()
