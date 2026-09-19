import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.ssh_connection_wait import wait_for_settings
from manager_core.ssh_shim import ShimError


class ConnectionWaitTests(unittest.TestCase):
    def setUp(self):
        self.manifest = dict(profile_id='profile', generation='current')
        self.data = {'profiles': [{'id': 'profile', 'generation': 'current'}],
                     'ssh_maintenance': {'profile': {'state': 'held', 'generation': 'current'}}}
        self.store = Mock()
        self.store.read.side_effect = lambda: copy.deepcopy(self.data)
        self.store.profile.side_effect = lambda pid, data: data['profiles'][0]
        self.elapsed = 0
        self.sleeps = 0

    def sleep(self, duration):
        self.elapsed += duration
        self.sleeps += 1

    def wait(self, **kwargs):
        return wait_for_settings(['dev-host', 'codex --version'], self.manifest,
            store=self.store, clock=lambda: self.elapsed, sleep=kwargs.pop('sleep', self.sleep), **kwargs)

    def test_pending_connection_waits_without_starting_remote_work(self):
        def release(duration):
            self.sleep(duration)
            if self.sleeps == 3:
                self.data['ssh_maintenance']['profile']['state'] = 'released'
        self.wait(sleep=release)
        self.assertEqual(self.sleeps, 3)

    def test_failed_preparation_is_immediate_and_does_not_wait_again(self):
        self.data['ssh_maintenance']['profile']['state'] = 'attention'
        with self.assertRaises(ShimError) as caught:
            self.wait()
        self.assertEqual(caught.exception.code, 'ssh_settings_pending')
        self.assertEqual(self.sleeps, 0)

    def test_generation_change_cancels_waiting_old_connection(self):
        def replace(duration):
            self.sleep(duration)
            self.data['profiles'][0]['generation'] = 'replacement'
        with self.assertRaises(ShimError) as caught:
            self.wait(sleep=replace)
        self.assertEqual(caught.exception.code, 'ssh_generation_changed')

    def test_removed_profile_cancels_connection(self):
        self.data['profiles'][0]['removed_at'] = 'removed'
        with self.assertRaises(ShimError):
            self.wait()

    def test_wait_has_bounded_deadline(self):
        with self.assertRaises(ShimError) as caught:
            self.wait(timeout=.3)
        self.assertEqual(caught.exception.code, 'ssh_settings_pending')
        self.assertAlmostEqual(self.elapsed, .3)

    def test_healthy_profiles_and_configuration_queries_have_no_wait(self):
        self.data['ssh_maintenance'].clear()
        self.wait()
        self.assertEqual(self.sleeps, 0)
        self.store.reset_mock()
        wait_for_settings(['-G', 'dev-host'], self.manifest, store=self.store)
        self.store.read.assert_not_called()


if __name__ == '__main__':
    unittest.main()
