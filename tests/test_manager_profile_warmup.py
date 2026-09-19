"""Warmup scheduling uses fake launches and temporary stores, never native UI."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.profile_warmup import ProfileWarmup
from manager_core.store import Store


class ProfileWarmupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name))
        self.profiles = [self.store.add_profile(alias) for alias in ('first', 'second', 'third')]
        self.workers, self.launched, self.observed = [], [], []
        self.running, self.unhealthy = {}, set()
        self.warmup = ProfileWarmup(self.store, self, self.launch, spawn=self.workers.append,
                                   health=lambda p: dict(blocks_launch=p['id'] in self.unhealthy),
                                   max_workers=1)

    def observe(self, profile):
        self.observed.append(profile['id'])
        return self.running.get(profile['id'], dict(status='not_started'))

    def launch(self, profile_id):
        self.launched.append(profile_id)
        return dict(state='launched', profile=dict(status='running', window_handle=12))

    def change(self, profile, **changes):
        self.store.mutate(lambda data: self.store.profile(profile['id'], data).update(changes))

    def run_worker(self):
        self.warmup.start()
        self.workers.pop()()
        return self.warmup.status()

    def test_start_is_nonblocking_singleflight_and_completed_pass_is_not_repeated(self):
        first = self.warmup.start()
        self.assertEqual(first, self.warmup.start())
        self.assertEqual(len(self.workers), 1)
        self.assertEqual((self.launched, self.observed), ([], []))
        self.workers.pop()()
        complete = self.warmup.status()
        self.assertEqual(complete['state'], 'complete')
        self.assertEqual(complete['counts'], dict(ready=3, started=0, pending=0,
                                                 skipped=0, attention=0, cancelled=0))
        self.assertEqual(self.warmup.start(), complete)
        self.assertEqual(self.workers, [])
        self.assertEqual(self.launched, [p['id'] for p in self.profiles])
        complete['profiles'].clear()
        self.assertEqual(len(self.warmup.status()['profiles']), 3)

    def test_default_pool_bounds_concurrency_prioritizes_and_cancels_queued_profiles(self):
        self.profiles += [self.store.add_profile('parallel') for _ in range(4)]
        entered = {p['id']: threading.Event() for p in self.profiles}
        release = {p['id']: threading.Event() for p in self.profiles}
        workers, active, peak = [], set(), [0]
        gate = threading.Lock()
        def launch(profile_id):
            with gate:
                active.add(profile_id)
                peak[0] = max(peak[0], len(active))
            entered[profile_id].set()
            try:
                if not release[profile_id].wait(5):
                    raise TimeoutError('fixture launch was not released')
                return self.launch(profile_id)
            finally:
                with gate:
                    active.remove(profile_id)
        def spawn(fn):
            worker = threading.Thread(target=fn)
            workers.append(worker)
            worker.start()
        warmup = ProfileWarmup(self.store, self, launch, spawn=spawn,
                               health=lambda _: dict(blocks_launch=False))
        try:
            warmup.start()
            for profile in self.profiles[:4]:
                self.assertTrue(entered[profile['id']].wait(2))
            self.assertEqual(peak[0], 4)
            self.assertEqual(warmup.status()['counts']['pending'], 7)
            chosen = self.profiles[-1]['id']
            self.assertTrue(warmup.prioritize(chosen))
            release[self.profiles[0]['id']].set()
            self.assertTrue(entered[chosen].wait(2))
            self.assertFalse(entered[self.profiles[4]['id']].is_set())
            warmup.shutdown()
            self.assertTrue(warmup.status()['worker_active'])
            self.assertEqual(warmup.status()['counts']['cancelled'], 2)
        finally:
            warmup.shutdown()
            for event in release.values():
                event.set()
            for worker in workers:
                worker.join(3)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(peak[0], 4)
        self.assertEqual(len(self.launched), len(set(self.launched)))
        self.assertEqual(warmup.status()['state'], 'stopped')
        self.assertFalse(warmup.status()['worker_active'])

    def test_running_window_is_never_launched_or_focused(self):
        self.running[self.profiles[0]['id']] = dict(status='running', window_handle=99)
        self.running[self.profiles[1]['id']] = dict(status='running', window_handle=None)
        result = self.run_worker()
        self.assertEqual(self.launched, [self.profiles[2]['id']])
        self.assertEqual([p['state'] for p in result['profiles']], ['ready', 'started', 'ready'])

    def test_removed_viewer_login_pending_and_missing_accounts_are_skipped(self):
        cases = [dict(removed_at='removed'), dict(view_only=True), dict(native_login_pending=True),
                 dict(runtime_channel='packaged'), dict(account_missing=True, auth_mode='source')]
        for changes in cases:
            profile = self.store.add_profile('skip')
            self.change(profile, **changes)
        self.unhealthy.add(self.profiles[1]['id'])
        result = self.run_worker()
        self.assertEqual(self.launched, [self.profiles[0]['id'], self.profiles[2]['id']])
        self.assertEqual(result['counts']['skipped'], 6)
        self.assertEqual([p['code'] for p in result['profiles'] if p['state'] == 'skipped'],
                         ['login_unhealthy', 'removed', 'view_only', 'login_pending',
                          'login_pending', 'account_missing'])

    def test_profile_is_rechecked_after_preceding_launch(self):
        def launch(profile_id):
            self.change(self.profiles[1], removed_at='removed-during-warmup')
            self.unhealthy.add(self.profiles[2]['id'])
            return self.launch(profile_id)
        self.warmup.launch = launch
        result = self.run_worker()
        self.assertEqual(self.launched, [self.profiles[0]['id']])
        self.assertEqual([p['state'] for p in result['profiles']], ['ready', 'skipped', 'skipped'])

    def test_launch_failure_does_not_block_other_profiles_or_leak_diagnostics(self):
        def launch(profile_id):
            if profile_id == self.profiles[0]['id']:
                raise RuntimeError('private-token-diagnostic')
            return self.launch(profile_id)
        self.warmup.launch = launch
        result = self.run_worker()
        self.assertEqual(result['state'], 'attention')
        self.assertEqual(self.launched, [p['id'] for p in self.profiles[1:]])
        self.assertEqual(result['profiles'][0]['code'], 'prepare_failed')
        self.assertNotIn('private-token', str(result))

    def test_shutdown_before_worker_and_after_launch_cancels_pending_work(self):
        self.warmup.start()
        self.warmup.shutdown()
        self.workers.pop()()
        self.assertEqual(self.launched, [])
        self.assertEqual(self.warmup.status()['state'], 'stopped')
        with self.assertRaises(RuntimeError):
            self.warmup.start()
        self.assertFalse(self.warmup.prioritize(self.profiles[0]['id']))

    def test_shutdown_in_flight_allows_current_callback_only(self):
        def launch(profile_id):
            self.warmup.shutdown()
            return self.launch(profile_id)
        self.warmup.launch = launch
        result = self.run_worker()
        self.assertEqual(self.launched, [self.profiles[0]['id']])
        self.assertEqual(result['state'], 'stopped')
        self.assertFalse(result['worker_active'])
        self.assertEqual([p['state'] for p in result['profiles']], ['ready', 'cancelled', 'cancelled'])

    def test_shutdown_during_health_check_never_enters_launcher(self):
        def health(profile):
            self.warmup.shutdown()
            return dict(blocks_launch=False)
        self.warmup.health = health
        result = self.run_worker()
        self.assertEqual(self.launched, [])
        self.assertEqual(result['counts']['cancelled'], 3)

    def test_selected_profile_gets_next_slot_without_second_worker(self):
        def launch(profile_id):
            if profile_id == self.profiles[0]['id']:
                self.assertTrue(self.warmup.prioritize(self.profiles[2]['id']))
                self.assertFalse(self.warmup.prioritize(profile_id))
                self.warmup.start()
            return self.launch(profile_id)
        self.warmup.launch = launch
        self.run_worker()
        self.assertEqual(self.launched, [self.profiles[i]['id'] for i in (0, 2, 1)])
        self.assertEqual(self.workers, [])

    def test_priority_before_snapshot_is_respected(self):
        self.warmup.start()
        self.assertTrue(self.warmup.prioritize(self.profiles[2]['id']))
        self.workers.pop()()
        self.assertEqual(self.launched, [self.profiles[i]['id'] for i in (2, 0, 1)])

    def test_started_without_window_is_not_reported_ready(self):
        self.warmup.launch = lambda _: dict(state='launched', profile=dict(status='running'))
        result = self.run_worker()
        self.assertEqual(result['counts']['started'], 3)
        self.assertEqual(result['counts']['ready'], 0)

    def test_nonlaunch_result_is_attention_and_never_retried(self):
        self.warmup.launch = lambda _: dict(state='blocked', profile=dict(status='not_started'))
        result = self.run_worker()
        self.assertEqual(result['counts']['attention'], 3)
        self.assertEqual(self.warmup.start(), result)

    def test_worker_start_failure_releases_active_flag_without_retry_loop(self):
        def fail(_):
            raise RuntimeError('thread unavailable')
        self.warmup.spawn = fail
        with self.assertRaisesRegex(RuntimeError, 'thread unavailable'):
            self.warmup.start()
        result = self.warmup.status()
        self.assertEqual((result['state'], result['worker_active'], result['code']),
                         ('attention', False, 'worker_start_failed'))
        self.assertEqual(self.warmup.start(), result)

    def test_profile_scan_failure_does_not_leave_worker_active(self):
        self.warmup.start()
        with patch.object(self.store, 'read', side_effect=OSError('private-diagnostic')):
            self.workers.pop()()
        result = self.warmup.status()
        self.assertEqual((result['state'], result['worker_active'], result['code']),
                         ('attention', False, 'profile_scan_failed'))
        self.assertNotIn('private-diagnostic', str(result))
        self.assertEqual(self.launched, [])

    def test_concurrent_start_admits_one_serial_worker(self):
        entered, release = threading.Event(), threading.Event()
        launched = []
        snapshot = deepcopy(self.profiles)
        def launch(profile_id):
            launched.append(profile_id)
            entered.set()
            self.assertTrue(release.wait(2))
            return dict(state='launched', profile=dict(status='running', window_handle=12))
        workers = []
        def spawn(fn):
            thread = threading.Thread(target=fn)
            workers.append(thread)
            thread.start()
        self.warmup = ProfileWarmup(self.store, self, launch, spawn=spawn,
                                   health=lambda _: dict(blocks_launch=False), max_workers=1)
        try:
            self.warmup.start()
            self.assertTrue(entered.wait(2))
            starters = [threading.Thread(target=self.warmup.start) for _ in range(5)]
            for thread in starters:
                thread.start()
            for thread in starters:
                thread.join(2)
            self.assertEqual(len(workers), 1)
            self.assertEqual(launched, [snapshot[0]['id']])
        finally:
            self.warmup.shutdown()
            release.set()
            for thread in workers:
                thread.join(2)
        self.assertTrue(all(not thread.is_alive() for thread in workers))
        self.assertEqual(self.warmup.status()['state'], 'stopped')

    def test_slow_launch_does_not_block_status_priority_or_shutdown(self):
        entered, release, responsive = threading.Event(), threading.Event(), threading.Event()
        responses, workers = [], []
        def launch(profile_id):
            self.launched.append(profile_id)
            entered.set()
            if not release.wait(3):
                raise TimeoutError('test launcher was not released')
            return dict(state='launched', profile=dict(status='running', window_handle=12))
        def spawn(fn):
            thread = threading.Thread(target=fn)
            workers.append(thread)
            thread.start()
        self.warmup = ProfileWarmup(self.store, self, launch, spawn=spawn,
                                   health=lambda _: dict(blocks_launch=False), max_workers=1)
        def interact():
            responses.append(self.warmup.status())
            responses.append(self.warmup.prioritize(self.profiles[2]['id']))
            self.warmup.shutdown()
            responses.append(self.warmup.status())
            responsive.set()
        controls = threading.Thread(target=interact)
        try:
            self.warmup.start()
            self.assertTrue(entered.wait(2))
            controls.start()
            self.assertTrue(responsive.wait(1), 'UI controls waited for the launcher')
            self.assertFalse(release.is_set())
            self.assertEqual(responses[0]['profiles'][0]['state'], 'opening')
            self.assertTrue(responses[1])
            self.assertEqual(responses[2]['state'], 'stopped')
            self.assertEqual(responses[2]['counts']['cancelled'], 2)
        finally:
            release.set()
            if controls.ident is not None:
                controls.join(2)
            for thread in workers:
                thread.join(2)
        self.assertFalse(controls.is_alive())
        self.assertTrue(all(not thread.is_alive() for thread in workers))
        self.assertEqual(self.launched, [self.profiles[0]['id']])
        self.assertFalse(self.warmup.status()['worker_active'])


if __name__ == '__main__':
    unittest.main()
