"""Revision 124: usage-limit stops, automatic continuation and usage alerts (offline, dummy data)."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from manager_core.app_transport import RuntimeObserver
from manager_core.claude_runner import usage_limit_message
from manager_core.runtime_admin import CONTINUATION_TEXT, AdminError, sanitize_result, validate_request
from manager_core.store import Store
from manager_core.usage_alerts import UsageAlerts
from manager_core.usage_continuation import (CLOSED_GRACE, MAX_ATTEMPTS, UsageContinuations, parse_stop,
                                             record_stops)

NOW = 1800000000.0


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


class Clock:
    def __init__(self, value=NOW):
        self.value = value

    def __call__(self):
        return self.value


class FakeAdmin:
    """The runtime admin endpoint of one profile: a thread, its newest turn, recorded calls."""
    def __init__(self, thread_status='idle', newest=None, turn_status='failed', error=None, start_error=None):
        self.thread_status, self.newest, self.turn_status = thread_status, newest, turn_status
        self.error, self.start_error = error, start_error
        self.calls = []

    def __call__(self, profile, host):
        if self.error is not None:
            raise self.error
        self.host = host
        return self

    def request(self, method, params, timeout=15):
        validate_request(method, params)  # Only what the real endpoint would admit.
        self.calls.append((method, params))
        if method == 'thread/read':
            return {'thread': {'id': params['threadId'], 'status': {'type': self.thread_status}}}
        if method == 'thread/turns/list':
            return {'data': [{'id': self.newest, 'status': self.turn_status}] if self.newest else []}
        if self.start_error is not None:
            raise self.start_error
        return {'turn': {'id': 'continued-turn', 'status': 'inProgress'}}


class UsageStopObserverTests(unittest.TestCase):
    def test_observer_keeps_only_ids_kind_and_reset_of_a_usage_limit_stop(self):
        observer = RuntimeObserver(str(uuid4()))
        thread = str(uuid4())
        message = 'Claude agent: ' + usage_limit_message('session', int(NOW) + 3600, '4:30am (Asia/Seoul)')
        observer.consume('server', {'method': 'error', 'params': {'threadId': thread, 'turnId': 'turn-1',
                                                                 'willRetry': False, 'error': {'message': message}}})
        observer.consume('server', {'method': 'turn/completed', 'params': {'threadId': thread, 'turn': {
            'id': 'turn-1', 'status': 'failed', 'error': {'message': message}}}})
        # Other failures, retried errors and successful turns are not stops.
        observer.consume('server', {'method': 'turn/completed', 'params': {'threadId': thread, 'turn': {
            'id': 'turn-2', 'status': 'failed', 'error': {'message': 'Claude agent: Claude did not complete this turn.'}}}})
        observer.consume('server', {'method': 'error', 'params': {'threadId': thread, 'turnId': 'turn-3',
                                                                 'willRetry': True, 'error': {'message': message}}})
        observer.consume('server', {'method': 'turn/completed', 'params': {'threadId': thread, 'turn': {
            'id': 'turn-4', 'status': 'completed', 'error': {'message': message}}}})
        stops = observer.drain_usage_stops()
        self.assertEqual([(s['thread_id'], s['turn_id'], s['kind'], s['resets_at']) for s in stops],
                         [(thread, 'turn-1', 'session', int(NOW) + 3600)])
        self.assertNotIn('Asia/Seoul', json.dumps(stops))
        self.assertEqual(observer.drain_usage_stops(), [])

    def test_parse_stop_needs_the_runner_marker_and_tag(self):
        self.assertIsNone(parse_stop('[usage-limit: session, resets 2027-01-15T10:00:00Z]'))
        self.assertIsNone(parse_stop('Claude usage limit reached without a tag'))
        self.assertIsNone(parse_stop(None))
        self.assertEqual(parse_stop('Claude usage limit reached ... [usage-limit: bogus]'), dict(kind='unknown', resets_at=None))


class AdminContinuationTests(unittest.TestCase):
    def test_only_the_fixed_continuation_and_newest_turn_read_are_admitted(self):
        thread = str(uuid4())
        good = {'threadId': thread, 'input': [{'type': 'text', 'text': CONTINUATION_TEXT, 'text_elements': []}]}
        self.assertEqual(validate_request('turn/start', good), good)
        for params in ({'threadId': thread, 'input': [{'type': 'text', 'text': 'anything else', 'text_elements': []}]},
                       {**good, 'model': 'other'}, {'threadId': thread},
                       {'threadId': thread, 'input': good['input'] * 2}, {**good, 'threadId': '../x'}):
            with self.subTest(params=params), self.assertRaises(AdminError):
                validate_request('turn/start', params)
        listing = {'threadId': thread, 'limit': 1, 'sortDirection': 'desc', 'itemsView': 'notLoaded'}
        self.assertEqual(validate_request('thread/turns/list', listing), listing)
        for params in ({**listing, 'limit': 50}, {**listing, 'itemsView': 'full'}, {**listing, 'cursor': 'x'},
                       {'threadId': thread}):
            with self.subTest(params=params), self.assertRaises(AdminError):
                validate_request('thread/turns/list', params)
        # Results keep ids and statuses only, never items or error text.
        self.assertEqual(sanitize_result('thread/turns/list', listing, {'data': [{
            'id': 'turn-1', 'status': 'failed', 'items': [{'text': 'private'}], 'error': {'message': 'private'}}]}),
            {'data': [{'id': 'turn-1', 'status': 'failed'}]})
        self.assertEqual(sanitize_result('turn/start', good, {'turn': {'id': 'turn-2', 'status': 'inProgress', 'items': []}}),
                         {'turn': {'id': 'turn-2', 'status': 'inProgress'}})
        for value in ({'data': [{'id': 'a b', 'status': 'failed'}]}, {'data': [{'id': 'a', 'status': 'odd'}]},
                      {'data': [{'id': 'a', 'status': 'failed'}] * 2}):
            with self.subTest(value=value), self.assertRaises(AdminError):
                sanitize_result('thread/turns/list', listing, value)


class ContinuationSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root)
        self.profile = self.store.add_profile('Claude test', claude_settings={})
        self.store.mutate(lambda data: self.store.profile(self.profile['id'], data).update(generation=str(uuid4())))
        self.thread = str(uuid4())
        self.clock = Clock()

    def scheduler(self, admin):
        return UsageContinuations(self.root, self.store, clock=self.clock, admin=admin, jitter=lambda: 90.0)

    def stop(self, turn='turn-1', resets_at=int(NOW) + 3600, host='local', kind='session', observed=NOW):
        record_stops(self.root, self.profile['id'], host, [dict(thread_id=self.thread, turn_id=turn, kind=kind,
                                                               resets_at=resets_at, observed_at=observed)])

    def log(self):
        path = self.root / 'work/control-center/usage-continuations/log.jsonl'
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]

    def test_one_continuation_is_sent_after_the_reset_and_survives_a_restart(self):
        admin = FakeAdmin(newest='turn-1')
        self.stop()
        self.stop()  # The same stop observed twice is one inbox record.
        first = self.scheduler(admin)
        self.assertEqual(first.ingest(), 1)
        entry = first.scheduled(self.profile['id'])
        self.assertEqual((entry['due_at'], entry['status']), (NOW + 3600 + 90, 'scheduled'))
        first.tick()
        self.assertEqual(admin.calls, [])  # Not due yet.
        # A manager restart keeps the schedule.
        second = self.scheduler(admin)
        self.clock.value = NOW + 3600 + 91
        second.tick()
        self.assertEqual([method for method, _ in admin.calls], ['thread/read', 'thread/turns/list', 'turn/start'])
        self.assertEqual(admin.calls[-1][1]['input'][0]['text'], CONTINUATION_TEXT)
        self.assertEqual(admin.host, 'local')
        sent = second.entries()[0]
        self.assertEqual((sent['status'], sent['sent_turn_id']), ('sent', 'continued-turn'))
        second.tick()
        self.assertEqual(len(admin.calls), 3)  # One continuation per stop.
        self.assertEqual([event['action'] for event in self.log()], ['scheduled', 'sent'])
        self.assertEqual([event['action'] for event in second.events()], ['scheduled', 'sent'])
        self.assertNotIn(CONTINUATION_TEXT, (self.root / 'work/control-center/usage-continuations/log.jsonl').read_text(encoding='utf-8'))

    def test_skip_conditions_are_checked_when_the_continuation_is_due(self):
        cases = (('new_message', FakeAdmin(newest='turn-2')), ('thread_running', FakeAdmin(thread_status='active', newest='turn-1')),
                 ('thread_not_open', FakeAdmin(thread_status='notLoaded', newest='turn-1')),
                 ('turn_changed', FakeAdmin(newest='turn-1', turn_status='completed')),
                 ('runtime_outdated', FakeAdmin(error=AdminError('invalid_request'))),
                 ('send_uncertain', FakeAdmin(newest='turn-1', start_error=AdminError('timeout', uncertain=True))))
        for index, (reason, admin) in enumerate(cases):
            with self.subTest(reason=reason):
                self.stop(turn='turn-skip-%d' % index)
                if admin.newest == 'turn-1':
                    admin.newest = 'turn-skip-%d' % index  # The stopped turn is still the newest.
                scheduler = self.scheduler(admin)
                scheduler.ingest()
                self.clock.value = NOW + 3600 + 91
                scheduler.tick()
                entry = next(entry for entry in scheduler.entries() if entry['turn_id'] == 'turn-skip-%d' % index)
                self.assertEqual((entry['status'], entry['reason']), ('skipped', reason))
                self.assertEqual(sum(method == 'turn/start' for method, _ in admin.calls), 1 if reason == 'send_uncertain' else 0)
                scheduler.tick()
                self.assertEqual(sum(method == 'turn/start' for method, _ in admin.calls), 1 if reason == 'send_uncertain' else 0)
                self.clock.value = NOW

    def test_profile_setting_removal_and_unknown_reset_skip_at_schedule_and_due_time(self):
        admin = FakeAdmin(newest='turn-1')
        self.store.mutate(lambda data: self.store.profile(self.profile['id'], data).update(claude_auto_continue=False))
        self.stop(turn='off')
        self.stop(turn='unknown', resets_at=None)
        self.stop(turn='far', resets_at=int(NOW) + 30 * 86400)
        scheduler = self.scheduler(admin)
        scheduler.ingest()
        self.assertEqual({entry['turn_id']: entry['reason'] for entry in scheduler.entries()},
                         {'off': 'disabled', 'unknown': 'disabled', 'far': 'disabled'})
        self.store.mutate(lambda data: self.store.profile(self.profile['id'], data).update(claude_auto_continue=True))
        self.stop(turn='unknown-2', resets_at=None)
        self.stop(turn='far-2', resets_at=int(NOW) + 30 * 86400)
        self.stop(turn='later')
        scheduler.ingest()
        reasons = {entry['turn_id']: entry.get('reason') for entry in scheduler.entries()}
        self.assertEqual((reasons['unknown-2'], reasons['far-2'], reasons['later']), ('reset_unknown', 'reset_too_far', None))
        # Turned off after it was scheduled; then a removed profile.
        self.store.mutate(lambda data: self.store.profile(self.profile['id'], data).update(claude_auto_continue=False))
        self.clock.value = NOW + 3600 + 91
        scheduler.tick()
        self.assertEqual(next(e for e in scheduler.entries() if e['turn_id'] == 'later')['reason'], 'disabled')
        self.store.mutate(lambda data: self.store.profile(self.profile['id'], data).update(claude_auto_continue=True,
                                                                                            removed_at='2026-10-10T00:00:00Z'))
        self.stop(turn='removed', observed=NOW + 3600)
        scheduler.ingest()
        self.assertEqual(next(e for e in scheduler.entries() if e['turn_id'] == 'removed')['reason'], 'profile_removed')
        self.assertEqual(admin.calls, [])

    def test_a_closed_profile_is_waited_for_then_skipped(self):
        admin = FakeAdmin(error=AdminError('unavailable'))
        self.stop()
        scheduler = self.scheduler(admin)
        scheduler.ingest()
        self.clock.value = NOW + 3600 + 91
        scheduler.tick()
        scheduler.tick()
        entry = scheduler.entries()[0]
        self.assertEqual((entry['status'], entry.get('waiting')), ('scheduled', True))
        # It reopens within the grace period: the continuation goes out.
        admin.error, admin.newest = None, 'turn-1'
        scheduler.tick()
        self.assertEqual(scheduler.entries()[0]['status'], 'sent')
        self.stop(turn='turn-closed', resets_at=int(NOW) + 7200)
        admin.error = AdminError('stale_runtime')
        scheduler.ingest()
        self.clock.value = NOW + 7200 + 90 + CLOSED_GRACE + 1
        scheduler.tick()
        entry = next(e for e in scheduler.entries() if e['turn_id'] == 'turn-closed')
        self.assertEqual((entry['status'], entry['reason']), ('skipped', 'profile_closed'))
        self.assertEqual([e['action'] for e in self.log()].count('waiting'), 1)

    def test_a_still_exhausted_window_reschedules_at_most_three_times(self):
        admin = FakeAdmin(newest='turn-1')
        self.stop()
        scheduler = self.scheduler(admin)
        scheduler.ingest()
        reset = int(NOW) + 3600
        for attempt in range(1, MAX_ATTEMPTS + 2):
            self.clock.value = reset + 91
            reset += 5 * 3600
            window = dict(key='five_hour', label='5시간', used_percent=100, remaining_percent=0, resets_at=reset,
                          observed_at=iso(self.clock.value))
            self.store.mutate(lambda data: self.store.profile(self.profile['id'], data).update(
                usage=dict(provider='claude_code', windows=[window], observed_at=window['observed_at'])))
            scheduler.tick()
            entry = scheduler.entries()[0]
            if attempt <= MAX_ATTEMPTS:
                self.assertEqual((entry['status'], entry['attempts'], entry['due_at']), ('scheduled', attempt, reset + 90))
            else:
                self.assertEqual((entry['status'], entry['reason']), ('skipped', 'still_limited'))
        self.assertEqual(admin.calls, [])
        self.assertEqual([e['action'] for e in self.log()].count('rescheduled'), MAX_ATTEMPTS)

    def test_a_continuation_that_stops_again_at_once_inherits_its_attempts(self):
        admin = FakeAdmin(newest='turn-1')
        self.stop()
        scheduler = self.scheduler(admin)
        scheduler.ingest()
        self.clock.value = NOW + 3600 + 91
        scheduler.tick()
        self.stop(turn='continued-turn', resets_at=int(NOW) + 5 * 3600, observed=self.clock.value + 30)
        scheduler.ingest()
        chained = next(e for e in scheduler.entries() if e['turn_id'] == 'continued-turn')
        self.assertEqual((chained['status'], chained['attempts']), ('scheduled', 1))

    def test_ssh_stops_use_their_host_endpoint(self):
        admin = FakeAdmin(newest='turn-1')
        self.stop(host='ssh:remote-dev')
        scheduler = self.scheduler(admin)
        scheduler.ingest()
        self.clock.value = NOW + 3600 + 91
        scheduler.tick()
        self.assertEqual((admin.host, scheduler.entries()[0]['status']), ('ssh:remote-dev', 'sent'))
        record_stops(self.root, self.profile['id'], 'ssh:bad host', [dict(thread_id=self.thread, turn_id='x', kind='session',
                                                                          resets_at=None, observed_at=NOW)])
        record_stops(self.root, 'not-a-profile', 'local', [dict(thread_id=self.thread, turn_id='x', kind='session',
                                                                resets_at=None, observed_at=NOW)])
        self.assertEqual(scheduler.ingest(), 0)


class UsageAlertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.clock = Clock()
        self.reset = int(NOW) + 3600

    def profile(self, used, *, claude=True, reset=None, label='5시간', pid='11111111-1111-4111-8111-111111111111'):
        window = dict(label=label, used_percent=used, remaining_percent=100 - used, resets_at=reset or self.reset)
        if claude:
            window.update(key='five_hour' if label == '5시간' else 'seven_day', freshness='live')
        return dict(id=pid, alias='계정', usage=dict(windows=[window], freshness='live'))

    def test_each_level_alerts_once_per_window_and_reset_period(self):
        alerts = UsageAlerts(self.root, clock=self.clock)
        self.assertEqual(alerts.evaluate([self.profile(10)]), [])  # First run: baseline only.
        self.assertEqual([a['kind'] for a in alerts.evaluate([self.profile(91)])], ['warn'])
        self.assertEqual([a['kind'] for a in alerts.evaluate([self.profile(95)])], ['warn'])  # Same period: no repeat.
        self.assertEqual([a['kind'] for a in alerts.evaluate([self.profile(100)])], ['warn', 'limit'])
        # A manager restart keeps what was delivered.
        again = UsageAlerts(self.root, clock=self.clock)
        self.assertEqual([a['kind'] for a in again.evaluate([self.profile(100)])], ['warn', 'limit'])
        self.clock.value = self.reset + 1
        result = again.evaluate([self.profile(100)])  # Stale value past its reset: no new limit alert.
        self.assertEqual([a['kind'] for a in result], ['warn', 'limit', 'reset'])
        self.assertEqual(result[-1]['title'], '다시 사용 가능')
        # The next period alerts again.
        self.clock.value = self.reset + 3600
        result = again.evaluate([self.profile(92, reset=self.reset + 5 * 3600)])
        self.assertEqual([a['kind'] for a in result][-1], 'warn')

    def test_codex_weekly_windows_and_stale_values(self):
        alerts = UsageAlerts(self.root, clock=self.clock)
        alerts.evaluate([])
        codex = self.profile(90, claude=False, label='주간')
        self.assertEqual([a['title'] for a in alerts.evaluate([codex])], ['주간 한도 90% 사용'])
        stale = self.profile(100, pid='22222222-2222-4222-8222-222222222222')
        stale['usage']['windows'][0]['freshness'] = 'stale'
        old_codex = self.profile(100, claude=False, pid='44444444-4444-4444-8444-444444444444')
        old_codex['usage']['freshness'] = 'stale'
        other = self.profile(100, label='기본', claude=False, pid='33333333-3333-4333-8333-333333333333')
        self.assertEqual(len(alerts.evaluate([codex, stale, old_codex, other])), 1)
        # A Claude window stays usable when only another window or the refresh is stale.
        live = self.profile(100, pid='55555555-5555-4555-8555-555555555555')
        live['usage']['freshness'] = 'stale'
        self.assertEqual([a['kind'] for a in alerts.evaluate([live])][-1], 'limit')

    def test_a_usage_limit_stop_alerts_once_and_replaces_the_limit_alert(self):
        alerts = UsageAlerts(self.root, clock=self.clock)
        alerts.evaluate([])
        stop = dict(key='k1', profile_id='11111111-1111-4111-8111-111111111111', kind='session', resets_at=self.reset,
                    stopped_at=NOW, status='scheduled', due_at=self.reset + 90, thread_id=str(uuid4()), host_id='local')
        result = alerts.evaluate([self.profile(100)], [stop])
        self.assertEqual([a['kind'] for a in result], ['stopped'])
        self.assertIn('자동으로 이어 합니다', result[0]['body'])
        self.assertEqual((result[0]['thread_id'], result[0]['host_id']), (stop['thread_id'], 'local'))
        self.assertEqual(len(alerts.evaluate([self.profile(100)], [stop])), 1)
        self.clock.value = self.reset + 1
        self.assertEqual([a['kind'] for a in alerts.evaluate([self.profile(100)], [stop])], ['stopped', 'reset'])

    def test_card_attention(self):
        self.assertIsNone(UsageAlerts.attention(self.profile(50), now=NOW))
        self.assertEqual(UsageAlerts.attention(self.profile(93), now=NOW)['short'], '한도 93%')
        full = UsageAlerts.attention(self.profile(100), {'due_at': self.reset + 90}, now=NOW)
        self.assertEqual(full['short'], '한도 도달')
        self.assertIn('자동 이어하기', full['detail'])
        self.assertEqual(UsageAlerts.attention(self.profile(10), {'due_at': self.reset}, now=NOW)['level'], 'limit')


class AutoContinueSettingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name))
        self.profile = self.store.add_profile('Claude test', claude_settings={})

    def control(self):
        from control_center import ControlCenter
        control = ControlCenter.__new__(ControlCenter)
        control.store = self.store
        control.instances = Mock()
        control.instances.prepare.return_value = {}
        control.instances.observe.return_value = {'status': 'running'}
        control.restarts = Mock()
        return control

    def test_the_setting_is_kept_outside_claude_settings_and_needs_no_restart(self):
        control = self.control()
        before = self.store.profile(self.profile['id'])['claude_settings']
        result = control.dispatch('claude.settings', {'profile_id': self.profile['id'], **before,
                                                      'auto_continue_after_limit': False})
        saved = self.store.profile(self.profile['id'])
        self.assertEqual((saved['claude_auto_continue'], saved['claude_settings']), (False, before))
        self.assertEqual(result['state'], 'saved')
        control.restarts.schedule.assert_not_called()
        # Changing a CLI setting together still applies it as before.
        control.dispatch('claude.settings', {'profile_id': self.profile['id'], 'reasoning_effort': 'max',
                                             'auto_continue_after_limit': True})
        saved = self.store.profile(self.profile['id'])
        self.assertEqual((saved['claude_auto_continue'], saved['claude_settings']['reasoning_effort']), (True, 'max'))
        control.restarts.schedule.assert_called_once_with(self.profile['id'])
        with self.assertRaises(ValueError):
            control.dispatch('claude.settings', {'profile_id': self.profile['id'], 'auto_continue_after_limit': 'no'})


if __name__ == '__main__':
    unittest.main()
