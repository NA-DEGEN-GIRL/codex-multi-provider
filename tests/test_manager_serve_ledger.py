import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from uuid import uuid4

SCRIPT_ROOT = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPT_ROOT))
from manager_core.app_transport import RuntimeObserver
from manager_core.external_profile import ExternalProfile
from manager_core.runtime_proxy import managed_client_message
from manager_core import serve_ledger
from manager_core.serve_ledger import (LEDGER_FILE, LEDGER_OLD_FILE, PROVIDER_RETURN_MARKER, SETTINGS_FILE,
                                       ProviderReturn, ServeLedger, account_tag, lookup_provider_settings,
                                       thread_hash)

THREAD = '0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b'
PROFILE = '11111111-2222-4333-8444-555555555555'
PROMPT = 'PRIVATE-PROMPT-TEXT'


def usage(thread, total, last, turn='turn-1', window=258400):
    def breakdown(values):
        names = ('inputTokens', 'cachedInputTokens', 'outputTokens', 'reasoningOutputTokens')
        data = dict(zip(names, values))
        data['totalTokens'] = values[0] + values[2]
        return data
    return {'method': 'thread/tokenUsage/updated', 'params': {'threadId': thread, 'turnId': turn, 'tokenUsage': {
        'total': breakdown(total), 'last': breakdown(last), 'modelContextWindow': window}}}


def started(thread=THREAD, provider='openai', model='gpt-6-astra', effort='medium', tier=None, method_id=1):
    return {'id': method_id, 'result': {'thread': {'id': thread, 'preview': PROMPT}, 'model': model,
                                        'modelProvider': provider, 'serviceTier': tier, 'reasoningEffort': effort}}


class Clock:
    def __init__(self, value=1_790_000_000.0):
        self.value = value

    def __call__(self):
        return self.value


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.clock = Clock()

    def tearDown(self):
        self.temporary.cleanup()

    def ledger(self, directory=None, account='abcdef012345', **options):
        return ServeLedger(directory or self.root, PROFILE, account, clock=self.clock, start=False, **options)

    def rows(self, directory=None):
        path = (directory or self.root) / LEDGER_FILE
        return [json.loads(line) for line in path.read_text(encoding='ascii').splitlines()] if path.exists() else []

    def open_thread(self, ledger, method='thread/start', **values):
        ledger.consume('client', {'id': 1, 'method': method, 'params': {'threadId': THREAD} if method != 'thread/start' else {}})
        ledger.consume('server', started(**values))


