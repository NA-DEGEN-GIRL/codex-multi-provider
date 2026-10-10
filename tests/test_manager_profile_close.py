"""Revision 126: close one profile's Codex, optionally removing it afterwards.

Fake launchers, observations and admissions only: no native app, window or
service is started or stopped.
"""
from contextlib import contextmanager
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from control_center import ControlCenter
from manager_core.instances import Instances
from manager_core.profile_lifecycle import ProfileLifecycle, ProfileRunning
from manager_core.profile_warmup import ProfileWarmup
from manager_core.store import Store


class HoldingInstances(Instances):
    """The real launch admission and close hold; launches and observations are faked."""

    def __init__(self, root, store):
        super().__init__(root, store, Mock(), embed_windows=True)
        self.calls = []
        self.status = {}

    def observe(self, profile):
        return dict(status=self.status.get(profile['id'], 'not_started'), thread_activity={})

    def _show(self, profile_id, *, reopen_existing=True):
        self.calls.append(profile_id)
        return dict(state='launched', profile_id=profile_id, profile=dict(status='running', window_handle=7))

    def finish_show(self, result):
        return result


class ProfileCloseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.native = self.enterContext(patch('manager_core.instances.subprocess.Popen',
            side_effect=AssertionError('test must not launch a native app')))
        self.broker = self.enterContext(patch('manager_core.rust_service.launch',
            side_effect=AssertionError('test must not launch through the service')))
        self.store = Store(self.root)
        self.profile = self.store.add_profile('closing')
        generation = str(uuid4())
        self.store.mutate(lambda data: self.store.profile(self.profile['id'], data).update(generation=generation))
        self.profile = self.store.profile(self.profile['id'])
        self.other = self.store.add_profile('other')
        self.instances = HoldingInstances(self.root, self.store)
        self.admissions = []

        @contextmanager
        def admission(profile_id):
            self.admissions.append(profile_id)
            yield
        center = ControlCenter.__new__(ControlCenter)
        center.root = self.root
        center.store = self.store
        center.instances = self.instances
        center.update_hooks = Mock()
        center.update_hooks.launch_admission = admission
        center.restarts = Mock()
        center.restarts.status.return_value = {}
        center.remote_maintenance = Mock()
        center.remote_maintenance.pending_on_open.return_value = False
        center.profile_lifecycle = ProfileLifecycle(self.store, self.instances)
        center.profile_warmup = Mock()
        center.profile_warmup.status.return_value = {'worker_active': False, 'profiles': []}
        self.center = center
        self.instances.status[self.profile['id']] = 'running'

    def begin(self, remove=False, profile=None):
        return self.center.dispatch('profile.close_begin',
                                    {'profile_id': (profile or self.profile)['id'], 'remove': remove})

    def end(self, remove=False):
        return self.center.dispatch('profile.close_end', {'profile_id': self.profile['id'], 'remove': remove})

    def test_close_then_remove_holds_every_launch_until_the_profile_is_removed(self):
        begun = self.begin(remove=True)
        self.assertTrue(begun['held'])
        self.assertEqual(begun['profile']['status'], 'running')
        self.assertEqual(begun['profile']['generation'], self.profile['generation'])
        self.assertEqual(self.admissions, [self.profile['id']])
        self.assertTrue(self.instances.launch_held(self.profile['id']))
        # Between the close and the removal nothing reopens the profile.
        self.instances.status[self.profile['id']] = 'not_started'
        for attempt in (lambda: self.instances.show(self.profile['id'], reopen_existing=False),
                        lambda: self.center.dispatch('profile.show', {'profile_id': self.profile['id']})):
            with self.assertRaises(RuntimeError) as caught:
                attempt()
            self.assertEqual(caught.exception.code, 'profile_closing')
        self.assertEqual(self.instances.calls, [])
        # Other profiles still open normally.
        self.instances.show(self.other['id'], reopen_existing=False)
        self.assertEqual(self.instances.calls, [self.other['id']])
        removed = self.end(remove=True)
        self.assertTrue(removed['removed'])
        self.assertTrue(removed['closed'])
        self.assertTrue(self.store.profile(self.profile['id'])['removed_at'])
        self.assertFalse(self.instances.launch_held(self.profile['id']))

    def test_close_without_removal_keeps_the_profile_and_it_reopens_normally(self):
        self.begin()
        self.instances.status[self.profile['id']] = 'not_started'
        result = self.end()
        self.assertTrue(result['closed'])
        self.assertFalse(result['removed'])
        self.assertNotIn('removed_at', self.store.profile(self.profile['id']))
        self.assertFalse(self.instances.launch_held(self.profile['id']))
        self.center.dispatch('profile.show', {'profile_id': self.profile['id']})
        self.assertEqual(self.instances.calls, [self.profile['id']])

    def test_removal_is_refused_with_a_reason_when_the_close_did_not_finish(self):
        self.begin(remove=True)
        # The app still runs (its quit was not confirmed): nothing is removed.
        with self.assertRaises(ProfileRunning) as caught:
            self.end(remove=True)
        self.assertEqual(caught.exception.code, 'profile_running')
        self.assertIn('제거하지 않았습니다', str(caught.exception))
        self.assertNotIn('removed_at', self.store.profile(self.profile['id']))
        # The hold never outlives the attempt.
        self.assertFalse(self.instances.launch_held(self.profile['id']))

    def test_plain_remove_of_a_running_profile_reports_a_code_the_shell_can_offer_closing_for(self):
        with self.assertRaises(ProfileRunning) as caught:
            self.center.dispatch('profile.remove', {'profile_id': self.profile['id']})
        self.assertEqual(caught.exception.code, 'profile_running')
        self.assertNotIn('removed_at', self.store.profile(self.profile['id']))

    def test_unremovable_profile_is_refused_before_anything_is_closed_or_held(self):
        self.store.shortcut_add('work', self.profile['id'], str(uuid4()), 'local', 'manager:' + self.profile['id'])
        with self.assertRaises(ValueError):
            self.begin(remove=True)
        self.assertFalse(self.instances.launch_held(self.profile['id']))
        self.assertEqual(self.admissions, [])
        # Closing alone is still possible.
        self.assertTrue(self.begin()['held'])

    def test_pending_local_settings_restart_refuses_the_close(self):
        self.center.restarts.status.return_value = {self.profile['id']: {'phase': 'waiting'}}
        with self.assertRaises(RuntimeError) as caught:
            self.begin()
        self.assertEqual(caught.exception.code, 'profile_prepare_busy')
        self.assertFalse(self.instances.launch_held(self.profile['id']))
        # An SSH-only background job is stopped by the close itself (remote stop).
        self.center.restarts.status.return_value = {
            self.profile['id']: {'phase': 'waiting', 'remote_background': True}}
        self.assertTrue(self.begin()['held'])

    def test_warmup_skips_a_profile_being_closed(self):
        self.begin()
        self.instances.status[self.profile['id']] = 'not_started'
        self.instances.status[self.other['id']] = 'not_started'
        launched, workers = [], []

        def launch(profile_id):
            launched.append(profile_id)
            return self.center._open_profile_locally(profile_id)
        warmup = ProfileWarmup(self.store, self.instances, launch, spawn=workers.append,
                               health=lambda _: dict(blocks_launch=False), max_workers=1)
        warmup.start()
        workers.pop()()
        entries = {entry['profile_id']: entry for entry in warmup.status()['profiles']}
        self.assertEqual((entries[self.profile['id']]['state'], entries[self.profile['id']]['code']),
                         ('skipped', 'closing'))
        self.assertEqual(launched, [self.other['id']])
        self.assertEqual(self.instances.calls, [self.other['id']])

    def test_launch_queued_behind_the_close_admission_sees_the_hold_before_preparing(self):
        # The real _show checks the hold first, before reading or preparing anything.
        self.instances.hold_launches(self.profile['id'])
        store = Mock()
        store.profile.side_effect = AssertionError('a held launch must not prepare')
        bare = Instances.__new__(Instances)
        bare.store, bare._close_holds, bare._launch_lock = store, dict(self.instances._close_holds), threading.Lock()
        with self.assertRaises(RuntimeError) as caught:
            Instances._show(bare, self.profile['id'], reopen_existing=False)
        self.assertEqual(caught.exception.code, 'profile_closing')

    def test_hold_expires_when_the_shell_never_finishes(self):
        self.instances.hold_launches(self.profile['id'], seconds=0)
        self.assertFalse(self.instances.launch_held(self.profile['id']))
        self.instances.status[self.profile['id']] = 'not_started'
        self.instances.show(self.profile['id'], reopen_existing=False)
        self.assertEqual(self.instances.calls, [self.profile['id']])

    def test_capability_and_waiting_open_drop_are_advertised(self):
        from control_center import ENDS_WAITING_OPENS
        self.assertIn('profile.close_begin', ENDS_WAITING_OPENS)
        source = (Path(__file__).resolve().parents[1] / 'scripts/control_center.py').read_text(encoding='utf-8')
        self.assertIn('profile_close=True', source)


if __name__ == '__main__':
    unittest.main()
