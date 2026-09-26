import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

SCRIPT_ROOT = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPT_ROOT))
from manager_core import cache_warmth
from manager_core.cache_warmth import CacheWarmth, format_cost, korean_tokens, retention, KINDS
from manager_core.serve_ledger import LEDGER_FILE, LEDGER_OLD_FILE, thread_hash

THREAD = '0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b'
NOW = 1_790_000_000.0
A, B, C, D = ('aaaaaaaa-0000-4000-8000-000000000001', 'bbbbbbbb-0000-4000-8000-000000000002',
              'cccccccc-0000-4000-8000-000000000003', 'dddddddd-0000-4000-8000-000000000004')


class Clock:
    def __init__(self):
        self.value = NOW

    def __call__(self):
        return self.value


def profile(identifier, alias, fingerprint=None, external=False, remaining=None):
    data = {'id': identifier, 'alias': alias, 'auth_mode': 'external' if external else 'chatgpt'}
    if fingerprint:
        data['account_fingerprint'] = fingerprint
    if remaining is not None:
        data['usage'] = {'windows': [{'label': '주간', 'remaining_percent': remaining}]}
    return data


class WarmthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.instances = Path(self.temporary.name) / 'instances'
        self.clock = Clock()
        self.warmth = CacheWarmth(self.instances, Path(self.temporary.name) / 'cache-prices.json', clock=self.clock)
        self.profiles = [profile(A, '01', 'a' * 64, remaining=41), profile(B, '03', 'b' * 64),
                         profile(C, '05', 'a' * 64), profile(D, 'DeepSeek', external=True)]
        self.families = {D: ('cc_ds', 'deepseek-flash')}

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, owner, rows, old=False):
        directory = self.instances / owner
        directory.mkdir(parents=True, exist_ok=True)
        with open(directory / (LEDGER_OLD_FILE if old else LEDGER_FILE), 'ab') as stream:
            for row in rows:
                stream.write(json.dumps(row).encode('ascii') + b'\n')

    def row(self, owner, ago, value, account, family='openai', model='gpt-6-astra', tier=None, output=2000, window=258400):
        return {'v': 1, 'ts': NOW - ago, 'profile': owner, 'account': account, 'family': family,
                'provider': family, 'model': model, 'effort': 'high', 'tier': tier, 'thread': thread_hash(THREAD),
                'input': value, 'cached': 0, 'output': output, 'reasoning': 0, 'kind': 'usage', 'window': window}

    def summary(self):
        return self.warmth.thread(THREAD, self.profiles, self.families)

    def test_warm_account_cold_account_notice_and_shared_account(self):
        self.write(A, [self.row(A, 600, 140_000, 'a' * 12), self.row(A, 300, 150_000, 'a' * 12)])
        result = self.summary()
        self.assertEqual(result['context'], 152_000)
        warm, cold, shared = (result['profiles'][key] for key in (A, B, C))
        self.assertEqual((warm['state'], warm['minutes_left'], warm['tone']), ('warm', 25, 'ready'))
        self.assertEqual(warm['warm'], int(150_000 * .83))
        expected = ((152_000 - int(150_000 * .83)) * 250 + int(150_000 * .83) * 25) / 1e6
        self.assertAlmostEqual(warm['cost'], round(expected, 4))
        self.assertEqual(warm['line'], f'이 작업 캐시 · 약 25분 남음 · 첫 요청 ≈ {expected:.0f} 크레딧')
        # Same account fingerprint: the same prompt cache, so also warm.
        self.assertEqual(shared['state'], 'warm')
        self.assertEqual((cold['state'], cold['reason'], cold['tone']), ('cold', 'not_served', 'warning'))
        self.assertEqual(cold['line'], '이 작업 캐시 없음 · 첫 요청 ≈ 38 크레딧 (15.2만 token)')
        self.assertEqual(cold['notice'], '01에서 12.4만 token이 5분 전에 캐시됐습니다. '
                                         '여기서 이어가면 첫 요청이 캐시 없이 처리됩니다(약 38 크레딧). 01 남은 사용량 41%.')
        self.assertEqual(cold['warm_profile'], A)
        # The API profile has another family: no notice about GPT accounts.
        self.assertNotIn('notice', result['profiles'][D])

    def test_small_context_has_no_notice(self):
        self.write(A, [self.row(A, 60, 40_000, 'a' * 12)])
        cold = self.summary()['profiles'][B]
        self.assertEqual(cold['tone'], 'warning')
        self.assertNotIn('notice', cold)

    def test_compaction_elsewhere_breaks_the_prefix(self):
        self.write(A, [self.row(A, 900, 200_000, 'a' * 12)])
        self.write(B, [self.row(B, 600, 205_000, 'b' * 12), self.row(B, 300, 48_000, 'b' * 12)])
        result = self.summary()['profiles']
        self.assertEqual((result[A]['state'], result[A]['reason']), ('cold', 'compacted'))
        self.assertEqual(result[B]['state'], 'warm')

    def test_append_only_account_switch_keeps_the_earlier_account_warm(self):
        self.write(A, [self.row(A, 900, 150_000, 'a' * 12)])
        self.write(B, [self.row(B, 300, 160_000, 'b' * 12), self.row(B, 200, 168_000, 'b' * 12)])
        result = self.summary()['profiles']
        self.assertEqual(result[A]['state'], 'warm')
        self.assertEqual(result[A]['minutes_left'], 15)
        self.assertEqual(result[A]['warm'], int(150_000 * .83))

    def test_provider_detour_breaks_gpt_prefix_and_deepseek_uses_its_own_table(self):
        self.write(A, [self.row(A, 1200, 150_000, 'a' * 12)])
        self.write(D, [self.row(D, 600, 356_000, 'provider:cc_ds', family='cc_ds', model='deepseek-flash', window=1048576)])
        result = self.summary()['profiles']
        self.assertEqual((result[A]['state'], result[A]['reason']), ('cold', 'provider_changed'))
        # GPT's rebuild is bounded by its window, not DeepSeek's 358k context.
        self.assertEqual(result[A]['uncached'], int(258400 * .6))
        deepseek = result[D]
        self.assertEqual((deepseek['state'], deepseek['kind'], deepseek['unit']), ('warm', 'deepseek', 'usd'))
        self.assertEqual(deepseek['minutes_left'], 350)
        self.assertTrue(deepseek['assumed_price'])
        # A dollar figure from the built-in rate says it is an assumption.
        self.assertTrue(deepseek['line'].startswith('이 작업 캐시 · 약 350분 남음 · 첫 요청 ≈ $'))
        self.assertTrue(deepseek['line'].endswith(' (가정 단가)'), deepseek['line'])
        self.clock.value += 7 * 3600
        cold = self.summary()['profiles'][D]
        self.assertEqual(cold['state'], 'cold')
        self.assertRegex(cold['line'], r'^이 작업 캐시 없음 · 첫 요청 ≈ \$[0-9.]+ \([0-9.]+만 token, 가정 단가\)$')

    def test_retention_bands_and_fast_multiplier(self):
        self.write(A, [self.row(A, 45 * 60, 100_000, 'a' * 12, tier='priority')])
        partial = self.summary()['profiles'][A]
        self.assertEqual(partial['state'], 'partial')
        self.assertEqual(partial['warm'], 50_000)
        expected = ((102_000 - 50_000) * 250 + 50_000 * 25) / 1e6 * 2.5
        self.assertAlmostEqual(partial['cost'], round(expected, 4))
        self.assertTrue(partial['line'].startswith('이 작업 캐시 일부 · 첫 요청 ≈ '))
        self.clock.value = NOW + 6 * 3600
        # Beyond the 6 h report horizon the thread leaves the recent set; an
        # opened or selected (pinned) task is still described.
        self.assertNotIn(thread_hash(THREAD), self.warmth.summary(self.profiles, self.families)['threads'])
        self.assertEqual(self.summary()['profiles'][A]['reason'], 'expired')
        self.clock.value = NOW + 4.5 * 3600 - 45 * 60
        expired = self.summary()['profiles'][A]
        self.assertEqual((expired['state'], expired['reason'], expired['warm']), ('cold', 'expired', 0))
        self.assertEqual(retention(KINDS['gpt'], 30), .91)
        self.assertEqual(retention(KINDS['claude'], 301), 0.0)

    def test_incremental_reads_and_rotation_count_each_row_once(self):
        self.write(A, [self.row(A, 500, 10_000, 'a' * 12)])
        self.summary()
        self.write(A, [self.row(A, 400, 11_000, 'a' * 12)])
        self.summary()
        # Appended after the last read, then rotated before the next one: the
        # rest of the previous file is read before the new one.
        self.write(A, [self.row(A, 350, 11_500, 'a' * 12)])
        directory = self.instances / A
        os.replace(directory / LEDGER_FILE, directory / LEDGER_OLD_FILE)
        self.write(A, [self.row(A, 300, 12_000, 'a' * 12)])
        self.summary()
        # Partial line at the end is left for the next read.
        with open(directory / LEDGER_FILE, 'ab') as stream:
            stream.write(json.dumps(self.row(A, 200, 13_000, 'a' * 12)).encode('ascii')[:40])
        self.summary()
        rows = self.warmth.index.threads[thread_hash(THREAD)].rows
        self.assertEqual([row.input for row in rows], [10_000, 11_000, 11_500, 12_000])
        # A first look finds the short current file does not reach the report
        # horizon, so it also reads the rotated file.
        fresh = CacheWarmth(self.instances, clock=self.clock)
        fresh.summary(self.profiles, self.families)
        self.assertEqual([row.input for row in fresh.index.threads[thread_hash(THREAD)].rows],
                         [10_000, 11_000, 11_500, 12_000])

    def test_price_override_and_formatting(self):
        (Path(self.temporary.name) / 'cache-prices.json').write_text(json.dumps(
            {'deepseek': {'input': 0.5, 'cached': 0.05, 'unit': 'usd', 'warm_minutes': 30}}), encoding='utf-8')
        self.warmth._prices_checked = -1e9
        self.write(D, [self.row(D, 60, 200_000, 'provider:cc_ds', family='cc_ds', model='deepseek-flash', output=0)])
        deepseek = self.summary()['profiles'][D]
        self.assertEqual(deepseek['minutes_left'], 29)
        self.assertNotIn('assumed_price', deepseek)
        self.assertNotIn('가정', deepseek['line'])
        self.assertAlmostEqual(deepseek['cost'], round((200_000 - 193_600) * 0.5 / 1e6 + 193_600 * 0.05 / 1e6, 4))
        self.assertEqual(korean_tokens(152_000), '15.2만')
        self.assertEqual(korean_tokens(150_000), '15만')
        self.assertEqual(korean_tokens(8_400), '8,400')
        self.assertEqual(format_cost(0.4, 'credits'), '1 크레딧 미만')
        self.assertEqual(format_cost(0.004, 'usd'), '$0.01 미만')

    def test_rows_written_by_the_proxy_ledger_feed_the_estimate(self):
        from manager_core.serve_ledger import ServeLedger
        ledger = ServeLedger(self.instances / A, A, 'a' * 12, clock=lambda: NOW - 120, start=False)
        ledger.consume('client', {'id': 1, 'method': 'thread/start', 'params': {}})
        ledger.consume('server', {'id': 1, 'result': {'thread': {'id': THREAD}, 'model': 'gpt-6-astra',
                                                      'modelProvider': 'openai', 'serviceTier': 'priority', 'reasoningEffort': 'high'}})
        breakdown = {'inputTokens': 80_000, 'cachedInputTokens': 0, 'outputTokens': 500, 'reasoningOutputTokens': 0, 'totalTokens': 80_500}
        ledger.consume('server', {'method': 'thread/tokenUsage/updated', 'params': {'threadId': THREAD, 'turnId': 't', 'tokenUsage': {
            'total': breakdown, 'last': breakdown, 'modelContextWindow': 258400}}})
        ledger.flush()
        result = self.summary()
        self.assertEqual(result['context'], 80_500)
        warm, cold = result['profiles'][A], result['profiles'][B]
        self.assertEqual((warm['state'], warm['minutes_left'], warm['unit']), ('warm', 28, 'credits'))
        # Fast on the anchor request: the estimate carries the 2.5x multiplier.
        self.assertAlmostEqual(warm['cost'], round(((80_500 - 66_400) * 250 + 66_400 * 25) / 1e6 * 2.5, 4))
        self.assertIn('notice', cold)

    def test_a_long_stint_on_another_account_keeps_the_earlier_anchor(self):
        # 01 served the thread 8 minutes ago; then 03 (another account) ran a
        # 100-request turn on it, more rows than a thread keeps in its window.
        self.write(A, [self.row(A, 480, 150_000, 'a' * 12)])
        self.summary()
        stint = [self.row(B, 470 - index * 4, 152_000 + index * 800, 'b' * 12) for index in range(100)]
        self.write(B, stint)
        for warmth in (self.warmth, CacheWarmth(self.instances, clock=self.clock)):
            # Incremental reads and a first look at both ledgers agree.
            result = warmth.thread(THREAD, self.profiles, self.families)['profiles']
            self.assertEqual((result[A]['state'], result[A]['minutes_left']), ('warm', 22))
            self.assertEqual(result[A]['warm'], int(150_000 * .83))
            self.assertEqual(result[C]['state'], 'warm', 'the same account shares the anchor')
            self.assertEqual(result[B]['state'], 'warm')
            self.assertEqual(len(warmth.index.threads[thread_hash(THREAD)].rows), cache_warmth.MAX_ROWS_PER_THREAD)

    def test_breaks_that_left_the_window_still_break_an_older_anchor(self):
        self.write(A, [self.row(A, 900, 150_000, 'a' * 12)])
        # A compaction early in 03's long stint.
        stint = [self.row(B, 880, 160_000, 'b' * 12), self.row(B, 870, 40_000, 'b' * 12)]
        stint += [self.row(B, 860 - index * 5, 41_000 + index * 500, 'b' * 12) for index in range(100)]
        self.write(B, stint)
        result = self.summary()['profiles']
        self.assertEqual((result[A]['state'], result[A]['reason']), ('cold', 'compacted'))
        self.assertEqual(result[B]['state'], 'warm')

    def test_a_provider_detour_that_left_the_window_still_breaks_the_prefix(self):
        self.write(A, [self.row(A, 900, 150_000, 'a' * 12)])
        self.write(D, [self.row(D, 880, 200_000, 'provider:cc_ds', family='cc_ds', model='deepseek-flash', window=1048576)])
        self.write(B, [self.row(B, 860 - index * 5, 150_000 + index * 500, 'b' * 12) for index in range(100)])
        result = self.summary()['profiles']
        self.assertEqual((result[A]['state'], result[A]['reason']), ('cold', 'provider_changed'))
        self.assertEqual((result[D]['state'], result[D]['reason']), ('cold', 'provider_changed'))
        self.assertEqual(result[B]['state'], 'warm')

    def test_first_look_reads_back_only_to_the_report_horizon(self):
        older = '0199a1b2-c3d4-7e5f-8a9b-000000000009'
        rotated = '0199a1b2-c3d4-7e5f-8a9b-00000000000a'
        self.write(A, [dict(self.row(A, 9 * 3600, 90_000, 'a' * 12), thread=thread_hash(rotated))], old=True)
        self.write(A, [dict(self.row(A, 8 * 3600 - index, 80_000, 'a' * 12), thread=thread_hash(older)) for index in range(100)]
                   + [self.row(A, 600 - index, 100_000 + index, 'a' * 12) for index in range(20)])
        # B's current file was just rotated: its recent past is in the old file.
        self.write(B, [self.row(B, 2 * 3600, 90_000, 'b' * 12)], old=True)
        self.write(B, [dict(self.row(B, 60, 1_000, 'b' * 12), thread=thread_hash(older))])
        with mock.patch.object(cache_warmth, 'FIRST_BLOCK_BYTES', 2048):
            self.warmth.summary(self.profiles, self.families)
        threads = self.warmth.index.threads
        self.assertEqual(len(threads[thread_hash(THREAD)].rows), 21)
        # Blocks stop once a row is older than the horizon, and the rotated
        # file is not needed then.
        self.assertLess(len([row for row in threads[thread_hash(older)].rows if row.profile == A]), 20)
        self.assertNotIn(thread_hash(rotated), threads)
        self.assertEqual([row.profile for row in threads[thread_hash(THREAD)].rows[:1]], [B])

    def test_background_first_read_does_not_hold_the_poll(self):
        self.write(A, [self.row(A, 300, 150_000, 'a' * 12)])
        warmth = CacheWarmth(self.instances, clock=self.clock, background=True)
        entered, release = threading.Event(), threading.Event()
        original = cache_warmth.LedgerIndex._first_look

        def slow(index, *arguments):
            entered.set()
            release.wait(10)
            return original(index, *arguments)

        with mock.patch.object(cache_warmth.LedgerIndex, '_first_look', slow):
            started = time.monotonic()
            first = warmth.summary(self.profiles, self.families, pinned=[THREAD])
            self.assertTrue(entered.wait(10))
            second = warmth.summary(self.profiles, self.families, pinned=[THREAD])
            self.assertLess(time.monotonic() - started, 5)
            release.set()
            warmth.wait(10)
        self.assertEqual((first['threads'], first['loading']), ({}, True))
        self.assertTrue(second['loading'])
        ready = warmth.summary(self.profiles, self.families, pinned=[THREAD])
        self.assertNotIn('loading', ready)
        self.assertEqual(ready['threads'][thread_hash(THREAD)]['profiles'][A]['state'], 'warm')

    def test_selected_task_outside_the_recent_set_stays_described(self):
        self.write(A, [self.row(A, 600, 150_000, 'a' * 12)])
        others = [f'0199a1b2-0000-7000-8000-0000000001{index:02d}' for index in range(20)]
        self.write(B, [dict(self.row(B, 300 - index, 1000, 'b' * 12), thread=thread_hash(other))
                       for index, other in enumerate(others)])
        recent = self.warmth.summary(self.profiles, self.families)
        self.assertEqual(len(recent['threads']), cache_warmth.MAX_REPORTED_THREADS)
        self.assertNotIn(thread_hash(THREAD), recent['threads'])
        pinned = self.warmth.summary(self.profiles, self.families, pinned=[THREAD.upper(), THREAD, 'not-a-thread'])
        self.assertEqual(pinned['threads'][thread_hash(THREAD)]['profiles'][A]['state'], 'warm')
        self.assertEqual(len(pinned['threads']), cache_warmth.MAX_REPORTED_THREADS + 1)

    def test_sub_agent_threads_do_not_crowd_out_tasks(self):
        self.write(A, [self.row(A, 600, 150_000, 'a' * 12)])
        children = [f'0199a1b2-0000-7000-8000-0000000002{index:02d}' for index in range(20)]
        self.write(B, [dict(self.row(B, 300 - index, 1000, 'b' * 12), thread=thread_hash(child), sub=1)
                       for index, child in enumerate(children)])
        recent = self.warmth.summary(self.profiles, self.families)['threads']
        self.assertEqual(list(recent), [thread_hash(THREAD)])

    def test_a_thread_never_on_gpt_is_priced_within_the_gpt_window(self):
        self.write(D, [self.row(D, 600, 356_000, 'provider:cc_ds', family='cc_ds', model='deepseek-flash', window=1048576)])
        result = self.summary()['profiles']
        self.assertEqual(result[B]['uncached'], int(258_400 * .6))
        self.assertEqual(result[B]['line'], '이 작업 캐시 없음 · 첫 요청 ≈ 39 크레딧 (15.5만 token)')

    def test_the_threads_standard_tier_is_not_replaced_by_the_profiles_newest(self):
        other = '0199a1b2-c3d4-7e5f-8a9b-00000000000f'
        self.write(A, [self.row(A, 600, 100_000, 'a' * 12, tier=None),
                       dict(self.row(A, 60, 5_000, 'a' * 12, tier='priority'), thread=thread_hash(other))])
        entry = self.summary()['profiles'][A]
        self.assertAlmostEqual(entry['cost'], round(((102_000 - 83_000) * 250 + 83_000 * 25) / 1e6, 4))
        # A profile that never served the thread prices it at its newest tier.
        never = self.warmth.thread(other, self.profiles, self.families)['profiles']
        self.assertEqual(never[A]['state'], 'warm')

    def test_index_keeps_the_newest_threads_and_drops_expired_ones(self):
        threads = [f'0199a1b2-0000-7000-8000-0000000003{index:02d}' for index in range(5)]
        rows = [dict(self.row(A, 100 - index, 1000, 'a' * 12), thread=thread_hash(thread)) for index, thread in enumerate(threads)]
        rows.insert(0, dict(self.row(A, 25 * 3600, 1000, 'a' * 12), thread=thread_hash('expired')))
        self.write(A, rows)
        with mock.patch.object(cache_warmth, 'MAX_THREADS', 3):
            self.warmth.summary(self.profiles, self.families)
        self.assertEqual(set(self.warmth.index.threads), {thread_hash(thread) for thread in threads[2:]})

    def test_unknown_api_provider_and_corrupt_rows_are_ignored(self):
        self.profiles.append(profile('eeeeeeee-0000-4000-8000-000000000005', 'API2', external=True))
        directory = self.instances / A
        directory.mkdir(parents=True)
        (directory / LEDGER_FILE).write_bytes(b'not json\n{"v":2}\n' + json.dumps(self.row(A, 60, 90_000, 'a' * 12)).encode() + b'\n')
        result = self.summary()['profiles']
        self.assertNotIn('eeeeeeee-0000-4000-8000-000000000005', result)
        self.assertEqual(result[A]['state'], 'warm')