class UsageRowTests(LedgerCase):
    def test_new_thread_rows_carry_settings_and_no_content(self):
        ledger = self.ledger()
        self.open_thread(ledger)
        ledger.consume('client', {'id': 2, 'method': 'turn/start', 'params': {
            'threadId': THREAD, 'input': [{'type': 'text', 'text': PROMPT}], 'effort': 'high', 'serviceTier': 'priority'}})
        ledger.consume('server', {'method': 'turn/started', 'params': {'threadId': THREAD, 'turn': {'id': 'turn-1'}}})
        ledger.consume('server', usage(THREAD, (1000, 0, 50, 10), (1000, 0, 50, 10)))
        ledger.consume('server', usage(THREAD, (2100, 900, 90, 20), (1100, 900, 40, 10)))
        ledger.flush()
        rows = self.rows()
        self.assertEqual([row['input'] for row in rows], [1000, 1100])
        self.assertEqual(rows[1]['cached'], 900)
        row = rows[0]
        self.assertEqual((row['family'], row['provider'], row['model'], row['effort'], row['tier']),
                         ('openai', 'openai', 'gpt-6-astra', 'high', 'priority'))
        self.assertEqual(row['thread'], thread_hash(THREAD))
        self.assertEqual(row['account'], 'abcdef012345')
        self.assertEqual(row['window'], 258400)
        self.assertEqual(set(row), {'v', 'ts', 'profile', 'account', 'family', 'provider', 'model', 'effort', 'tier',
                                    'thread', 'turn', 'input', 'cached', 'output', 'reasoning', 'kind', 'window'})
        text = (self.root / LEDGER_FILE).read_text(encoding='ascii') + (self.root / SETTINGS_FILE).read_text(encoding='ascii')
        self.assertNotIn(PROMPT, text)
        self.assertNotIn(THREAD, text)
        self.assertNotIn('turn-1', text)

    def test_restored_snapshot_synthetic_estimate_and_reemission_are_not_requests(self):
        ledger = self.ledger()
        self.open_thread(ledger, method='thread/resume')
        # Replay of the persisted snapshot right after the resume response.
        ledger.consume('server', usage(THREAD, (50_000, 40_000, 900, 100), (1000, 900, 20, 5)))
        # Provider switch / compaction estimate: last input is zero.
        ledger.consume('server', usage(THREAD, (50_000, 40_000, 900, 100), (0, 0, 0, 0)))
        ledger.consume('server', usage(THREAD, (51_500, 41_000, 950, 110), (1500, 1000, 50, 10)))
        # Rate-limit update re-emits the unchanged info.
        ledger.consume('server', usage(THREAD, (51_500, 41_000, 950, 110), (1500, 1000, 50, 10)))
        ledger.flush()
        self.assertEqual([row['input'] for row in self.rows()], [1500])
        self.assertEqual(ledger.status()['skipped_usage_events'], 3)

    def test_shared_history_reload_replays_another_profiles_totals_without_a_row(self):
        ledger = self.ledger()
        self.open_thread(ledger)
        ledger.consume('server', usage(THREAD, (1000, 0, 5, 0), (1000, 0, 5, 0)))
        # Another profile served two turns; the in-place reload recomputes an
        # estimate (last input 0) over the restored, larger totals ...
        ledger.consume('server', usage(THREAD, (90_000, 70_000, 400, 0), (0, 0, 0, 0)))
        # ... and a later reopen replays that profile's last request.
        ledger.consume('client', {'id': 7, 'method': 'thread/resume', 'params': {'threadId': THREAD}})
        ledger.consume('server', started(method_id=7))
        ledger.consume('server', usage(THREAD, (140_000, 120_000, 800, 0), (50_000, 49_000, 400, 0)))
        ledger.consume('server', {'method': 'turn/started', 'params': {'threadId': THREAD, 'turn': {'id': 'turn-9'}}})
        ledger.consume('server', usage(THREAD, (141_200, 120_000, 810, 0), (1200, 0, 10, 0), turn='turn-9'))
        # A resume without a replay (excludeTurns) must not swallow the next request.
        ledger.consume('client', {'id': 8, 'method': 'thread/resume', 'params': {'threadId': THREAD}})
        ledger.consume('server', started(method_id=8))
        ledger.consume('server', {'method': 'turn/started', 'params': {'threadId': THREAD, 'turn': {'id': 'turn-10'}}})
        ledger.consume('server', usage(THREAD, (142_500, 121_000, 820, 0), (1300, 1000, 10, 0), turn='turn-10'))
        ledger.flush()
        rows = self.rows()
        self.assertEqual([row['input'] for row in rows], [1000, 1200, 1300])
        self.assertTrue(all('unlogged_input' not in row for row in rows))

    def test_unknown_baseline_waits_for_the_first_total(self):
        ledger = self.ledger()
        # A sub-agent thread this proxy never opened: the first event only sets
        # the baseline, so a restored total cannot become a false request row.
        child = '0199a1b2-0000-7000-8000-000000000001'
        ledger.consume('server', {'method': 'thread/started', 'params': {'thread': {
            'id': child, 'modelProvider': 'openai', 'model': 'gpt-6-astra-mini', 'reasoningEffort': 'low'}}})
        ledger.consume('server', usage(child, (3000, 0, 10, 0), (3000, 0, 10, 0)))
        ledger.consume('server', usage(child, (7000, 2500, 30, 0), (4000, 2500, 20, 0)))
        ledger.flush()
        rows = self.rows()
        self.assertEqual([(row['input'], row['model'], row['effort']) for row in rows], [(4000, 'gpt-6-astra-mini', 'low')])

    def test_unlogged_input_counts_only_gaps_within_one_turn(self):
        ledger = self.ledger()
        self.open_thread(ledger)
        ledger.consume('server', usage(THREAD, (1000, 0, 5, 0), (1000, 0, 5, 0), turn='turn-1'))
        # Within one turn two responses reached one event: the gap is ours.
        ledger.consume('server', usage(THREAD, (3300, 900, 15, 0), (1200, 900, 5, 0), turn='turn-1'))
        # Another profile served three requests; this profile's next turn
        # reloaded the shared history in place, which re-seeds the totals
        # without any token event. That gap is not this profile's.
        ledger.consume('server', usage(THREAD, (124_500, 1000, 20, 0), (1200, 1000, 5, 0), turn='turn-2'))
        ledger.flush()
        rows = self.rows()
        self.assertEqual([row['input'] for row in rows], [1000, 1200, 1200])
        self.assertEqual([row.get('unlogged_input') for row in rows], [None, 1100, None])

    def test_sub_agent_rows_are_marked(self):
        ledger = self.ledger()
        child = '0199a1b2-0000-7000-8000-000000000002'
        ledger.consume('server', {'method': 'thread/started', 'params': {'thread': {
            'id': child, 'parentThreadId': THREAD, 'modelProvider': 'openai', 'model': 'gpt-6-astra-mini'}}})
        ledger.consume('server', usage(child, (3000, 0, 10, 0), (3000, 0, 10, 0)))
        ledger.consume('server', usage(child, (7000, 2500, 30, 0), (4000, 2500, 20, 0)))
        self.open_thread(ledger)
        ledger.consume('server', usage(THREAD, (1000, 0, 5, 0), (1000, 0, 5, 0)))
        ledger.flush()
        self.assertEqual([(row['input'], row.get('sub')) for row in self.rows()], [(4000, 1), (1000, None)])

    def test_turn_tier_and_settings_notifications(self):
        ledger = self.ledger()
        self.open_thread(ledger, tier='priority')
        ledger.consume('client', {'id': 3, 'method': 'turn/start', 'params': {'threadId': THREAD, 'serviceTierForTurn': 'default'}})
        ledger.consume('server', usage(THREAD, (1000, 0, 5, 0), (1000, 0, 5, 0)))
        ledger.consume('server', {'method': 'turn/completed', 'params': {'threadId': THREAD, 'turn': {'id': 'turn-1', 'status': 'completed'}}})
        ledger.consume('server', {'method': 'thread/settings/updated', 'params': {'threadId': THREAD, 'threadSettings': {
            'model': 'gpt-6-astra', 'modelProvider': 'openai', 'effort': 'xhigh', 'serviceTier': 'priority'}}})
        ledger.consume('server', usage(THREAD, (2200, 900, 10, 0), (1200, 900, 5, 0), turn='turn-2'))
        ledger.consume('client', {'id': 4, 'method': 'turn/start', 'params': {'threadId': THREAD, 'collaborationMode': {
            'settings': {'model': 'gpt-6-astra', 'reasoning_effort': 'low'}}, 'serviceTier': None}})
        ledger.consume('server', usage(THREAD, (3500, 2000, 15, 0), (1300, 1100, 5, 0), turn='turn-3'))
        ledger.flush()
        self.assertEqual([(row['tier'], row['effort']) for row in self.rows()],
                         [(None, 'medium'), ('priority', 'xhigh'), (None, 'low')])
        self.assertNotEqual(self.rows()[0]['turn'], self.rows()[1]['turn'])

    def test_raw_response_rows_replace_usage_rows(self):
        ledger = self.ledger()
        self.open_thread(ledger)
        raw = {'inputTokens': 1000, 'cachedInputTokens': 0, 'cacheWriteInputTokens': 0, 'outputTokens': 20,
               'reasoningOutputTokens': 5, 'totalTokens': 1020}
        ledger.consume('server', {'method': 'rawResponse/completed', 'params': {
            'threadId': THREAD, 'turnId': 't', 'responseId': 'resp_1', 'usage': raw}})
        ledger.consume('server', usage(THREAD, (1000, 0, 20, 5), (1000, 0, 20, 5)))
        ledger.flush()
        rows = self.rows()
        self.assertEqual([(row['kind'], row['input']) for row in rows], [('raw', 1000)])
        self.assertNotIn('resp_1', (self.root / LEDGER_FILE).read_text(encoding='ascii'))

    def test_rotation_keeps_one_previous_file_and_bounded_size(self):
        ledger = self.ledger(max_bytes=4096)
        self.open_thread(ledger)
        total = 0
        for index in range(60):
            total += 1000 + index
            ledger.consume('server', usage(THREAD, (total, 0, 0, 0), (1000 + index, 0, 0, 0)))
            ledger.flush()
        self.assertTrue((self.root / LEDGER_OLD_FILE).exists())
        self.assertLessEqual((self.root / LEDGER_FILE).stat().st_size, 4096)
        self.assertLessEqual((self.root / LEDGER_OLD_FILE).stat().st_size, 4096)
        self.assertEqual(self.rows()[-1]['input'], 1059)
        self.assertEqual(ledger.status()['rows'], 60)

    def test_failed_rotation_never_lets_the_file_pass_twice_the_limit(self):
        ledger = self.ledger(max_bytes=2048)
        self.open_thread(ledger)
        total = 0
        with mock.patch.object(serve_ledger.os, 'replace', side_effect=PermissionError('held by a reader')):
            for index in range(40):
                total += 1000 + index
                ledger.consume('server', usage(THREAD, (total, 0, 0, 0), (1000 + index, 0, 0, 0)))
                ledger.flush()
        size = (self.root / LEDGER_FILE).stat().st_size
        self.assertGreater(size, 2048)
        self.assertLessEqual(size, 2 * 2048)
        self.assertFalse((self.root / LEDGER_OLD_FILE).exists())
        status = ledger.status()
        self.assertGreater(status['dropped'], 0)
        self.assertEqual(status['rows'] + status['dropped'], 40)
        # The reader let go: the next batch rotates and appends again.
        total += 2000
        ledger.consume('server', usage(THREAD, (total, 0, 0, 0), (2000, 0, 0, 0)))
        ledger.flush()
        self.assertTrue((self.root / LEDGER_OLD_FILE).exists())
        self.assertEqual([row['input'] for row in self.rows()], [2000])

    def test_settings_keep_the_newest_threads_and_their_account(self):
        with mock.patch.object(serve_ledger, 'MAX_SETTINGS_THREADS', 3):
            ledger = self.ledger()
            threads = [f'0199a1b2-0000-7000-8000-00000000001{index}' for index in range(6)]

            def start(index):
                self.clock.value += 10
                ledger.consume('client', {'id': 100 + index, 'method': 'thread/start', 'params': {}})
                ledger.consume('server', started(thread=threads[index], method_id=100 + index))
                ledger.consume('server', usage(threads[index], (1000, 0, 5, 0), (1000, 0, 5, 0)))

            for index in range(5):
                start(index)
            # Thread 2, the oldest one kept, is used again: now it is the newest,
            # so the next new thread pushes out thread 3 instead.
            self.clock.value += 10
            ledger.consume('server', usage(threads[2], (2500, 900, 9, 0), (1500, 900, 4, 0)))
            start(5)
            ledger.flush()
            stored = json.loads((self.root / SETTINGS_FILE).read_text(encoding='ascii'))['threads']
            # Update order, newest last, in memory and in the file.
            self.assertEqual(list(stored), [thread_hash(threads[index]) for index in (4, 2, 5)])
            self.assertEqual(stored[thread_hash(threads[2])]['openai']['account'], 'abcdef012345')
            reloaded = self.ledger()
            self.assertEqual(list(reloaded.settings), list(stored))
            # A longer file (an older, larger limit) keeps its newest threads.
            longer = {thread_hash(thread): {'openai': {'model': 'm', 'effort': 'low', 'at': 1.0}} for thread in threads}
            (self.root / SETTINGS_FILE).write_text(json.dumps({'version': 1, 'threads': longer}), encoding='ascii')
            self.assertEqual(list(self.ledger().settings), [thread_hash(thread) for thread in threads[3:]])

    def test_background_writer_and_observer_integration(self):
        ledger = ServeLedger(self.root, PROFILE, None, clock=self.clock)
        try:
            observer = RuntimeObserver(PROFILE, usage=ledger)
            observer.consume('client', {'id': 1, 'method': 'thread/start', 'params': {}})
            observer.consume('server', started())
            observer.consume('server', usage(THREAD, (1000, 0, 5, 0), (1000, 0, 5, 0)))
            # Malformed traffic never raises out of the observer.
            observer.consume('server', {'method': 'thread/tokenUsage/updated', 'params': {'threadId': THREAD, 'tokenUsage': {'total': 'x'}}})
            observer.consume('server', usage(THREAD, (2**70, 0, 0, 0), (1, 0, 0, 0)))
        finally:
            ledger.close()
        self.assertEqual([row['input'] for row in self.rows()], [1000])
        self.assertIsNone(self.rows()[0]['account'])
        self.assertTrue(observer.snapshot()['stream_complete'])

    def test_account_tag_uses_fingerprint_or_external_provider(self):
        self.assertEqual(account_tag({'CODEX_MANAGER_EXPECTED_ACCOUNT_FINGERPRINT': 'ABCDEF0123456789' * 4}), 'abcdef012345')
        self.assertEqual(account_tag({'CODEX_MANAGER_PRIMARY_MODEL': json.dumps({'model_provider': 'cc_ds', 'model': 'm'})}),
                         'provider:cc_ds')
        self.assertIsNone(account_tag({'CODEX_MANAGER_EXPECTED_ACCOUNT_FINGERPRINT': 'not-hex-value-here'}))

    def test_thread_hash_matches_the_shell_vector(self):
        # manager/Shell ProfileCacheLine.ThreadHash must produce the same digest.
        self.assertEqual(thread_hash(THREAD), 'b912d61979d18e95338694ec')
        self.assertEqual(thread_hash(THREAD.upper()), thread_hash(THREAD))


