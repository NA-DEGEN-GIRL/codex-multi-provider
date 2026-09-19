"""Launch admission contention uses real fixture file locks, never apps or SSH."""
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.store import Store
from manager_core.update_hooks import UpdateHooks
from manager_core.updates import UpdateError


class RecordingInstances:
    def __init__(self, store):
        self.store = store
        self.hooks = None
        self.show_calls = []

    def paths(self, profile):
        expected = self.store.directory / 'profiles' / profile['id']
        assert Path(profile['home']) == expected / 'codex'
        assert Path(profile['ui_home']) == expected / 'ui'

    def show(self, profile_id, *, reopen_existing=True):
        with self.hooks.launch_admission(profile_id):
            self.show_calls.append((profile_id, reopen_existing))
            return dict(state='fixture_only', profile=self.store.profile(profile_id))


class FakeClock:
    def __init__(self):
        self.elapsed = 0.0
        self.sleeps = []

    def __call__(self):
        return self.elapsed

    def sleep(self, duration):
        self.sleeps.append(duration)
        self.elapsed += duration


class LaunchAdmissionWaitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root)
        self.profile = self.store.add_profile('waiting fixture')
        self.holder_profile = self.store.add_profile('holding fixture')
        self.first = self.hooks()
        self.second = self.hooks()
        self.enterContext(patch('subprocess.Popen',
            side_effect=AssertionError('fixture must not launch an app or SSH')))
        self.enterContext(patch('manager_core.rust_service.launch',
            side_effect=AssertionError('fixture must not launch through the service')))

    def hooks(self, **kwargs):
        # Independent Store and hooks objects share only the on-disk workspace.
        store = Store(self.root)
        instances = RecordingInstances(store)
        hooks = UpdateHooks(self.root, store, instances, **kwargs)
        instances.hooks = hooks
        return hooks

    def action(self, kind, hooks=None):
        hooks = hooks or self.second
        if kind == 'launch':
            return lambda: hooks.instances.show(self.profile['id'])
        if kind == 'local_ssh':
            return lambda: hooks.open_local_for_remote_reconcile(self.profile['id'])
        if kind == 'maintenance':
            return lambda: hooks._begin_global(str(uuid4()))
        raise AssertionError(kind)

    def run_behind_holder(self, action, while_waiting=None):
        waiting = threading.Event()
        results, errors = [], []

        def sleep(duration):
            waiting.set()
            time.sleep(duration)

        self.second.sleep = sleep

        def run():
            try:
                results.append(action())
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=run)
        try:
            with self.first.launch_admission(self.holder_profile['id']):
                worker.start()
                self.assertTrue(waiting.wait(2), 'contending request did not wait for admission')
                self.assertTrue(worker.is_alive())
                self.assertEqual(results, [])
                self.assertEqual(errors, [])
                self.assertEqual(self.second.instances.show_calls, [])
                if while_waiting is not None:
                    while_waiting()
        finally:
            if worker.ident is not None:
                worker.join(3)
        self.assertFalse(worker.is_alive(), 'admission did not proceed after holder drained')
        return results, errors

    def test_independent_launches_wait_and_guard_reads_state_after_acquiring(self):
        observed = []
        original = self.second.guard_launch

        def guard(profile_id):
            observed.append(self.second.store.read().get('fixture_epoch'))
            return original(profile_id)

        self.second.guard_launch = guard

        def update_while_waiting():
            self.assertEqual(observed, [])
            self.store.mutate(lambda data: data.update(fixture_epoch='after first launch'))

        results, errors = self.run_behind_holder(self.action('launch'), update_while_waiting)
        self.assertEqual(errors, [])
        self.assertEqual(results[0]['state'], 'fixture_only')
        self.assertEqual(observed, ['after first launch'])
        self.assertEqual(self.second.instances.show_calls, [(self.profile['id'], True)])

    def test_local_ssh_open_waits_and_retains_its_scoped_gate(self):
        results, errors = self.run_behind_holder(self.action('local_ssh'))
        self.assertEqual(errors, [])
        shown, transaction_id = results[0]
        self.assertEqual(shown['state'], 'fixture_only')
        self.assertEqual(self.second.instances.show_calls, [(self.profile['id'], False)])
        gate = self.store.read()['ssh_maintenance'][self.profile['id']]
        self.assertEqual(gate['transaction_id'], transaction_id)
        self.assertEqual(gate['state'], 'held')
        # Admission does not relax the SSH fence for a full update.
        with self.assertRaises(UpdateError) as caught:
            self.first._begin_global(str(uuid4()))
        self.assertEqual(caught.exception.code, 'profile_maintenance')

    def test_global_maintenance_waits_until_launch_drains_then_blocks_new_launches(self):
        def unchanged_while_waiting():
            self.assertIsNone(self.store.read().get('update_maintenance'))

        results, errors = self.run_behind_holder(self.action('maintenance'), unchanged_while_waiting)
        self.assertEqual(errors, [])
        self.assertEqual(results, [None])
        self.assertEqual(self.store.read()['update_maintenance']['state'], 'held')
        with self.assertRaises(UpdateError) as caught:
            self.first.instances.show(self.profile['id'])
        self.assertEqual(caught.exception.code, 'update_maintenance')
        self.assertEqual(self.first.instances.show_calls, [])

    def test_maintenance_started_during_wait_is_rechecked_by_all_admission_paths(self):
        for kind in ['launch', 'local_ssh', 'maintenance']:
            with self.subTest(kind=kind):
                self.store.mutate(lambda data: data.pop('update_maintenance', None))
                gate = dict(state='held', transaction_id=str(uuid4()))

                def hold_maintenance():
                    self.store.mutate(lambda data: data.update(update_maintenance=gate))

                results, errors = self.run_behind_holder(self.action(kind), hold_maintenance)
                self.assertEqual(results, [])
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], UpdateError)
                self.assertEqual(errors[0].code,
                    'update_in_progress' if kind == 'maintenance' else 'update_maintenance')
                self.assertEqual(self.store.read()['update_maintenance'], gate)
                self.assertEqual(self.second.instances.show_calls, [])

    def test_timeout_is_bounded_and_cannot_launch_or_create_maintenance(self):
        for kind in ['launch', 'local_ssh', 'maintenance']:
            with self.subTest(kind=kind):
                clock = FakeClock()
                waiter = self.hooks(clock=clock, sleep=clock.sleep)
                before = self.store.read()
                with self.first.launch_admission(self.holder_profile['id']):
                    with self.assertRaises(UpdateError) as caught:
                        self.action(kind, waiter)()
                self.assertEqual(caught.exception.code, 'profile_launch_busy')
                self.assertIn('다른 프로필을 여는 작업', str(caught.exception))
                self.assertAlmostEqual(clock.elapsed, 10.0)
                self.assertTrue(clock.sleeps)
                self.assertTrue(all(0 < delay <= 0.05 for delay in clock.sleeps))
                self.assertLessEqual(len(clock.sleeps), 201)
                self.assertEqual(waiter.instances.show_calls, [])
                self.assertEqual(self.store.read(), before)
                self.assertEqual(getattr(waiter._admission, 'depth', 0), 0)

    def test_non_contention_errors_propagate_without_retry_at_every_call_site(self):
        for kind in ['launch', 'local_ssh', 'maintenance']:
            for error in [UpdateError('invalid_update_state', 'fixture error'),
                          PermissionError('fixture access denied')]:
                with self.subTest(kind=kind, error=type(error).__name__):
                    clock = FakeClock()
                    waiter = self.hooks(clock=clock, sleep=clock.sleep)
                    with patch('manager_core.update_hooks._lock_file', side_effect=error) as acquire:
                        with self.assertRaises(type(error)) as caught:
                            self.action(kind, waiter)()
                    self.assertIs(caught.exception, error)
                    acquire.assert_called_once_with(waiter.directory / 'launch-admission.lock')
                    self.assertEqual(clock.sleeps, [])
                    self.assertEqual(waiter.instances.show_calls, [])

    def test_actual_maintenance_conflict_is_not_retried_as_lock_contention(self):
        clock = FakeClock()
        waiter = self.hooks(clock=clock, sleep=clock.sleep)
        gate = dict(state='held', transaction_id=str(uuid4()))
        self.store.mutate(lambda data: data.update(update_maintenance=gate))
        with self.assertRaises(UpdateError) as caught:
            waiter._begin_global(str(uuid4()))
        self.assertEqual(caught.exception.code, 'update_in_progress')
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(self.store.read()['update_maintenance'], gate)


if __name__ == '__main__':
    unittest.main()
