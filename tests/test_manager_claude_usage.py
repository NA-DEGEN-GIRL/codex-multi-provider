"""Subscription values come only from CLI metadata; no credentials or inference."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.claude_usage import (ClaudeUsage, normalize_event, normalize_statusline,
                                       read_quota, record_event, unavailable)
from manager_core.store import Store


IDENTITY = 'a' * 64


def event(key='five_hour', utilization=.25, reset=None):
    return {'type': 'rate_limit_event', 'rate_limit_info': {
        'rateLimitType': key, 'utilization': utilization,
        'resetsAt': reset or int(time.time()) + 3600,
        'access_token': 'secret-not-for-state', 'email': 'private@example.invalid'}}


class ClaudeUsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root)
        self.profile = self.store.add_profile('Claude quota', claude_settings={})
        self.pid = self.profile['id']
        self.set_auth(IDENTITY)

    def set_auth(self, identity, logged_in=True):
        self.store.mutate(lambda data: self.store.profile(self.pid, data).update(
            claude_account_identity=identity,
            claude_status={'logged_in': logged_in, 'observed_at': datetime.now(timezone.utc).isoformat()}))

    def value(self):
        return ClaudeUsage(self.root, self.store).value(self.store.profile(self.pid))

    def test_actual_fraction_and_reset_preserved_without_identity_or_raw_fields(self):
        source = event(utilization=.235, reset=2000000000)
        usage = normalize_event(source)
        window = usage['windows'][0]
        self.assertAlmostEqual(23.5, window['used_percent'])
        self.assertAlmostEqual(76.5, window['remaining_percent'])
        self.assertEqual(2000000000, window['resets_at'])
        self.assertNotIn('secret', json.dumps(usage))
        self.assertNotIn('email', json.dumps(usage))
        self.assertNotIn('reset_credits', usage)

    def test_missing_or_invalid_percent_never_becomes_zero(self):
        for used in (None, True, '0.2', -1, 1.01, float('nan'), float('inf')):
            with self.subTest(used=used):
                self.assertIsNone(normalize_event(event(utilization=used)))
        for key in ('seven_day_opus', 'seven_day_sonnet', 'overage', None, 'unknown'):
            self.assertIsNone(normalize_event(event(key)))
        self.assertIsNone(normalize_event({'type': 'result', 'usage': {'input_tokens': 2000}}))
        self.assertEqual(0, normalize_event(event(utilization=0))['windows'][0]['used_percent'])

    def test_statusline_accepts_only_provider_quota_fields(self):
        usage = normalize_statusline({'rate_limits': {
            'five_hour': {'used_percentage': 50.5, 'resets_at': 2000000000},
            'seven_day': {'used_percentage': 7, 'resets_at': None},
            'spend_limit': {'used_percentage': 19}}, 'transcript_path': 'private',
            'context_window': {'used_percentage': 91}, 'cost': {'total_cost_usd': 1}})
        self.assertEqual([50.5, 7], [w['used_percent'] for w in usage['windows']])
        self.assertNotIn('private', json.dumps(usage))
        self.assertIsNone(normalize_statusline({'context_window': {'used_percentage': 91}}))

    def test_per_profile_and_account_event_isolation(self):
        other = self.store.add_profile('Other Claude', claude_settings={})
        self.assertTrue(record_event(self.store, self.pid, IDENTITY, event()))
        self.assertFalse(record_event(self.store, other['id'], IDENTITY, event()))
        self.assertFalse(record_event(self.store, self.pid, 'b'*64, event()))
        self.assertFalse(record_event(self.store, self.pid, None, event()))
        self.assertEqual(1, len(self.value()['windows']))
        self.assertFalse(self.store.profile(other['id']).get('usage'))
        self.assertNotIn('private@example', self.store.path.read_text(encoding='utf-8'))
        self.assertNotIn('secret-not', self.store.path.read_text(encoding='utf-8'))

    def test_windows_merge_without_inventing_missing_one(self):
        record_event(self.store, self.pid, IDENTITY, event('five_hour'))
        self.assertEqual(['five_hour'], [w['key'] for w in self.value()['windows']])
        record_event(self.store, self.pid, IDENTITY, event('seven_day', .64))
        self.assertEqual(['five_hour', 'seven_day'], [w['key'] for w in self.value()['windows']])

    def test_event_cannot_freshen_older_other_window(self):
        stamp = (datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
        old = normalize_event(event('seven_day'), observed_at=stamp)
        self.store.mutate(lambda data: self.store.profile(self.pid, data).update(usage=old))
        record_event(self.store, self.pid, IDENTITY, event())
        value = self.value()
        self.assertEqual('stale', value['freshness'])
        self.assertEqual(['live', 'stale'], [w['freshness'] for w in value['windows']])
        self.assertEqual(stamp, value['observed_at'])

    def test_passed_reset_is_stale_never_automatically_zero(self):
        record_event(self.store, self.pid, IDENTITY, event(utilization=.7, reset=int(time.time())-1))
        value = self.value()
        self.assertEqual('stale', value['freshness'])
        self.assertEqual(70, value['windows'][0]['used_percent'])

    def test_logged_out_or_unverified_never_exposes_old_windows(self):
        record_event(self.store, self.pid, IDENTITY, event())
        self.set_auth(None, False)
        self.assertEqual([], self.value()['windows'])
        self.assertEqual('not_logged_in', self.value()['error']['code'])
        self.set_auth(None, True)
        self.assertEqual([], self.value()['windows'])
        self.assertEqual('account_unverified', self.value()['error']['code'])

    def test_read_probe_only_calls_official_auth_metadata(self):
        status = {'logged_in': True, 'account_identity': IDENTITY}
        with patch('manager_core.claude_usage.auth_status', return_value=status) as auth, \
                patch('manager_core.claude_usage_terminal.query', return_value=(None, 'interactive_usage_required')) as query:
            result = read_quota(self.profile)
        auth.assert_called_once_with(self.pid)
        query.assert_called_once_with(self.pid)
        self.assertEqual('interactive_usage_required', result['usage']['error']['code'])
        self.assertEqual([], result['usage']['windows'])

    def test_terminal_values_recheck_auth_and_preserve_provider_reset_text(self):
        status = {'logged_in': True, 'account_identity': IDENTITY}
        window = {'key': 'five_hour', 'used_percent': 33, 'resets_at': None,
                  'reset_text': 'Resets 11:42am (UTC)'}
        with patch('manager_core.claude_usage.auth_status', return_value=status) as auth, \
                patch('manager_core.claude_usage_terminal.query', return_value=([window], None)):
            result = read_quota(self.profile)
        self.assertEqual(2, auth.call_count)
        self.assertEqual(33, result['usage']['windows'][0]['used_percent'])
        self.assertEqual('Resets 11:42am (UTC)', result['usage']['windows'][0]['reset_text'])

    def test_account_switch_during_terminal_probe_drops_values(self):
        first = {'logged_in': True, 'account_identity': IDENTITY}
        second = {'logged_in': True, 'account_identity': 'b'*64}
        with patch('manager_core.claude_usage.auth_status', side_effect=[first, second]), \
                patch('manager_core.claude_usage_terminal.query', return_value=([
                    {'key': 'five_hour', 'used_percent': 33, 'resets_at': None}], None)):
            result = read_quota(self.profile)
        self.assertEqual([], result['usage']['windows'])
        self.assertEqual('account_unverified', result['usage']['error']['code'])

    def test_manual_refresh_ttl_coalesces_and_preserves_same_account_values(self):
        record_event(self.store, self.pid, IDENTITY, event())
        probe = Mock(return_value=dict(status={'logged_in': True}, account_identity=IDENTITY,
                                       usage=unavailable('interactive_usage_required')))
        refresh = ClaudeUsage(self.root, self.store, probe=probe)
        first, second = refresh.refresh(self.pid), refresh.refresh(self.pid)
        self.assertEqual(1, probe.call_count)
        self.assertEqual(25, first['windows'][0]['used_percent'])
        self.assertEqual('stale', first['freshness'])
        self.assertEqual(first, second)

    def test_refresh_changed_account_discards_other_account_values(self):
        record_event(self.store, self.pid, IDENTITY, event())
        probe = lambda p: dict(status={'logged_in': True}, account_identity='b'*64,
                               usage=unavailable('interactive_usage_required'))
        value = ClaudeUsage(self.root, self.store, probe=probe).refresh(self.pid)
        self.assertEqual([], value['windows'])
        self.assertEqual('b'*64, self.store.profile(self.pid)['claude_account_identity'])

    def test_refresh_cannot_commit_after_concurrent_login_switch(self):
        def probe(profile):
            self.set_auth('b'*64)
            return dict(status={'logged_in': True}, account_identity=IDENTITY, usage=normalize_event(event()))
        refresh = ClaudeUsage(self.root, self.store, probe=probe)
        self.assertEqual([], refresh.refresh(self.pid)['windows'])
        self.assertEqual('b'*64, self.store.profile(self.pid)['claude_account_identity'])

    def test_failed_refresh_keeps_previous_value_and_fixed_error(self):
        record_event(self.store, self.pid, IDENTITY, event())
        probe = Mock(side_effect=RuntimeError('private@example.invalid secret-token'))
        value = ClaudeUsage(self.root, self.store, probe=probe).refresh(self.pid)
        self.assertEqual('stale', value['freshness'])
        self.assertEqual(25, value['windows'][0]['used_percent'])
        self.assertEqual('refresh_failed', value['error']['code'])
        self.assertNotIn('private@example', json.dumps(value))

    def test_inflight_refresh_does_not_duplicate_probe(self):
        started, released = threading.Event(), threading.Event()
        def probe(profile):
            started.set()
            released.wait(2)
            return dict(status={'logged_in': True}, account_identity=IDENTITY,
                        usage=unavailable('interactive_usage_required'))
        refresh = ClaudeUsage(self.root, self.store, probe=probe)
        worker = threading.Thread(target=refresh.refresh, args=(self.pid,))
        worker.start()
        try:
            self.assertTrue(started.wait(1))
            self.assertTrue(refresh.refresh(self.pid)['refreshing'])
        finally:
            released.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertFalse(refresh.active(self.pid))

    def test_manual_refresh_shares_global_two_process_bound(self):
        profiles = [self.profile] + [self.store.add_profile('Quota '+str(i), claude_settings={}) for i in range(2)]
        entered, release, calls = [threading.Event(), threading.Event()], threading.Event(), []
        readiness = {p['id']: ready for p, ready in zip(profiles[:2], entered)}
        def probe(profile):
            calls.append(profile['id'])
            if profile['id'] in readiness:
                readiness[profile['id']].set()
                release.wait(5)
            return dict(status={'logged_in': False}, account_identity=None, usage=unavailable('not_logged_in'))
        refresh = ClaudeUsage(self.root, self.store, probe=probe)
        workers = [threading.Thread(target=refresh.refresh, args=(p['id'],)) for p in profiles[:2]]
        try:
            for worker in workers:
                worker.start()
            self.assertTrue(all(ready.wait(3) for ready in entered))
            refresh.refresh(profiles[2]['id'])
            self.assertEqual(2, len(calls))
        finally:
            release.set()
            for worker in workers:
                worker.join(5)
        refresh.refresh(profiles[2]['id'])
        self.assertEqual(3, len(calls))


if __name__ == '__main__':
    unittest.main()