class ProviderReturnTests(LedgerCase):
    def seed(self, directory, effort, tier, at, model='gpt-6-astra', provider='openai', account='abcdef012345'):
        ledger = self.ledger(directory, account=account)
        self.clock.value = at
        self.open_thread(ledger, provider=provider, model=model, effort=effort, tier=tier)
        ledger.consume('server', usage(THREAD, (1000, 0, 5, 0), (1000, 0, 5, 0)))
        ledger.flush()
        return ledger

    def resume(self, **extra):
        params = {'threadId': THREAD, 'model': 'gpt-6-astra', 'modelProvider': 'openai',
                  'config': {'model_provider': 'openai', 'model': 'gpt-6-astra', 'model_reasoning_effort': 'medium'},
                  PROVIDER_RETURN_MARKER: True, **extra}
        return {'id': 9, 'method': 'thread/resume', 'params': params}

    def test_newest_settings_across_profiles_restore_effort_and_tier(self):
        instances = self.root / 'instances'
        self.seed(instances / 'a', 'high', 'priority', 1_790_000_000.0)
        self.seed(instances / 'b', 'xhigh', None, 1_790_000_100.0)
        self.seed(instances / 'c', 'low', None, 1_790_000_200.0, model='deepseek-flash', provider='cc_ds')
        self.assertEqual(lookup_provider_settings(instances, THREAD, 'openai')['effort'], 'xhigh')
        restore = ProviderReturn(instances, account='abcdef012345')
        original = self.resume()
        message = restore(original)
        params = message['params']
        self.assertNotIn(PROVIDER_RETURN_MARKER, params)
        self.assertIn(PROVIDER_RETURN_MARKER, original['params'], 'the caller object is not modified')
        self.assertEqual(params['config']['model_reasoning_effort'], 'xhigh')
        self.assertIn('serviceTier', params)
        self.assertIsNone(params['serviceTier'], 'standard tier is restored as explicit standard routing')
        self.assertEqual(restore.restored, 1)

    def test_tier_is_restored_only_from_the_same_account(self):
        instances = self.root / 'instances'
        # Fast profile 01 (account X) ran the thread on GPT, then DeepSeek did.
        self.seed(instances / 'p01', 'high', 'priority', 1_790_000_000.0, account='aaaaaaaaaaaa')
        self.seed(instances / 'ds', 'max', None, 1_790_000_100.0, model='deepseek-flash', provider='cc_ds',
                  account='provider:cc_ds')
        # Standard profile 06 (account Y) reopens it: effort follows the
        # thread, the tier stays 06's own.
        other = ProviderReturn(instances, account='bbbbbbbbbbbb')(self.resume())['params']
        self.assertEqual(other['config']['model_reasoning_effort'], 'high')
        self.assertNotIn('serviceTier', other)
        # Y served the thread before, on standard: that is the tier to keep,
        # while the newer effort still comes from X's entry.
        self.seed(instances / 'p06', 'low', None, 1_789_999_000.0, account='bbbbbbbbbbbb')
        own = ProviderReturn(instances, account='bbbbbbbbbbbb')(self.resume())['params']
        self.assertEqual(own['config']['model_reasoning_effort'], 'high')
        self.assertIn('serviceTier', own)
        self.assertIsNone(own['serviceTier'])
        # Another profile on account X gets X's Fast tier back.
        same = ProviderReturn(instances, account='aaaaaaaaaaaa')(self.resume())['params']
        self.assertEqual(same['serviceTier'], 'priority')
        # A profile without an account tag trusts only its own ledger.
        unknown = ProviderReturn(instances)(self.resume())['params']
        self.assertNotIn('serviceTier', unknown)
        # Entries written before account tags existed restore effort only.
        legacy = instances / 'legacy'
        legacy.mkdir()
        (legacy / SETTINGS_FILE).write_text(json.dumps({'version': 1, 'threads': {thread_hash(THREAD): {
            'openai': {'model': 'gpt-6-astra', 'effort': 'minimal', 'tier': 'priority', 'at': 1_790_000_500.0}}}}),
            encoding='ascii')
        old_entry = ProviderReturn(instances, account='bbbbbbbbbbbb')(self.resume())['params']
        self.assertEqual(old_entry['config']['model_reasoning_effort'], 'minimal')
        self.assertIsNone(old_entry['serviceTier'])

    def test_claude_ultracode_is_restored_after_another_provider(self):
        instances = self.root / 'instances'
        self.seed(instances / 'claude', 'ultracode', None, 1_790_000_000.0, model='cc-opus',
                  provider='claude_code', account='provider:claude_code')
        self.seed(instances / 'gpt', 'high', None, 1_790_000_100.0)
        params = ProviderReturn(instances)(self.resume(
            model='cc-opus', modelProvider='claude_code',
            config={'model_provider': 'claude_code', 'model': 'cc-opus', 'model_reasoning_effort': 'xhigh'}))['params']
        self.assertEqual(params['config']['model_reasoning_effort'], 'ultracode')

    def test_lookup_reads_one_thread_and_only_changed_files(self):
        instances = self.root / 'instances'
        self.seed(instances / 'a', 'high', None, 1_790_000_000.0)
        self.seed(instances / 'b', 'xhigh', None, 1_790_000_100.0)
        # This profile's own directory: its ledger's memory is authoritative.
        (instances / 'self').mkdir()
        (instances / 'self' / SETTINGS_FILE).write_text(json.dumps({'version': 1, 'threads': {thread_hash(THREAD): {
            'openai': {'model': 'gpt-6-astra', 'effort': 'none', 'tier': None, 'at': 9e9}}}}), encoding='ascii')
        restore = ProviderReturn(instances, account='abcdef012345', exclude='self')
        self.assertEqual(restore.settings(THREAD, 'openai')[0]['effort'], 'xhigh')
        self.assertEqual(restore.lookup.reads, 2)
        restore.settings(THREAD, 'openai')
        self.assertEqual(restore.lookup.reads, 2, 'unchanged files are not read again')
        self.seed(instances / 'a', 'low', None, 1_790_000_200.0)
        self.assertEqual(restore.settings(THREAD, 'openai')[0]['effort'], 'low')
        self.assertEqual(restore.lookup.reads, 3)
        # Only the thread's own object is decoded; the same digest as a key
        # inside another thread's object is not taken for the thread.
        key = thread_hash(THREAD)
        text = json.dumps({'version': 1, 'threads': {'f' * 24: {key: {'model': 'x', 'effort': 'low', 'at': 1}},
                                                     key: {'openai': {'model': 'm', 'effort': 'high', 'at': 2}}}},
                          separators=(',', ':'))
        self.assertEqual(serve_ledger._thread_settings(text, key), {'openai': {'model': 'm', 'effort': 'high', 'at': 2}})
        only_nested = text.replace('"' + key + '":{"openai', '"' + 'e' * 24 + '":{"openai')
        self.assertEqual(serve_ledger._thread_settings(only_nested, key), {})
        self.assertEqual(serve_ledger._thread_settings(text[:-12], key), {})

    def test_no_restore_for_other_model_explicit_tier_or_unmarked_requests(self):
        instances = self.root / 'instances'
        self.seed(instances / 'a', 'high', 'priority', 1_790_000_000.0)
        restore = ProviderReturn(instances, account='abcdef012345')
        other = restore(self.resume(model='gpt-other', config={'model': 'gpt-other', 'model_reasoning_effort': 'medium'}))
        self.assertEqual(other['params']['config']['model_reasoning_effort'], 'medium')
        self.assertNotIn('serviceTier', other['params'])
        self.assertNotIn(PROVIDER_RETURN_MARKER, other['params'])
        explicit = restore(self.resume(serviceTier='flex'))
        self.assertEqual(explicit['params']['serviceTier'], 'flex')
        self.assertEqual(explicit['params']['config']['model_reasoning_effort'], 'high')
        unmarked = {'id': 3, 'method': 'thread/resume', 'params': {'threadId': THREAD, 'model': 'gpt-6-astra'}}
        self.assertIs(restore(unmarked), unmarked)
        turn = {'id': 4, 'method': 'turn/start', 'params': {'threadId': THREAD, PROVIDER_RETURN_MARKER: True}}
        self.assertIs(restore(turn), turn)
        # Without any stored settings the adapter's profile defaults stay.
        fresh = ProviderReturn(self.root / 'empty')
        kept = fresh(self.resume())
        self.assertEqual(kept['params']['config']['model_reasoning_effort'], 'medium')
        self.assertNotIn('serviceTier', kept['params'])

    def test_external_binding_still_validates_a_restored_effort(self):
        instances = self.root / 'instances'
        self.seed(instances / 'd', 'xhigh', None, 1_790_000_000.0, model='deepseek-flash', provider='cc_ds')
        external = ExternalProfile({'CODEX_MANAGER_PRIMARY_MODEL': json.dumps({
            'model': 'deepseek-flash', 'model_provider': 'cc_ds', 'reasoning_effort': 'none',
            'supported_reasoning_efforts': ['none', 'low', 'high', 'max'],
            'effort_aliases': {'minimal': 'low', 'medium': 'high', 'xhigh': 'high', 'ultra': 'max'}})})
        message = {'id': 5, 'method': 'thread/resume', 'params': {
            'threadId': THREAD, 'model': 'deepseek-flash', 'modelProvider': 'cc_ds', PROVIDER_RETURN_MARKER: True,
            'config': {'model_provider': 'cc_ds', 'model': 'deepseek-flash', 'model_reasoning_effort': 'none'}}}
        result = managed_client_message(message, external, None, ProviderReturn(instances))
        self.assertEqual(result['params']['config']['model_reasoning_effort'], 'high')
        self.assertNotIn(PROVIDER_RETURN_MARKER, result['params'])

    def test_local_ledger_settings_are_consulted_before_they_are_written(self):
        ledger = self.ledger(self.root / 'instances' / 'x')
        self.open_thread(ledger, effort='low')
        ledger.consume('server', usage(THREAD, (1000, 0, 5, 0), (1000, 0, 5, 0)))
        restore = ProviderReturn(None, ledger)
        self.assertEqual(restore(self.resume())['params']['config']['model_reasoning_effort'], 'low')


