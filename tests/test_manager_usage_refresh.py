from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.store import Store
from manager_core.native_usage import normalize, presentation, newer
from manager_core.usage_refresh import UsageRefresh


class UsageRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.profile = self.store.add_profile('02')
        self.old = dict(windows=[dict(label='7d', remaining_percent=90)],
                        observed_at='2020-01-01T00:00:00Z', freshness='cached', error=None)
        self.store.mutate(lambda data: self.store.profile(self.profile['id'], data).update(
            account_fingerprint='a' * 64, usage=deepcopy(self.old)))
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.monitors = []

    def monitor(self, probe):
        monitor = UsageRefresh(self.root, self.store, probe=probe)
        self.monitors.append(monitor)
        return monitor

    def tearDown(self):
        self.release.set()
        for monitor in self.monitors:
            self.finish(monitor)

    def finish(self, monitor):
        with monitor.lock:
            threads = list(monitor.running.values())
        for thread in threads:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive(), 'quota fixture did not finish')

    def quota(self):
        return normalize({'rateLimits': {'primary': {'usedPercent': 24, 'windowDurationMins': 10080}}})

    def test_polling_returns_while_quota_is_pending_and_only_one_request_is_sent(self):
        called = threading.Event()
        def probe(profile):
            called.set()
            self.release.wait(2)
            return self.quota()
        monitor = self.monitor(probe)
        monitor.schedule()
        self.assertTrue(called.wait(1))
        for _ in range(10):
            monitor.schedule()
        self.assertTrue(monitor.active(self.profile['id']))
        with monitor.lock:
            self.assertEqual(len(monitor.running), 1)
        self.release.set()
        self.finish(monitor)
        profile = self.store.profile(self.profile['id'])
        self.assertEqual(profile['usage']['windows'][0]['remaining_percent'], 76)
        # Simulate an old llm-usage snapshot or an older manager overwriting disk.
        self.store.mutate(lambda data: self.store.profile(profile['id'], data).update(usage=self.old))
        current = monitor.value(self.store.profile(profile['id']))
        self.assertEqual(current['windows'][0]['remaining_percent'], 76)
        monitor.schedule()
        self.assertFalse(monitor.active(profile['id']))

    def test_a_changed_account_never_receives_the_previous_accounts_quota(self):
        started = threading.Event()
        def probe(profile):
            started.set()
            self.release.wait(2)
            return self.quota()
        monitor = self.monitor(probe)
        monitor.schedule()
        self.assertTrue(started.wait(1))
        self.store.mutate(lambda data: self.store.profile(self.profile['id'], data).update(account_fingerprint='b' * 64))
        self.release.set()
        self.finish(monitor)
        profile = self.store.profile(self.profile['id'])
        self.assertEqual(profile['usage'], self.old)
        self.assertEqual(monitor.value(profile), self.old)

    def test_failed_lookup_retains_old_value_with_stale_status_and_no_raw_error(self):
        def fail(profile):
            raise RuntimeError('private-token-or-server-response')
        monitor = self.monitor(fail)
        monitor.schedule()
        self.finish(monitor)
        usage = self.store.profile(self.profile['id'])['usage']
        self.assertEqual(usage['windows'][0]['remaining_percent'], 90)
        self.assertEqual(usage['freshness'], 'stale')
        self.assertNotIn('private-token', str(usage))
        self.assertGreater(presentation(usage)['age_seconds'], 86400)

    def test_parallel_quota_requests_are_bounded(self):
        for alias in ('03', '04', '05'):
            profile = self.store.add_profile(alias)
            self.store.mutate(lambda data: self.store.profile(profile['id'], data).update(account_fingerprint='a' * 64))
        def probe(profile):
            self.release.wait(2)
            return self.quota()
        monitor = self.monitor(probe)
        monitor.schedule()
        with monitor.lock:
            self.assertEqual(len(monitor.running), 2)
        self.release.set()
        self.finish(monitor)

    def test_unknown_identity_and_removed_profiles_are_not_queried(self):
        self.store.mutate(lambda data: self.store.profile(self.profile['id'], data).pop('account_fingerprint'))
        called = []
        monitor = self.monitor(lambda profile: called.append(profile))
        monitor.schedule()
        self.assertEqual(called, [])
        self.store.mutate(lambda data: self.store.profile(self.profile['id'], data).update(
            account_fingerprint='a' * 64, removed_at='fixture'))
        monitor.schedule()
        self.assertEqual(called, [])

    def test_older_import_cannot_replace_new_live_usage(self):
        fresh = self.quota()
        self.assertEqual(newer(fresh, self.old), fresh)
        self.assertEqual(newer(self.old, fresh), fresh)
        stale = presentation(dict(self.old, freshness='live'))
        self.assertEqual(stale['freshness'], 'stale')
        self.assertEqual(self.old['freshness'], 'cached')


if __name__ == '__main__':
    unittest.main()
