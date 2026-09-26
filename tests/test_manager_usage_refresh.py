from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core import usage_refresh
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

    def observed(self, seconds_ago):
        return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()

    def set_usage(self, **fields):
        self.store.mutate(lambda data: self.store.profile(self.profile['id'], data)['usage'].update(fields))

    def test_background_quota_uses_a_five_minute_cadence(self):
        called = []
        monitor = self.monitor(lambda profile: called.append(profile['id']) or self.quota())
        self.assertEqual(monitor.interval, 300)
        self.set_usage(observed_at=self.observed(100))
        monitor.schedule()
        self.finish(monitor)
        self.assertEqual(called, [])
        self.set_usage(observed_at=self.observed(400))
        monitor.schedule()
        self.finish(monitor)
        self.assertEqual(called, [self.profile['id']])

    def test_given_profiles_are_used_without_reading_the_store(self):
        called = threading.Event()
        def probe(profile):
            called.set()
            self.release.wait(2)
            return self.quota()
        monitor = self.monitor(probe)
        profiles = self.store.read()['profiles']
        with patch.object(self.store, 'read', side_effect=AssertionError('store read')):
            monitor.schedule(profiles)
        self.assertTrue(called.wait(1))
        self.release.set()
        self.finish(monitor)
        self.assertEqual(self.store.profile(self.profile['id'])['usage']['windows'][0]['remaining_percent'], 76)

    def test_unchanged_windows_skip_the_state_rewrite_and_failures_still_turn_stale(self):
        stored = dict(self.quota(), observed_at=self.observed(400))
        self.set_usage(**deepcopy(stored))
        first, second = self.quota(), self.quota()
        replies = [deepcopy(first), RuntimeError('fixture'), deepcopy(second)]
        def probe(profile):
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        monitor = UsageRefresh(self.root, self.store, probe=probe, interval=0)
        self.monitors.append(monitor)
        revision, written = self.store.read()['revision'], self.store.path.read_bytes()
        monitor.schedule()
        self.finish(monitor)
        self.assertEqual((self.store.read()['revision'], self.store.path.read_bytes()), (revision, written))
        profile = self.store.profile(self.profile['id'])
        self.assertEqual(profile['usage']['observed_at'], stored['observed_at'])
        self.assertEqual(monitor.value(profile)['observed_at'], first['observed_at'])
        monitor.schedule()
        self.finish(monitor)
        shown = monitor.value(self.store.profile(self.profile['id']))
        self.assertEqual((shown['freshness'], shown['error']['code']), ('stale', 'refresh_failed'))
        self.assertEqual(shown['windows'], first['windows'])
        # The saved value is stale now, so an identical reply is written again.
        monitor.schedule()
        self.finish(monitor)
        saved = self.store.profile(self.profile['id'])['usage']
        self.assertEqual((saved['error'], saved['observed_at']), (None, second['observed_at']))

    def test_a_mark_saved_while_the_probe_runs_is_replaced_by_the_later_success(self):
        stored = dict(self.quota(), observed_at=self.observed(400))
        self.set_usage(**deepcopy(stored))
        reply, started = self.quota(), threading.Event()
        def probe(profile):
            started.set()
            self.release.wait(2)
            return deepcopy(reply)
        monitor = self.monitor(probe)
        monitor.schedule()
        self.assertTrue(started.wait(1))
        # refresh_all marks the saved value while the background probe is out.
        self.set_usage(freshness='stale', error=dict(code='refresh_failed', message='fixture'))
        revision = self.store.read()['revision']
        self.release.set()
        self.finish(monitor)
        self.assertEqual(self.store.read()['revision'], revision + 1)
        saved = self.store.profile(self.profile['id'])['usage']
        self.assertEqual((saved['freshness'], saved['error']), ('live', None))
        self.assertEqual((saved['observed_at'], saved['windows']), (reply['observed_at'], stored['windows']))
        shown = monitor.value(self.store.profile(self.profile['id']))
        self.assertEqual((shown['freshness'], shown['error']), ('live', None))
        self.assertEqual(presentation(shown)['freshness'], 'live')

    def test_idle_profile_rewrites_a_saved_observation_older_than_30_minutes(self):
        self.set_usage(**dict(self.quota(), observed_at=self.observed(2000)))
        reply = self.quota()
        monitor = self.monitor(lambda profile: deepcopy(reply))
        monitor.schedule()
        self.finish(monitor)
        saved = self.store.profile(self.profile['id'])['usage']
        self.assertEqual((saved['observed_at'], saved['windows']), (reply['observed_at'], reply['windows']))

    def test_a_later_saved_stale_mark_is_not_hidden_by_the_reply_kept_in_memory(self):
        stored = dict(self.quota(), observed_at=self.observed(400))
        self.set_usage(**deepcopy(stored))
        reply = self.quota()
        monitor = self.monitor(lambda profile: deepcopy(reply))
        monitor.schedule()
        self.finish(monitor)
        self.assertEqual(self.store.profile(self.profile['id'])['usage']['observed_at'], stored['observed_at'])
        self.assertEqual(monitor.value(self.store.profile(self.profile['id']))['freshness'], 'live')
        # native_login marks the saved value after a sign-out or a failed refresh_all.
        self.set_usage(freshness='stale', error=dict(code='signed_out', message='fixture'))
        shown = monitor.value(self.store.profile(self.profile['id']))
        self.assertEqual((shown['freshness'], shown['error']['code']), ('stale', 'signed_out'))
        self.assertEqual((shown['observed_at'], shown['windows']), (reply['observed_at'], reply['windows']))
        self.assertEqual(presentation(shown)['freshness'], 'stale')
        self.set_usage(freshness='live', error=None)  # the memory copy itself stays clean
        self.assertEqual(monitor.value(self.store.profile(self.profile['id']))['freshness'], 'live')

    def test_stale_probe_purge_repeats_after_six_hours_and_never_overlaps(self):
        calls, entered, release = [], threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def purge(root):
            calls.append(root)
            entered.set()
            release.wait(2)
        def join():
            usage_refresh._purges[str(self.root)][1].join(3)
        monitor = self.monitor(lambda profile: self.quota())
        with patch.object(usage_refresh, '_PURGE_DELAY', 0), \
             patch('manager_core.login_probe.purge_in_child', side_effect=purge):
            monitor.schedule()
            monitor.schedule()
            self.assertTrue(entered.wait(2))
            with patch.object(usage_refresh, '_PURGE_EVERY', 0):
                monitor.schedule()  # the running purge is not started twice
                release.set()
                join()
                monitor.schedule()  # the last run is older than the period
                join()
            monitor.schedule()  # within six hours of the last run
            join()
        self.assertEqual(calls, [self.root, self.root])
        self.assertEqual(usage_refresh._PURGE_EVERY, 6 * 3600)

    def test_background_probe_reuses_the_service_package_lookup(self):
        reply = self.quota()
        with patch('manager_core.proxy_auth.read_existing_tokens', return_value=SimpleNamespace(account_id='fixture')), \
             patch('manager_core.proxy_auth.account_fingerprint', return_value='a' * 64), \
             patch('desktop_launch.cached_app', return_value={'Version': 'fixture'}) as lookup, \
             patch('desktop_launch.find_app', side_effect=AssertionError('uncached package lookup')), \
             patch('manager_core.login_probe.verification_runtime', return_value='fixture') as runtime, \
             patch('manager_core.login_probe.verify', return_value=dict(quota_read=True, usage=reply)):
            self.assertEqual(usage_refresh.read_quota(self.root, self.store.profile(self.profile['id'])), reply)
        lookup.assert_called_once_with()
        self.assertEqual(runtime.call_args.args[1], {'Version': 'fixture'})

    def test_older_import_cannot_replace_new_live_usage(self):
        fresh = self.quota()
        self.assertEqual(newer(fresh, self.old), fresh)
        self.assertEqual(newer(self.old, fresh), fresh)
        stale = presentation(dict(self.old, freshness='live'))
        self.assertEqual(stale['freshness'], 'stale')
        self.assertEqual(self.old['freshness'], 'cached')


if __name__ == '__main__':
    unittest.main()