class ProxyLedgerTests(unittest.TestCase):
    def test_proxy_writes_rows_and_restores_a_marked_provider_return(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = str(uuid4())
            instance = root / 'instances' / profile
            instance.mkdir(parents=True)
            other = root / 'instances' / str(uuid4())
            other.mkdir()
            second = '0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5c'
            # Another profile's older entries: one for the thread this proxy
            # serves itself (its own newer entry must win) and one for a
            # thread only the other profile (another account) served.
            (other / SETTINGS_FILE).write_text(json.dumps({'version': 1, 'threads': {
                thread_hash(THREAD): {'openai': {'model': 'gpt-6-astra', 'effort': 'high', 'tier': 'priority', 'at': 1.0,
                                                 'account': 'ffffffffffff'}},
                thread_hash(second): {'openai': {'model': 'gpt-6-astra', 'effort': 'xhigh', 'tier': 'priority', 'at': 1.0,
                                                 'account': 'ffffffffffff'}}}}), encoding='ascii')
            fake = root / 'runtime.py'
            fake.write_text('''import sys,json
for line in sys.stdin:
    m=json.loads(line)
    if m.get('method')=='thread/start':
        print(json.dumps({'id':m['id'],'result':{'thread':{'id':%r},'model':'gpt-6-astra','modelProvider':'openai','serviceTier':None,'reasoningEffort':'medium'}}),flush=True)
        for total,last in ((1000,1000),(2500,1500)):
            usage={'inputTokens':last,'cachedInputTokens':0,'outputTokens':1,'reasoningOutputTokens':0,'totalTokens':last+1}
            tusage=dict(usage,inputTokens=total,totalTokens=total+1)
            print(json.dumps({'method':'thread/tokenUsage/updated','params':{'threadId':%r,'turnId':'t','tokenUsage':{'total':tusage,'last':usage,'modelContextWindow':1000}}}),flush=True)
    elif 'id' in m:
        print(json.dumps({'id':m['id'],'result':{'received':m.get('params')}}),flush=True)
''' % (THREAD, THREAD), encoding='utf-8')
            bootstrap = root / 'bootstrap.py'
            bootstrap.write_text("import sys,os\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1])\n"
                                 "from manager_core.runtime_proxy import proxy\n"
                                 "raise SystemExit(proxy(Path(sys.executable),[sys.argv[2]],Path(sys.argv[3]),sys.argv[4],dict(os.environ)))\n",
                                 encoding='utf-8')
            environment = {key: value for key, value in os.environ.items() if not key.upper().startswith('CODEX_')}
            environment.update(CODEX_HOME=str(root), CODEX_MANAGER_REAL_RUNTIME=sys.executable)
            process = subprocess.Popen([sys.executable, str(bootstrap), str(SCRIPT_ROOT), str(fake),
                                        str(instance / 'runtime-state.json'), profile],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, encoding='utf-8', env=environment)
            lines: queue.Queue = queue.Queue()
            threading.Thread(target=lambda: [lines.put(line) for line in process.stdout], daemon=True).start()
            errors = []
            threading.Thread(target=lambda: errors.append(process.stderr.read()), daemon=True).start()

            def send(message):
                process.stdin.write(json.dumps(message) + '\n')
                process.stdin.flush()

            def until(predicate):
                while True:
                    try:
                        item = json.loads(lines.get(timeout=20))
                    except queue.Empty:
                        process.kill()
                        self.fail('The proxy did not answer in time.')
                    if predicate(item):
                        return item

            def resume(identifier, thread):
                send({'id': identifier, 'method': 'thread/resume', 'params': {
                    'threadId': thread, 'model': 'gpt-6-astra', 'modelProvider': 'openai', PROVIDER_RETURN_MARKER: True,
                    'config': {'model_provider': 'openai', 'model': 'gpt-6-astra', 'model_reasoning_effort': 'low'}}})
                return until(lambda item: item.get('id') == identifier)['result']['received']

            try:
                send({'id': 1, 'method': 'initialize', 'params': {}})
                send({'id': 2, 'method': 'thread/start', 'params': {}})
                # The proxy relays a runtime frame only after its ledger saw it,
                # so this profile's own entry exists before the resume below.
                until(lambda item: item.get('method') == 'thread/tokenUsage/updated'
                      and item['params']['tokenUsage']['total']['inputTokens'] == 2500)
                own = resume(3, THREAD)
                elsewhere = resume(4, second)
            finally:
                process.stdin.close()
                process.wait(timeout=20)
            self.assertEqual(process.returncode, 0, errors)
            for received in (own, elsewhere):
                self.assertNotIn(PROVIDER_RETURN_MARKER, received)
            # The newest entry wins: this profile's own 'medium' over the other's 'high'.
            self.assertEqual(own['config']['model_reasoning_effort'], 'medium')
            self.assertIn('serviceTier', own)
            self.assertIsNone(own['serviceTier'], "this profile's own standard tier is restored")
            # Only the other profile served this thread: its effort, never its tier.
            self.assertEqual(elsewhere['config']['model_reasoning_effort'], 'xhigh')
            self.assertNotIn('serviceTier', elsewhere)
            rows = [json.loads(line) for line in (instance / LEDGER_FILE).read_text(encoding='ascii').splitlines()]
            self.assertEqual([row['input'] for row in rows], [1000, 1500])
            self.assertTrue(all(row['profile'] == profile for row in rows))
            state = json.loads((instance / 'runtime-state.json').read_text(encoding='utf-8'))
            self.assertEqual(state['serve_ledger']['rows'], 2)
            self.assertEqual(state['serve_ledger']['provider_returns_restored'], 2)


if __name__ == '__main__':
    unittest.main()
