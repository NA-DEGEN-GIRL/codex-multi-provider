"""Profile preparation contention uses fixture locks, never live apps or SSH."""
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import test_manager_update_hooks as fixtures
from manager_core.profile_restart import ProfileRestarts
from manager_core.store import Store
from manager_core.updates import UpdateError, _lock_file, _unlock_file


class FakeClock:
    def __init__(self):
        self.elapsed = 0.0
        self.waits = []

    def __call__(self):
        return self.elapsed

    def wait(self, duration):
        self.waits.append(duration)
        self.elapsed += duration
        return False


class ProfileClaimWaitTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.HookFixtures()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.store, self.hooks = fixture.store, fixture.hooks
        self.instances, self.profile = fixture.instances, fixture.profile
        self.instances.close(self.profile)
        self.peer = self.store.add_profile('unrelated fixture')
        self.pending = []
        self.restarts = self.make_restarts()
        original_show = self.instances.show

        def show(profile_id, *, reopen_existing=True, wait_for_window=True):
            self.assertFalse(wait_for_window)
            result = original_show(profile_id)
            return {**result, 'state': 'launched'}

        self.instances.show = show
        self.enterContext(patch('subprocess.Popen',
            side_effect=AssertionError('fixture must not launch an app or SSH')))
        self.enterContext(patch('manager_core.rust_service.launch',
            side_effect=AssertionError('fixture must not launch through the service')))
        self.enterContext(patch('manager_core.profile_restart.process_identity',
                                return_value={'process_created': 1}))

    def make_restarts(self):
        return ProfileRestarts(Store(self.store.root), self.instances, self.hooks,
                               spawn=self.pending.append, owner_alive=lambda _: True)

    def path(self, profile=None):
        return self.store.directory / 'restarts' / ((profile or self.profile)['id'] + '.lock')

    def action(self, kind, restarts=None):
        return lambda: getattr(restarts or self.restarts, kind)(self.profile['id'])

    def start(self, action):
        results, errors, done = [], [], threading.Event()

        def run():
            try:
                results.append(action())
            except Exception as error:
                errors.append(error)
            finally:
                done.set()

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        return worker, results, errors, done

    def watch_wait(self, restarts, waiting):
        original = restarts.stopping.wait

        def wait(duration):
            waiting.set()
            return original(duration)

        return patch.object(restarts.stopping, 'wait', side_effect=wait)

    def pause_window(self, entered, release):
        def finish(result):
            entered.set()
            if not release.wait(4):
                raise TimeoutError('fixture did not release its window wait')
            return result

        return patch.object(self.instances, 'finish_show', side_effect=finish)

    def assert_duplicate_joins(self, second):
        entered, release, waiting = (threading.Event() for _ in range(3))
        workers = []
        with self.pause_window(entered, release), self.watch_wait(second, waiting):
            try:
                first = self.start(self.action('open_local'))
                workers.append(first[0])
                self.assertTrue(entered.wait(2))
                self.assertEqual(self.instances.observe(self.store.profile(self.profile['id']))['status'], 'running')
                self.assertNotIn(self.profile['id'], self.store.read().get('profile_restarts', {}))
                duplicate = self.start(self.action('open_local', second))
                workers.append(duplicate[0])
                self.assertTrue(waiting.wait(2))
                self.assertFalse(duplicate[3].is_set())
                self.assertEqual(self.instances.show_calls, [self.profile['id']])
            finally:
                release.set()
                for worker in workers:
                    worker.join(2)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(first[2] + duplicate[2], [])
        self.assertEqual(first[1][0]['state'], 'launched')
        self.assertEqual(duplicate[1][0]['state'], 'existing')
        job_id = first[1][0]['restart']['id']
        self.assertEqual(duplicate[1][0]['restart']['id'], job_id)
        self.assertEqual(self.store.read()['profile_restarts'][self.profile['id']]['id'], job_id)
        self.assertEqual(self.instances.show_calls, [self.profile['id']])
        self.assertEqual(len(self.pending), 1)

    def test_manual_open_joins_warmup_during_window_wait(self):
        self.assert_duplicate_joins(self.restarts)

    def test_separate_service_joins_job_published_after_its_wait(self):
        self.assert_duplicate_joins(self.make_restarts())

    def test_schedule_racing_open_does_not_deadlock_or_block_another_profile(self):
        entered, release, waiting = (threading.Event() for _ in range(3))
        workers = []
        with self.pause_window(entered, release), self.watch_wait(self.restarts, waiting):
            try:
                opener = self.start(self.action('open_local'))
                workers.append(opener[0])
                self.assertTrue(entered.wait(2))
                scheduled = self.start(self.action('schedule'))
                workers.append(scheduled[0])
                self.assertTrue(waiting.wait(2))
                self.assertFalse(scheduled[3].is_set())
                peer = self.start(lambda: self.restarts.schedule(self.peer['id']))
                workers.append(peer[0])
                self.assertTrue(peer[3].wait(2), 'same-profile waiter held the shared worker lock')
                self.assertEqual(peer[2], [])
                self.assertEqual(peer[1][0]['profile_id'], self.peer['id'])
            finally:
                release.set()
                for worker in workers:
                    worker.join(2)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(opener[2] + scheduled[2], [])
        self.assertEqual(opener[1][0]['restart']['id'], scheduled[1][0]['id'])
        self.assertEqual(self.instances.show_calls, [self.profile['id']])
        self.assertEqual(len(self.pending), 2)

    def test_timeout_is_scoped_bounded_and_preserves_state(self):
        for kind in ('open_local', 'schedule'):
            with self.subTest(kind=kind):
                clock = FakeClock()
                before = self.store.read()
                held = _lock_file(self.path())
                try:
                    with patch('manager_core.profile_restart.monotonic', clock), \
                         patch.object(self.restarts.stopping, 'wait', side_effect=clock.wait):
                        with self.assertRaises(UpdateError) as caught:
                            self.action(kind)()
                finally:
                    _unlock_file(held)
                self.assertEqual(caught.exception.code, 'profile_prepare_busy')
                self.assertIn('이 프로필의 창과 연결', str(caught.exception))
                self.assertAlmostEqual(clock.elapsed, 10.0)
                self.assertTrue(clock.waits)
                self.assertTrue(all(0 < delay <= 0.05 for delay in clock.waits))
                self.assertLessEqual(len(clock.waits), 201)
                self.assertEqual(self.store.read(), before)
                self.assertEqual(self.instances.show_calls, [])
                self.assertEqual(self.pending, [])
                self.assertEqual(self.restarts.workers, set())

    def test_shutdown_cancels_wait_while_the_other_claim_is_still_held(self):
        for kind in ('open_local', 'schedule'):
            with self.subTest(kind=kind):
                restarts = self.make_restarts()
                waiting = threading.Event()
                before = self.store.read()
                held = _lock_file(self.path())
                try:
                    with self.watch_wait(restarts, waiting):
                        worker, results, errors, done = self.start(self.action(kind, restarts))
                        try:
                            self.assertTrue(waiting.wait(2))
                            restarts.shutdown()
                            self.assertTrue(done.wait(1))
                        finally:
                            restarts.shutdown()
                            worker.join(2)
                finally:
                    _unlock_file(held)
                self.assertFalse(worker.is_alive())
                self.assertEqual(results, [])
                self.assertEqual(len(errors), 1)
                self.assertEqual(errors[0].code, 'service_stopped')
                self.assertEqual(self.store.read(), before)
                self.assertEqual(self.pending, [])

    def test_global_update_started_during_wait_is_rechecked_after_claim(self):
        waiting = threading.Event()
        held = _lock_file(self.path())
        gate = dict(state='held', transaction_id=str(uuid4()))
        with self.watch_wait(self.restarts, waiting):
            worker, results, errors, done = self.start(self.action('open_local'))
            try:
                self.assertTrue(waiting.wait(2))
                self.store.mutate(lambda data: data.update(update_maintenance=gate))
            finally:
                _unlock_file(held)
                worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [])
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].code, 'update_maintenance')
        state = self.store.read()
        self.assertEqual(state['update_maintenance'], gate)
        self.assertNotIn(self.profile['id'], state.get('profile_restarts', {}))
        self.assertNotIn(self.profile['id'], state.get('ssh_maintenance', {}))
        self.assertEqual(self.instances.show_calls, [])
        self.assertEqual(self.pending, [])

    def test_update_errors_inside_the_claim_are_not_retried_or_relabelled(self):
        for code in ('update_in_progress', 'update_maintenance'):
            with self.subTest(code=code):
                error = UpdateError(code, 'fixture actual update conflict')
                with patch.object(self.hooks, 'open_local_for_remote_reconcile', side_effect=error) as opened, \
                     patch.object(self.restarts.stopping, 'wait') as waited:
                    with self.assertRaises(UpdateError) as caught:
                        self.action('open_local')()
                self.assertIs(caught.exception, error)
                opened.assert_called_once_with(self.profile['id'])
                waited.assert_not_called()
                held = _lock_file(self.path())
                _unlock_file(held)
        self.assertEqual(self.pending, [])

    def test_non_contention_lock_errors_propagate_without_waiting(self):
        for kind in ('open_local', 'schedule'):
            for error in (UpdateError('invalid_update_state', 'fixture invalid state'),
                          PermissionError('fixture access denied')):
                with self.subTest(kind=kind, error=type(error).__name__):
                    with patch('manager_core.profile_restart._lock_file', side_effect=error) as acquire, \
                         patch.object(self.restarts.stopping, 'wait') as waited:
                        with self.assertRaises(type(error)) as caught:
                            self.action(kind)()
                    self.assertIs(caught.exception, error)
                    acquire.assert_called_once_with(self.path())
                    waited.assert_not_called()
        self.assertEqual(self.pending, [])


if __name__ == '__main__':
    unittest.main()