class ControlCenterWarmthTests(unittest.TestCase):
    def test_state_summary_maps_api_profiles_to_runtime_provider_ids(self):
        from control_center import ControlCenter
        from manager_core.providers import ProviderRegistry
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = {'id': '12345678-1234-4234-8234-123456789abc', 'base_url': 'https://api.example.test/v1',
                        'protocol': 'responses', 'adapter_id': 'native-responses', 'adapter_version': '1', 'revision': 2}
            runtime_id = ProviderRegistry._runtime_provider_id(provider)
            registry = {'providers': [provider], 'models': [{'id': 'm1', 'provider_id': provider['id'], 'wire_model_id': 'deepseek-flash'}]}
            store = type('Store', (), {'directory': root})()
            center = ControlCenter.__new__(ControlCenter)
            center.store = store
            instance = root / 'instances' / D
            instance.mkdir(parents=True)
            row = {'v': 1, 'ts': time.time() - 60, 'profile': D, 'account': 'provider:' + runtime_id, 'family': runtime_id,
                   'provider': runtime_id, 'model': 'deepseek-flash', 'tier': None, 'thread': thread_hash(THREAD),
                   'input': 100_000, 'output': 10, 'cached': 0}
            profiles = [{'id': D, 'alias': 'DS', 'auth_mode': 'external', 'external_model_id': 'm1'}]
            (instance / LEDGER_FILE).write_text(json.dumps(row) + '\n', encoding='ascii')
            # The first read runs in the background; this poll does not wait.
            self.assertEqual(center._cache_warmth_summary(profiles, registry), {'version': 1, 'threads': {}, 'loading': True})
            center._cache_warmth.wait(10)
            summary = center._cache_warmth_summary(profiles, registry)
            entry = summary['threads'][thread_hash(THREAD)]['profiles'][D]
            self.assertEqual((entry['state'], entry['kind']), ('warm', 'deepseek'))
            broken = center._cache_warmth_summary([{'id': 5}], registry)
            self.assertEqual(broken['threads'][thread_hash(THREAD)]['profiles'], {})

    def test_opened_and_selected_tasks_are_pinned(self):
        from control_center import ControlCenter
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            center = ControlCenter.__new__(ControlCenter)
            center.store = type('Store', (), {'directory': root})()
            selected, opened = '0199a1b2-0000-7000-8000-000000000401', '0199a1b2-0000-7000-8000-000000000402'
            (root / 'instances' / A).mkdir(parents=True)
            (root / 'instances' / A / 'active-task.json').write_text(json.dumps({'thread_id': selected}), encoding='utf-8')
            (root / 'instances' / B).mkdir(parents=True)
            (root / 'instances' / B / 'active-task.json').write_text('{broken', encoding='utf-8')
            profiles = [{'id': A, 'runtime_state': {'opened_task': {'thread_id': opened}}},
                        {'id': B, 'runtime_state': 'unexpected'}, {'id': '../escape'}]
            self.assertEqual(center._cache_warmth_pinned(profiles), [opened, selected])


if __name__ == '__main__':
    unittest.main()
