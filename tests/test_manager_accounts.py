"""Codex-only usage integration with synthetic accounts and cached snapshots."""

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core import accounts as accounts_module
from manager_core.accounts import Accounts
from manager_core.store import Store


class FakeWindow:
    def __init__(self, used=31, resets_at=None, key='short'):
        self.used_percent = used
        self.resets_at = resets_at
        self.key = key

    def is_expired(self, now):
        return self.resets_at is not None and self.resets_at <= now

    def to_dict(self):
        return {'key': self.key, 'label': 'Synthetic window',
                'used_percent': self.used_percent,
                'remaining_percent': 100 - self.used_percent,
                'duration_minutes': 300,
                'resets_at': self.resets_at.isoformat() if self.resets_at else None}


def fake_error(code='network', message='sensitive provider body and credential text'):
    return SimpleNamespace(code=SimpleNamespace(value=code), message=message,
                           to_dict=lambda: {'code': code, 'message': message})


def fake_snapshot(account, *, used=31, freshness='cached', error=None, windows=None):
    return SimpleNamespace(
        account_id=account.id, provider=account.provider, alias=account.alias,
        freshness=SimpleNamespace(value=freshness),
        auth_status=SimpleNamespace(value='authenticated'),
        observed_at=datetime.now(timezone.utc), error=error,
        windows=tuple(windows if windows is not None else [FakeWindow(used)]),
        source='synthetic-test', plan='test-plan', identity=None)


class FakeUsageStore:
    def __init__(self):
        self.accounts = []
        self.snapshots = {}
        self.ready = {}
        self.persistence_entries = 0
        self.codex_startup_is_ready = Mock(side_effect=self._ready)
        self.advance_codex_collection_cursor = Mock(
            side_effect=lambda ids: ids[0] if ids else None)

    def load_registry(self):
        return SimpleNamespace(accounts=self.accounts, active={})

    def load_snapshot(self, account_id):
        return self.snapshots.get(account_id)

    def _ready(self, account_id, *, binary_identity, state_identity):
        if binary_identity != 'synthetic-binary' or state_identity != 'synthetic-state':
            raise AssertionError('Startup identity was not preserved')
        return self.ready.get(account_id, True)

    @contextmanager
    def bounded_snapshot_persistence(self, *, lock_wait_budget):
        if lock_wait_budget < 0:
            raise AssertionError('Snapshot budget cannot be negative')
        self.persistence_entries += 1
        yield


class FakeCodexAdapter:
    def __init__(self, usage_store):
        self.usage_store = usage_store
        self.startup_precheck = Mock(return_value=(('synthetic-binary', 'synthetic-state'), None))
        self.collect = AsyncMock(side_effect=self._collect)
        self.warm = AsyncMock(side_effect=AssertionError('Usage refresh must not warm accounts'))

    async def _collect(self, profile, *, store, timeout, now):
        if profile.provider.value != 'codex':
            raise AssertionError('A non-Codex account reached the Codex collector')
        result = fake_snapshot(profile, used=42, freshness='live')
        store.snapshots[profile.id] = result
        return result


class AccountsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.accounts = Accounts(self.root)
        self.manager = Store(self.root)
        self.usage_store = FakeUsageStore()
        self.adapter = FakeCodexAdapter(self.usage_store)
        self.storage_factory = Mock(return_value=self.usage_store)
        self.adapter_factory = Mock(return_value=self.adapter)
        storage_patch = patch.object(Accounts, '_storage', return_value=self.usage_store)
        dependency_patch = patch.object(Accounts, '_dependencies',
                                        return_value=(self.storage_factory, self.adapter_factory))
        storage_patch.start()
        self.dependencies_mock = dependency_patch.start()
        self.addCleanup(storage_patch.stop)
        self.addCleanup(dependency_patch.stop)

    def add_account(self, alias='Synthetic Codex', provider='codex'):
        account = SimpleNamespace(id=str(uuid.uuid4()), alias=alias,
                                  provider=SimpleNamespace(value=provider),
                                  profile_dir=self.root / 'source-profiles' / str(uuid.uuid4()))
        self.usage_store.accounts.append(account)
        return account

    def profile_for(self, account):
        return next(profile for profile in self.manager.read()['profiles']
                    if profile.get('usage_account_id') == account.id)

    def test_cached_list_reads_only_codex_and_never_collects(self):
        codex = self.add_account()
        claude = self.add_account('Synthetic Claude', 'claude')
        self.usage_store.snapshots[codex.id] = fake_snapshot(codex, used=17)
        self.usage_store.snapshots[claude.id] = fake_snapshot(claude, used=93)
        result = self.accounts.list()
        self.assertEqual([item['id'] for item in result], [codex.id])
        self.assertEqual(result[0]['usage']['windows'][0]['used_percent'], 17)
        self.assertEqual(result[0]['usage']['freshness'], 'cached')
        self.adapter.collect.assert_not_called()
        self.adapter_factory.assert_not_called()
        self.dependencies_mock.assert_not_called()
        self.assertFalse(self.manager.path.exists())

    def test_missing_snapshot_remains_unknown_without_zero_usage(self):
        self.add_account()
        usage = self.accounts.list()[0]['usage']
        self.assertEqual(usage['windows'], [])
        self.assertIsNone(usage['observed_at'])
        self.assertEqual(usage['freshness'], 'unknown')
        self.assertNotIn('used_percent', usage)
        self.assertNotIn('remaining_percent', usage)

    def test_expired_window_is_removed_but_current_window_is_retained(self):
        account = self.add_account()
        now = datetime.now(timezone.utc)
        self.usage_store.snapshots[account.id] = fake_snapshot(account, windows=[
            FakeWindow(99, now - timedelta(seconds=1), 'expired'),
            FakeWindow(27, now + timedelta(hours=1), 'current')])
        usage = self.accounts.list()[0]['usage']
        self.assertEqual([window['key'] for window in usage['windows']], ['current'])
        self.assertEqual(usage['windows'][0]['used_percent'], 27)

    def test_cached_error_message_is_fixed_and_does_not_leak_raw_body(self):
        account = self.add_account()
        self.usage_store.snapshots[account.id] = fake_snapshot(account, error=fake_error())
        usage = self.accounts.list()[0]['usage']
        self.assertIsNotNone(usage['error'])
        rendered = json.dumps(usage)
        self.assertNotIn('sensitive provider body', rendered)
        self.assertNotIn('credential text', rendered)
        self.assertEqual(usage['windows'][0]['used_percent'], 31)

    def test_cached_snapshot_with_wrong_identity_is_not_displayed(self):
        expected = self.add_account('Expected')
        other = self.add_account('Other')
        self.usage_store.snapshots[expected.id] = fake_snapshot(other, used=91)
        result = self.accounts.list()
        usage = next(item['usage'] for item in result if item['id'] == expected.id)
        self.assertEqual(usage['windows'], [])
        self.assertEqual(usage['freshness'], 'unknown')
        self.assertIsNotNone(usage['error'])

    def test_alias_changes_preserve_manager_profile_uuid(self):
        account = self.add_account('Before rename')
        self.accounts.sync(self.manager)
        original_id = self.profile_for(account)['id']
        account.alias = 'After rename'
        self.accounts.sync(self.manager)
        updated = self.profile_for(account)
        self.assertEqual(updated['id'], original_id)
        self.assertEqual(updated['alias'], 'After rename')
        self.assertEqual(len(self.manager.read()['profiles']), 1)
        self.dependencies_mock.assert_not_called()
        self.adapter.collect.assert_not_called()

    def test_reused_alias_does_not_rebind_deleted_account(self):
        old = self.add_account('Reused alias')
        self.accounts.sync(self.manager)
        old_profile_id = self.profile_for(old)['id']
        self.usage_store.accounts.remove(old)
        replacement = self.add_account('Reused alias')
        self.accounts.sync(self.manager)
        old_profile = self.profile_for(old)
        new_profile = self.profile_for(replacement)
        self.assertEqual(old_profile['id'], old_profile_id)
        self.assertTrue(old_profile['account_missing'])
        self.assertNotEqual(new_profile['id'], old_profile_id)
        self.assertFalse(new_profile['account_missing'])

    def test_native_login_profile_preserves_identity_alias_and_usage_on_sync(self):
        account = self.add_account('Imported alias')
        self.accounts.sync(self.manager)
        profile = self.profile_for(account)
        expected_usage = {'windows': [], 'freshness': 'unknown', 'observed_at': None,
                          'error': None}
        self.manager.mutate(lambda data: self.manager.profile(profile['id'], data).update(
            auth_mode='native', alias='Windows 04', account_missing=False,
            source_home=None, account_fingerprint='native-identity', usage=expected_usage))
        before = self.profile_for(account)
        account.alias = 'Renamed elsewhere'
        self.usage_store.snapshots[account.id] = fake_snapshot(account, used=93)
        self.accounts.sync(self.manager)
        self.assertEqual(self.profile_for(account), before)
        self.assertEqual(len(self.manager.read()['profiles']), 1)
        self.usage_store.accounts.clear()
        self.accounts.sync(self.manager)
        self.assertEqual(self.profile_for(account), before)

    def test_manager_authority_alone_preserves_profile_during_usage_refresh(self):
        account = self.add_account('Imported alias')
        self.accounts.sync(self.manager)
        profile = self.profile_for(account)
        self.manager.mutate(lambda data: self.manager.profile(profile['id'], data).update(
            alias_authority='manager', alias='Windows alias', account_missing=False,
            usage={'windows': [], 'freshness': 'unknown', 'observed_at': None, 'error': None}))
        before = self.profile_for(account)
        account.alias = 'Foreign alias'
        result = self.accounts.refresh_live(self.manager)
        self.assertEqual(result['refreshed'], 1)
        self.assertEqual(self.profile_for(account), before)
        self.assertEqual(len(self.manager.read()['profiles']), 1)

    def test_unchanged_periodic_sync_never_rewrites_the_store(self):
        account = self.add_account('Stable alias')
        native = self.add_account('Native import')
        self.usage_store.snapshots[account.id] = fake_snapshot(account, used=17)
        self.accounts.sync(self.manager)
        self.manager.mutate(lambda data: self.manager.profile(self.profile_for(native)['id'], data).update(
            auth_mode='native', alias='Windows alias'))
        before = self.manager.path.read_bytes()
        with patch('manager_core.store.atomic_json', side_effect=AssertionError('rewrote unchanged store')):
            result = self.accounts.sync(self.manager)
            self.accounts.sync(self.manager)
        self.assertEqual(result['accounts'], 2)
        self.assertEqual(self.manager.path.read_bytes(), before)
        revision = self.manager.read()['revision']
        account.alias = 'Renamed alias'
        self.accounts.sync(self.manager)
        self.assertEqual(self.manager.read()['revision'], revision + 1)
        self.assertEqual(self.profile_for(account)['alias'], 'Renamed alias')
        self.usage_store.accounts.remove(account)
        self.accounts.sync(self.manager)
        self.assertTrue(self.profile_for(account)['account_missing'])
        self.assertEqual(self.manager.read()['revision'], revision + 2)

    def test_native_profile_without_usage_link_is_not_bound_by_equal_alias(self):
        profile = self.manager.add_profile('Same alias')
        self.manager.mutate(lambda data: self.manager.profile(profile['id'], data).update(
            auth_mode='native', alias_authority='manager'))
        before = self.manager.profile(profile['id'])
        imported = self.add_account('Same alias')
        self.accounts.sync(self.manager)
        self.assertEqual(self.manager.profile(profile['id']), before)
        self.assertNotEqual(self.profile_for(imported)['id'], profile['id'])

    def test_refresh_collects_only_ready_codex_accounts_and_exposes_live_usage(self):
        ready = self.add_account('Ready Codex')
        cold = self.add_account('Cold Codex')
        claude = self.add_account('Claude', 'claude')
        self.usage_store.ready[cold.id] = False
        self.accounts.refresh_live(self.manager)
        collected = [call.args[0].id for call in self.adapter.collect.await_args_list]
        self.assertEqual(collected, [ready.id])
        self.assertNotIn(claude.id, collected)
        ready_usage = self.profile_for(ready)['usage']
        self.assertEqual(ready_usage['freshness'], 'live')
        self.assertEqual(ready_usage['windows'][0]['used_percent'], 42)
        self.assertEqual(self.profile_for(cold)['usage']['windows'], [])
        self.assertIsNotNone(self.profile_for(cold)['usage']['error'])
        self.adapter.warm.assert_not_called()
        self.assertGreater(self.usage_store.persistence_entries, 0)
        self.assertEqual({profile['usage_account_id'] for profile in self.manager.read()['profiles']},
                         {ready.id, cold.id})

    def test_refresh_adapter_persists_snapshot_for_later_cached_list(self):
        account = self.add_account()
        self.usage_store.snapshots[account.id] = fake_snapshot(account, used=5)
        self.accounts.refresh_live(self.manager)
        self.assertEqual(self.usage_store.snapshots[account.id].windows[0].used_percent, 42)
        self.assertEqual(self.adapter.collect.await_count, 1)
        self.dependencies_mock.reset_mock()
        cached = self.accounts.list()[0]['usage']
        self.assertEqual(cached['windows'][0]['used_percent'], 42)
        self.assertEqual(cached['freshness'], 'cached')
        self.assertEqual(self.adapter.collect.await_count, 1)
        self.dependencies_mock.assert_not_called()

    def test_failed_collection_preserves_cached_windows_and_sanitizes_error(self):
        okay = self.add_account('Successful')
        failed = self.add_account('Failed')
        self.usage_store.snapshots[failed.id] = fake_snapshot(failed, used=64)

        async def collect(profile, *, store, timeout, now):
            if profile.id == failed.id:
                return fake_snapshot(profile, used=64, error=fake_error(), freshness='cached')
            return await self.adapter._collect(profile, store=store, timeout=timeout, now=now)

        self.adapter.collect.side_effect = collect
        self.accounts.refresh_live(self.manager)
        self.assertEqual(self.profile_for(okay)['usage']['freshness'], 'live')
        usage = self.profile_for(failed)['usage']
        self.assertEqual(usage['windows'][0]['used_percent'], 64)
        self.assertEqual(usage['freshness'], 'cached')
        self.assertIsNotNone(usage['error'])
        self.assertNotIn('sensitive provider body', json.dumps(self.manager.read()))

    def test_startup_precheck_failure_never_collects_or_warms(self):
        account = self.add_account()
        self.usage_store.snapshots[account.id] = fake_snapshot(account, used=71)
        self.adapter.startup_precheck.return_value = (None, fake_error('cli_missing'))
        self.accounts.refresh_live(self.manager)
        self.adapter.collect.assert_not_called()
        self.adapter.warm.assert_not_called()
        usage = self.profile_for(account)['usage']
        self.assertEqual(usage['windows'][0]['used_percent'], 71)
        self.assertIsNotNone(usage['error'])
        self.assertNotIn('sensitive provider body', json.dumps(usage))

    def test_rotating_cursor_changes_collection_order_without_identity_mixup(self):
        first = self.add_account('First')
        second = self.add_account('Second')
        self.usage_store.advance_codex_collection_cursor.side_effect = None
        self.usage_store.advance_codex_collection_cursor.return_value = second.id
        self.accounts.refresh_live(self.manager)
        self.assertEqual([call.args[0].id for call in self.adapter.collect.await_args_list],
                         [second.id, first.id])
        self.assertEqual(self.profile_for(first)['alias'], 'First')
        self.assertEqual(self.profile_for(second)['alias'], 'Second')

    def test_mismatched_result_account_id_cannot_cross_account_bind(self):
        expected = self.add_account('Expected')
        other = self.add_account('Other')
        self.usage_store.snapshots[expected.id] = fake_snapshot(expected, used=12)
        self.usage_store.snapshots[other.id] = fake_snapshot(other, used=83)

        async def mismatched(profile, *, store, timeout, now):
            if profile.id == expected.id:
                return fake_snapshot(other, used=98, freshness='live')
            return fake_snapshot(other, used=83, freshness='cached')

        self.adapter.collect.side_effect = mismatched
        self.accounts.refresh_live(self.manager)
        usage = self.profile_for(expected)['usage']
        self.assertEqual(usage['windows'][0]['used_percent'], 12)
        self.assertIsNotNone(usage['error'])
        self.assertEqual(self.profile_for(other)['usage']['windows'][0]['used_percent'], 83)

    def test_batch_timeout_cancels_collector_and_defers_remaining_account(self):
        first = self.add_account('Slow first')
        second = self.add_account('Deferred second')
        self.usage_store.snapshots[first.id] = fake_snapshot(first, used=28)
        self.usage_store.snapshots[second.id] = fake_snapshot(second, used=57)
        canceled = []

        async def too_slow(profile, *, store, timeout, now):
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                canceled.append(profile.id)
                raise

        self.adapter.collect.side_effect = too_slow
        with patch.object(accounts_module, '_REFRESH_BUDGET', 0.08), \
                patch.object(accounts_module, '_MIN_START_BUDGET', 0.02), \
                patch.object(accounts_module, '_CLEANUP_GRACE', 0.0):
            result = self.accounts.refresh_live(self.manager)
        self.assertEqual(canceled, [first.id])
        self.assertEqual(self.adapter.collect.await_count, 1)
        self.assertEqual(result['refreshed'], 0)
        self.assertEqual(result['failed'], 2)
        self.assertEqual(result['deferred'], 1)
        self.assertEqual(self.profile_for(first)['usage']['error']['code'], 'timeout')
        self.assertEqual(self.profile_for(second)['usage']['error']['code'], 'batch_deferred')
        self.assertEqual(self.profile_for(first)['usage']['windows'][0]['used_percent'], 28)
        self.assertEqual(self.profile_for(second)['usage']['windows'][0]['used_percent'], 57)

    def test_concurrent_refresh_is_rejected_without_second_collection(self):
        self.add_account()
        entered = threading.Event()
        released = threading.Event()
        results = []

        async def blocking(profile, *, store, timeout, now):
            entered.set()
            while not released.is_set():
                await asyncio.sleep(0.005)
            return await self.adapter._collect(profile, store=store, timeout=timeout, now=now)

        self.adapter.collect.side_effect = blocking

        def run_first():
            try:
                results.append(self.accounts.refresh_live(self.manager))
            except Exception as error:
                results.append(error)

        worker = threading.Thread(target=run_first, daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(3), 'First refresh did not enter its collector')
            with self.assertRaises(RuntimeError):
                Accounts(self.root).refresh_live(self.manager)
            self.assertEqual(self.adapter.collect.await_count, 1)
        finally:
            released.set()
            worker.join(4)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(results), 1)
        self.assertNotIsInstance(results[0], Exception)


if __name__ == '__main__':
    unittest.main()
