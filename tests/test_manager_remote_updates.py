"""SSH update coordinator uses simulated processes and no network or installers."""
from copy import deepcopy
import json
import threading
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import test_manager_update_hooks as fixtures
from test_manager_remote_restore import Fleet
from manager_core.remote_updates import RemoteUpdates
from manager_core.store import atomic_json, now
from manager_core.updates import UpdateError


class RemoteUpdateTests(unittest.TestCase):
    def setUp(self):
        fixture = self.fixture = fixtures.HookFixtures()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root, self.store, self.hooks = fixture.root, fixture.store, fixture.hooks
        self.profile_id = fixture.profile['id']
        self.fleet = Fleet(self.root, self.store, fixture.profile)
        self.hooks.remote_maintenance = self.fleet
        self.hooks.host_inventory = lambda profile: dict(complete=True,
            generation=profile['generation'], hosts=['local', 'fixture-a', 'fixture-b'])
        self.pending = []
        self.remote = Mock()
        self.probe = Mock(return_value=dict(cli_version='1.0', daemon_version='0.9',
            daemon_state='running', state='update_needed', update_supported=True,
            safe_auto_update=False, observation_id='d' * 64, host_identity='e' * 64,
            managed_host_identity='f' * 64, platform='linux', architecture='x86_64'))
        self.stock_update = Mock(return_value=dict(cli_version='1.0', daemon_version='0.9',
            state='update_needed', safe_auto_update=False,
            update_job=dict(state='starting', id='stock-job')))
        self.service = self.make_service()
        self.artifact = self.root / 'artifacts/remote/linux-x86_64/manifest.json'
        atomic_json(self.artifact, dict(version='2.0', files=[]))
        self.bundle = self.service._available('linux', 'x86_64')['bundle_id']
        def save(data):
            profile = self.store.profile(self.profile_id, data)
            profile['remote_bindings'] = [dict(b, prepared=True, runtime_bundle='1.0-' + 'a' * 16,
                host_identity='f' * 64) for b in self.fleet.bindings_by_alias.values()]
            data['ssh_inventory'] = {self.profile_id: dict(hosts=['fixture-a', 'fixture-b'])}
        self.store.mutate(save)
        self.manifest = self.store.directory / 'profiles' / self.profile_id / 'ssh-bindings.json'
        atomic_json(self.manifest, dict(profile_id=self.profile_id,
            generation=fixture.profile['generation'], bindings=list(self.fleet.bindings_by_alias.values())))
        prepare = self.fleet.prepare
        self.fleet.prepare = lambda *args, **kwargs: dict(prepare(*args, **kwargs),
            runtime_bundle=self.bundle, host_identity='f' * 64)
        request = self.fleet.request
        def observe(binding, operation, **params):
            value = request(binding, operation, **params)
            if value['process'] is not None:
                value.update(runtime_bundle='1.0-' + 'a' * 16 if value['process']['revision'] == 'a' * 64
                             else self.bundle, host_identity='f' * 64)
            return value
        self.fleet.request = observe
        self.addCleanup(self.finish_workers)

    def finish_workers(self):
        # Injected spawn keeps real worker file claims until its callback runs.
        for value in self.store.read().get('remote_updates', {}).values():
            self.service._save(value['profile_id'], value['alias'], auto_apply=False)
        while self.pending:
            self.drain()

    def make_service(self):
        return RemoteUpdates(self.root, self.store, self.remote, self.hooks,
            probe=self.probe, stock_update=self.stock_update, spawn=self.pending.append, clock=lambda: 4000)

    def status(self):
        return self.service.status(self.profile_id, 'fixture-a')

    def schedule(self):
        return self.service.schedule(self.profile_id, 'fixture-a')

    def drain(self):
        self.pending.pop(0)()

    def records(self):
        value = self.service._read(self.profile_id, 'fixture-a')
        return self.service._lease(value['_job']['transaction_id'])['profiles'][0]['remotes']

    def test_status_is_passive_and_defaults_do_not_auto_apply(self):
        value = self.status()
        self.assertTrue(value['auto_check'])
        self.assertFalse(value['auto_apply'])
        self.assertTrue(value['managed']['can_schedule'])
        self.probe.assert_not_called()
        self.assertEqual(self.fleet.calls, [])
        self.assertEqual(self.pending, [])

    def test_restart_clears_stale_checking_in_one_write_before_the_scheduler(self):
        self.service._save(self.profile_id, 'fixture-a', checking=True)
        self.service._save(self.profile_id, 'fixture-b', checking=False)
        revision = self.store.read()['revision']
        seen, looped = [], threading.Event()
        def loop():
            seen.append({v['alias']: v['checking'] for v in self.store.read()['remote_updates'].values()})
            looped.set()
        restarted = self.make_service()
        restarted._loop = loop
        restarted.start()
        self.assertTrue(looped.wait(3))
        self.assertEqual(seen, [{'fixture-a': False, 'fixture-b': False}])
        self.assertEqual(self.store.read()['revision'], revision + 1)
        self.assertFalse(restarted.status_all()['worker_active'])
        again = self.make_service()
        again._loop = lambda: None
        with patch('manager_core.store.atomic_json', side_effect=AssertionError('rewrote unchanged store')):
            again.start()
        self.assertEqual(self.store.read()['revision'], revision + 1)
        self.assertEqual(self.pending, [])
        self.probe.assert_not_called()
        self.stock_update.assert_not_called()

    def test_state_poll_status_all_reads_only_its_snapshot(self):
        self.service._save(self.profile_id, 'fixture-a', checking=True)
        snapshot = self.store.read()
        with patch.object(self.store, 'read', side_effect=AssertionError('store re-read')):
            value = self.service.status_all(state=snapshot)
        self.assertTrue(value['worker_active'])
        value['items'][0]['managed']['state'] = 'caller edit'
        self.assertNotEqual(snapshot['remote_updates'][self.profile_id + ':fixture-a']['managed']['state'],
                            'caller edit')
        self.assertEqual(self.service.status_all(state={})['items'], [])

    def test_shutdown_preserves_remote_journal_but_cancels_queued_callbacks(self):
        self.schedule()
        before = self.store.read()['remote_updates']
        self.service.shutdown()
        self.drain()
        self.service.tick()
        self.assertEqual(self.pending, [])
        self.assertEqual(self.service.busy, set())
        self.assertEqual(self.store.read()['remote_updates'], before)
        self.assertEqual(self.fleet.calls, [])
        self.stock_update.assert_not_called()

    def test_cohort_status_and_cancel_preserve_requested_alias(self):
        self.schedule()
        value = self.service.schedule(self.profile_id, 'fixture-b')
        self.assertEqual(value['alias'], 'fixture-b')
        self.assertEqual(value['job']['alias'], 'fixture-a')
        cancelled = self.service.cancel(self.profile_id, 'fixture-b')
        self.assertEqual(cancelled['alias'], 'fixture-b')
        self.drain()
        self.assertEqual(self.status()['job']['state'], 'cancelled')

    def test_check_is_async_and_separates_prepared_from_running_bundle(self):
        def newer_prepared(data):
            self.store.profile(self.profile_id, data)['remote_bindings'][0]['runtime_bundle'] = self.bundle
        self.store.mutate(newer_prepared)
        self.assertTrue(self.service.check(self.profile_id, 'fixture-a')['checking'])
        self.probe.assert_not_called()
        self.drain()
        managed = self.status()['managed']
        self.assertEqual(managed['active_version'], '1.0')
        self.assertEqual(managed['prepared_version'], '2.0')
        self.assertEqual(managed['available_version'], '2.0')
        self.assertEqual(managed['state'], 'update_available')
        self.remote.inspect.assert_not_called()
        self.stock_update.assert_not_called()

    def test_busy_remote_waits_without_stopping_or_changing_local_work(self):
        original = self.fleet.request
        self.fleet.request = lambda *a, **k: {**original(*a, **k), 'idle': False}
        generation = self.store.profile(self.profile_id)['generation']
        self.schedule()
        self.drain()
        self.assertEqual(self.status()['job']['state'], 'waiting')
        self.assertFalse(any(op == 'stop' for op, _ in self.fleet.calls))
        self.assertEqual(self.fixture.instances.show_calls, [])
        self.assertEqual(self.fixture.admin.calls, [])
        self.assertEqual(self.fixture.closes, [])
        self.assertEqual(self.store.profile(self.profile_id)['generation'], generation)
        self.hooks.guard_launch(self.profile_id)

    def test_unknown_inventory_waits_before_lifecycle(self):
        self.hooks.host_inventory = lambda profile: dict(complete=False, generation=profile['generation'],
            hosts=['local', 'fixture-a', 'fixture-b'])
        self.schedule()
        self.drain()
        self.assertEqual(self.status()['job']['state'], 'waiting')
        self.assertFalse(any(op in ('stop', 'start', 'prepare') for op, _ in self.fleet.calls))

    def test_idle_update_publishes_whole_profile_cohort_without_local_restart(self):
        self.fleet.binding_matches_settings = lambda *args: True
        self.schedule()
        self.drain()
        self.assertEqual(self.status()['job']['state'], 'complete')
        self.assertEqual(self.status()['managed']['state'], 'current')
        self.assertEqual({b['revision'] for b in json.loads(self.manifest.read_text())['bindings']}, {'b' * 64})
        self.assertEqual(self.fixture.instances.show_calls, [])
        self.assertEqual(self.fixture.closes, [])
        self.assertEqual(self.fleet.calls.count(('stop', 'fixture-a')), 1)
        self.assertEqual(self.fleet.calls.count(('stop', 'fixture-b')), 1)
        self.stock_update.assert_not_called()

    def test_lost_start_reply_resumes_by_inspection_without_replaying_start(self):
        self.fleet.lose_start = 'fixture-a'
        self.schedule()
        self.drain()
        self.assertEqual(self.status()['job']['state'], 'waiting')
        self.assertEqual(self.records()[0]['state'], 'start_requested')
        restarted = self.make_service()
        restarted.tick()
        self.drain()
        self.assertEqual(self.status()['job']['state'], 'complete')
        self.assertEqual(self.fleet.calls.count(('start', 'fixture-a')), 1)
        self.assertEqual(self.fleet.calls.count(('stop', 'fixture-a')), 1)

    def test_lost_stop_reply_does_not_replay_stop(self):
        stop = self.fleet.stop
        lost = [False]
        def uncertain(record):
            result = stop(record)
            if not lost[0]:
                lost[0] = True
                raise OSError('simulated lost stop reply')
            return result
        self.fleet.stop = uncertain
        self.schedule()
        self.drain()
        self.assertEqual(self.records()[0]['state'], 'stop_requested')
        self.service.step(self.profile_id, 'fixture-a')
        self.assertEqual(self.status()['job']['state'], 'complete')
        self.assertEqual(self.fleet.calls.count(('stop', 'fixture-a')), 1)

    def test_start_failure_preserves_immutable_previous_source_and_manifest(self):
        previous = self.manifest.read_bytes()
        request = self.fleet.request
        def fail(binding, operation, **params):
            if operation == 'start':
                raise UpdateError('remote_start_timeout', 'simulated timeout')
            return request(binding, operation, **params)
        self.fleet.request = fail
        self.schedule()
        self.drain()
        self.assertEqual(self.status()['job']['state'], 'attention')
        self.assertEqual(self.manifest.read_bytes(), previous)
        value = self.service._read(self.profile_id, 'fixture-a')
        lease = self.service._lease(value['_job']['transaction_id'])
        self.assertEqual(lease['previous_bindings'][0]['runtime_bundle'], '1.0-' + 'a' * 16)
        self.assertEqual(self.records()[0]['state'], 'start_requested')

    def test_cancellation_before_mutation_releases_ssh_gate(self):
        request = self.fleet.request
        self.fleet.request = lambda *a, **k: {**request(*a, **k), 'idle': False}
        self.schedule()
        self.drain()
        self.service.cancel(self.profile_id, 'fixture-a')
        self.drain()
        self.assertEqual(self.status()['job']['state'], 'cancelled')
        self.assertEqual(self.store.read()['ssh_maintenance'][self.profile_id]['state'], 'released')
        self.assertFalse(any(op == 'stop' for op, _ in self.fleet.calls))

    def test_cancel_after_uncertain_start_keeps_gate_and_journal(self):
        self.fleet.lose_start = 'fixture-a'
        self.schedule()
        self.drain()
        self.service.cancel(self.profile_id, 'fixture-a')
        self.drain()
        self.assertEqual(self.status()['job']['state'], 'recovering')
        self.assertEqual(self.records()[0]['state'], 'start_requested')
        self.assertEqual(self.store.read()['ssh_maintenance'][self.profile_id]['state'], 'held')

    def test_another_controller_cannot_cancel_without_worker_file_claim(self):
        # Scheduling owns the file claim even before its worker runs. A
        # second controller may record intent but cannot release its SSH gate.
        self.schedule()
        value = self.service._read(self.profile_id, 'fixture-a')
        transaction = value['_job']['transaction_id']
        self.hooks.begin_remote_reconcile(self.profile_id, ensure_local=False,
            force_runtime_update=True, transaction_id=transaction)
        other = self.make_service()
        response = other.cancel(self.profile_id, 'fixture-a')
        self.assertEqual(response['job']['state'], 'queued')
        self.assertEqual(len(self.pending), 1)
        self.assertEqual(self.store.read()['ssh_maintenance'][self.profile_id]['state'], 'held')
        self.drain()
        self.assertEqual(self.status()['job']['state'], 'cancelled')
        self.assertEqual(self.store.read()['ssh_maintenance'][self.profile_id]['state'], 'released')

    def test_cancellation_during_target_publication_is_not_lost(self):
        update = self.service._update_pin
        raced = [False]
        other = self.make_service()
        def publish(profile_id, alias, transaction, **changes):
            if 'targets' in changes and not raced[0]:
                raced[0] = True
                other.cancel(profile_id, alias)
            return update(profile_id, alias, transaction, **changes)
        self.service._update_pin = publish
        self.schedule()
        self.drain()
        self.assertEqual(self.status()['job']['state'], 'cancelled')
        self.assertFalse(any(op in ('stop', 'start', 'prepare') for op, _ in self.fleet.calls))

    def test_cancel_intent_preserves_pinned_targets_and_terminal_state(self):
        self.schedule()
        value = self.service._read(self.profile_id, 'fixture-a')
        transaction = value['_job']['transaction_id']
        targets = {'fixture-a': {'bundle': self.bundle}}
        self.service._update_pin(self.profile_id, 'fixture-a', transaction, targets=targets)
        self.service._update_pin(self.profile_id, 'fixture-a', transaction, cancel_requested=True)
        self.assertEqual(self.service._read(self.profile_id, 'fixture-a')['_job']['targets'], targets)
        self.drain()
        self.assertEqual(self.status()['job']['state'], 'cancelled')

    def test_generation_change_supersedes_job_before_any_lifecycle(self):
        self.schedule()
        self.store.mutate(lambda data: self.store.profile(self.profile_id, data).update(generation=str(uuid4())))
        self.drain()
        job = self.status()['job']
        self.assertEqual(job['state'], 'cancelled')
        self.assertEqual(job['code'], 'ssh_generation_superseded')
        # No lifecycle ran, so no gate was ever created for this reservation.
        gate = self.store.read().get('ssh_maintenance', {}).get(self.profile_id)
        self.assertIn(None if gate is None else gate['state'], (None, 'released'))
        self.assertFalse(any(op in ('stop', 'prepare', 'start') for op, _ in self.fleet.calls))

    def test_relaunch_rearms_auto_apply_for_the_current_generation(self):
        request = self.fleet.request
        self.fleet.request = lambda *a, **k: {**request(*a, **k), 'idle': False}
        self.service.settings(self.profile_id, 'fixture-a', auto_apply=True)
        self.schedule()
        self.drain()
        self.assertEqual(self.status()['job']['state'], 'waiting')
        generation = str(uuid4())
        self.store.mutate(lambda data: self.store.profile(self.profile_id, data).update(generation=generation))
        self.service.tick()
        self.drain()
        value = self.service._read(self.profile_id, 'fixture-a')
        # The stale reservation is retired and the same automatic update is
        # re-queued against the relaunched generation instead of leaving a gate.
        self.assertEqual(value['job']['state'], 'queued')
        self.assertEqual(value['_job']['generation'], generation)
        self.assertTrue(value['auto_apply'])
        self.assertEqual(self.store.read()['ssh_maintenance'][self.profile_id]['state'], 'released')

    def test_relaunch_keeps_gate_when_a_start_was_already_dispatched(self):
        self.fleet.lose_start = 'fixture-a'
        self.schedule()
        self.drain()
        self.assertEqual(self.records()[0]['state'], 'start_requested')
        self.store.mutate(lambda data: self.store.profile(self.profile_id, data).update(generation=str(uuid4())))
        self.service.tick()
        self.drain()
        self.assertFalse(self.service.retirement(self.profile_id, 'fixture-a')['eligible'])
        self.assertEqual(self.status()['job']['state'], 'attention')
        self.assertEqual(self.store.read()['ssh_maintenance'][self.profile_id]['state'], 'attention')
        self.assertEqual(self.records()[0]['state'], 'start_requested')

    def test_scheduler_recovers_a_gate_left_by_an_older_generation(self):
        # Production shape: the reservation flipped to attention when the
        # profile relaunched and nothing can step it any more.
        request = self.fleet.request
        self.fleet.request = lambda *a, **k: {**request(*a, **k), 'idle': False}
        self.schedule()
        self.drain()
        value = self.service._read(self.profile_id, 'fixture-a')
        transaction = value['_job']['transaction_id']
        self.store.mutate(lambda data: self.store.profile(self.profile_id, data).update(generation=str(uuid4())))
        self.service._job_state(self.profile_id, 'fixture-a', 'attention',
                                'SSH 업데이트 결과 확인이 필요합니다.', code='ssh_generation_changed')
        self.hooks.remote_open_failed(self.profile_id, transaction, 'ssh_generation_changed')
        self.service.tick()
        self.drain()
        job = self.status()['job']
        self.assertEqual(job['state'], 'cancelled')
        self.assertEqual(job['code'], 'ssh_generation_superseded')
        self.assertEqual(self.store.read()['ssh_maintenance'][self.profile_id]['state'], 'released')
        self.assertFalse(any(op in ('stop', 'prepare', 'start') for op, _ in self.fleet.calls))

    def test_retirement_without_a_journal_keeps_the_gate_and_asks_for_review(self):
        request = self.fleet.request
        self.fleet.request = lambda *a, **k: {**request(*a, **k), 'idle': False}
        self.schedule()
        self.drain()
        transaction = self.service._read(self.profile_id, 'fixture-a')['_job']['transaction_id']
        self.store.mutate(lambda data: self.store.profile(self.profile_id, data).update(generation=str(uuid4())))
        self.service.hooks._lease_path(transaction).unlink()
        report = self.service.retirement(self.profile_id, 'fixture-a')
        self.assertFalse(report['eligible'])
        self.assertEqual(report['reason'], 'journal_missing')
        self.service.tick()
        self.drain()
        self.assertEqual(self.status()['job']['state'], 'attention')
        self.assertEqual(self.store.read()['ssh_maintenance'][self.profile_id]['state'], 'attention')

    def test_recover_releases_only_an_unmoved_stale_reservation(self):
        request = self.fleet.request
        self.fleet.request = lambda *a, **k: {**request(*a, **k), 'idle': False}
        self.service.settings(self.profile_id, 'fixture-a', auto_apply=True)
        self.schedule()
        self.drain()
        self.assertEqual(self.service.recover(self.profile_id, 'fixture-a')['reason'], 'generation_current')
        self.assertEqual(self.store.read()['ssh_maintenance'][self.profile_id]['state'], 'held')
        self.store.mutate(lambda data: self.store.profile(self.profile_id, data).update(generation=str(uuid4())))
        report = self.service.recover(self.profile_id, 'fixture-a', observe=True)
        self.assertTrue(report['released'], report)
        self.assertTrue(report['gate_released'])
        self.assertTrue(report['journal_released'])
        self.assertEqual(report['reason'], None)
        self.assertEqual(self.store.read()['ssh_maintenance'][self.profile_id]['state'], 'released')
        current = self.status()
        self.assertTrue(current['auto_apply'])
        self.assertEqual(current['job']['state'], 'queued')
        self.assertEqual(self.service._read(self.profile_id, 'fixture-a')['_job']['generation'],
                         self.store.profile(self.profile_id)['generation'])
        self.assertFalse(any(op in ('stop', 'prepare') for op, _ in self.fleet.calls))
        # The freshly re-queued reservation pins the current generation and is
        # therefore never retired by a later recovery call.
        self.assertEqual(self.service.recover(self.profile_id, 'fixture-a')['reason'], 'generation_current')

    def test_recover_refuses_a_dispatched_lifecycle(self):
        self.fleet.lose_start = 'fixture-a'
        self.schedule()
        self.drain()
        self.store.mutate(lambda data: self.store.profile(self.profile_id, data).update(generation=str(uuid4())))
        report = self.service.recover(self.profile_id, 'fixture-a')
        self.assertFalse(report['released'])
        self.assertEqual(report['reason'], 'lifecycle_pending')
        self.assertEqual(self.records()[0]['state'], 'start_requested')
        self.assertEqual(self.store.read()['ssh_maintenance'][self.profile_id]['state'], 'held')

    def stale_after_relaunch(self):
        """Hold a real gate and journal, then relaunch the profile under it."""
        request = self.fleet.request
        self.fleet.request = lambda *a, **k: {**request(*a, **k), 'idle': False}
        self.schedule()
        self.drain()
        transaction = self.service._read(self.profile_id, 'fixture-a')['_job']['transaction_id']
        self.store.mutate(lambda data: self.store.profile(self.profile_id, data).update(generation=str(uuid4())))
        return transaction

    def test_retirement_refuses_a_journal_owned_by_another_transaction(self):
        transaction = self.stale_after_relaunch()
        path = self.hooks._lease_path(transaction)
        foreign = {**json.loads(path.read_text(encoding='utf-8')), 'transaction_id': str(uuid4())}
        atomic_json(path, foreign)
        self.assertEqual(self.service.retirement(self.profile_id, 'fixture-a')['reason'], 'journal_unverified')
        report = self.service.recover(self.profile_id, 'fixture-a')
        self.assertFalse(report['released'])
        self.assertFalse(report['journal_released'])
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['state'], 'held')
        self.assertEqual(self.store.read()['ssh_maintenance'][self.profile_id]['state'], 'held')

    def test_retirement_refuses_a_journal_bound_to_another_generation(self):
        transaction = self.stale_after_relaunch()
        path = self.hooks._lease_path(transaction)
        adopted = json.loads(path.read_text(encoding='utf-8'))
        adopted['profiles'][0]['generation'] = self.store.profile(self.profile_id)['generation']
        atomic_json(path, adopted)
        report = self.service.recover(self.profile_id, 'fixture-a')
        self.assertEqual(report['reason'], 'journal_generation_changed')
        self.assertFalse(report['released'])
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['state'], 'held')
        self.assertEqual(self.store.read()['ssh_maintenance'][self.profile_id]['state'], 'held')

    def test_retirement_refuses_a_gate_reclaimed_by_another_generation(self):
        self.stale_after_relaunch()
        generation = self.store.profile(self.profile_id)['generation']
        self.store.mutate(lambda data: data['ssh_maintenance'][self.profile_id].update(generation=generation))
        report = self.service.recover(self.profile_id, 'fixture-a')
        self.assertEqual(report['reason'], 'gate_generation_changed')
        self.assertFalse(report['released'])
        self.assertEqual(self.service._read(self.profile_id, 'fixture-a')['job']['state'], 'waiting')
        self.assertEqual(self.store.read()['ssh_maintenance'][self.profile_id]['state'], 'held')

    def test_retirement_refuses_a_journal_that_already_stopped_a_host(self):
        transaction = self.stale_after_relaunch()
        path = self.hooks._lease_path(transaction)
        stopped = json.loads(path.read_text(encoding='utf-8'))
        stopped['profiles'][0]['remotes'][0].update(state='closed', exit_proof=dict(exited=True, idle=True))
        atomic_json(path, stopped)
        report = self.service.retirement(self.profile_id, 'fixture-a')
        self.assertEqual(report['reason'], 'lifecycle_pending')
        self.assertFalse(report['eligible'])
        self.assertEqual(self.store.read()['ssh_maintenance'][self.profile_id]['state'], 'held')

    def test_retirement_keeps_the_gate_when_the_journal_cannot_be_persisted(self):
        transaction = self.stale_after_relaunch()
        path = self.hooks._lease_path(transaction)
        before = path.read_bytes()
        def broken(lease):
            raise OSError('simulated journal write failure')
        self.hooks._save_lease = broken
        report = self.service.recover(self.profile_id, 'fixture-a')
        self.assertEqual(report['reason'], 'unavailable')
        self.assertFalse(report['released'])
        self.assertFalse(report['journal_released'])
        self.assertEqual(self.store.read()['ssh_maintenance'][self.profile_id]['state'], 'held')
        self.assertEqual(path.read_bytes(), before)

    def test_recover_reports_busy_while_another_controller_holds_the_claim(self):
        self.schedule()
        transaction = self.service._read(self.profile_id, 'fixture-a')['_job']['transaction_id']
        self.hooks.begin_remote_reconcile(self.profile_id, ensure_local=False,
            force_runtime_update=True, transaction_id=transaction)
        self.store.mutate(lambda data: self.store.profile(self.profile_id, data).update(generation=str(uuid4())))
        report = self.make_service().recover(self.profile_id, 'fixture-a')
        self.assertEqual(report['reason'], 'busy')
        self.assertFalse(report['released'])
        self.assertEqual(self.store.read()['ssh_maintenance'][self.profile_id]['state'], 'held')
        self.drain()
        self.assertEqual(self.status()['job']['state'], 'cancelled')
        self.assertEqual(self.store.read()['ssh_maintenance'][self.profile_id]['state'], 'released')

    def test_host_identity_change_prevents_runtime_mutations(self):
        self.probe.return_value['managed_host_identity'] = 'c' * 64
        self.schedule()
        self.drain()
        self.assertEqual(self.status()['job']['state'], 'waiting')
        self.assertFalse(any(op in ('stop', 'prepare', 'start') for op, _ in self.fleet.calls))

    def test_auto_settings_persist_and_only_schedule_managed_updates(self):
        self.service.settings(self.profile_id, 'fixture-a', auto_check=True, auto_apply=True)
        restarted = self.make_service()
        restarted.tick()
        self.drain()
        self.assertEqual(self.status()['job']['state'], 'queued')
        self.assertTrue(self.status()['auto_apply'])
        self.stock_update.assert_not_called()

    def test_stock_manual_update_requires_current_confirmed_observation(self):
        with self.assertRaises(ValueError):
            self.service.update_stock(self.profile_id, 'fixture-a', confirmed=True)
        self.service.check(self.profile_id, 'fixture-a')
        self.drain()
        with self.assertRaises(ValueError):
            self.service.update_stock(self.profile_id, 'fixture-a', observation_id='d' * 64)
        for token in (None, '', 'old'):
            with self.subTest(token=token), self.assertRaises(ValueError):
                self.service.update_stock(self.profile_id, 'fixture-a', confirmed=True, observation_id=token)
        self.stock_update.assert_not_called()
        self.service.update_stock(self.profile_id, 'fixture-a', confirmed=True, observation_id='d' * 64)
        self.drain()
        self.stock_update.assert_called_once_with(self.root, self.remote, 'fixture-a', 'd' * 64, confirmed=True)
        self.assertEqual(self.status()['stock']['update_job']['state'], 'starting')
        self.assertIsNone(self.status()['job'])

    def test_stock_confirmation_cannot_substitute_a_newer_cached_observation(self):
        self.service.check(self.profile_id, 'fixture-a')
        self.drain()
        displayed_token = self.status()['stock']['observation_id']
        self.probe.return_value['observation_id'] = 'c' * 64
        self.service.check(self.profile_id, 'fixture-a')
        self.drain()
        with self.assertRaises(ValueError):
            self.service.update_stock(self.profile_id, 'fixture-a', confirmed=True,
                                      observation_id=displayed_token)
        with self.assertRaises(ValueError):
            self.service.update_stock(self.profile_id, 'fixture-a', confirmed=True)
        self.stock_update.assert_not_called()
        self.assertEqual(self.pending, [])

    def test_pending_stock_job_polls_read_only_even_when_auto_checks_disabled(self):
        self.service.settings(self.profile_id, 'fixture-a', auto_check=False)
        self.service._save(self.profile_id, 'fixture-a', stock=dict(
            update_job=dict(state='applying', id='stock-job'), safe_auto_update=False))
        self.service.tick()
        self.drain()
        self.probe.assert_called()
        self.stock_update.assert_not_called()

    def test_waiting_managed_job_cannot_starve_stock_completion_verification(self):
        request = self.fleet.request
        self.fleet.request = lambda *a, **k: {**request(*a, **k), 'idle': False}
        self.service.settings(self.profile_id, 'fixture-a', auto_check=False)
        self.schedule()
        self.drain()
        self.assertEqual(self.status()['job']['state'], 'waiting')
        self.service._save(self.profile_id, 'fixture-a', stock=dict(update_job=dict(
            state='attention', verification_pending=True), safe_auto_update=False))
        self.probe.reset_mock()
        self.service.tick()
        self.drain()
        self.probe.assert_called_once_with(self.root, self.remote, 'fixture-a')
        self.stock_update.assert_not_called()
        self.assertEqual(self.status()['job']['state'], 'waiting')

    def defer(self):
        """The profile relaunched with its previous SSH settings still running."""
        profile = self.store.profile(self.profile_id)
        transaction = str(uuid4())
        def write(data):
            data.setdefault('profile_restarts', {})[self.profile_id] = dict(
                id=str(uuid4()), profile_id=self.profile_id, phase='attention',
                code='ssh_settings_deferred', connections_restored=True,
                generation=profile['generation'], transaction_id=transaction,
                requested_revision=profile['policy']['desired_revision'],
                remote_background=True, message='fixture deferred', updated_at=now())
        self.store.mutate(write)
        manifest = json.loads(self.manifest.read_text())
        manifest.update(pending_policy_hosts=['fixture-a', 'fixture-b'],
                        deferred_policy_hosts=['fixture-a', 'fixture-b'])
        atomic_json(self.manifest, manifest)

    def notice(self):
        return self.store.read()['profile_restarts'][self.profile_id]

    def test_successful_managed_update_retires_the_deferred_notice(self):
        self.defer()
        self.schedule()
        self.drain()
        self.assertEqual(self.status()['job']['state'], 'complete')
        notice = self.notice()
        self.assertEqual(notice['phase'], 'complete')
        self.assertNotIn('code', notice)
        self.assertNotIn('connections_restored', notice)
        manifest = json.loads(self.manifest.read_text())
        self.assertEqual(manifest['pending_policy_hosts'], [])
        self.assertEqual(manifest['deferred_policy_hosts'], [])
        self.assertEqual({b['revision'] for b in manifest['bindings']}, {'b' * 64})
        self.assertEqual(self.fixture.instances.show_calls, [])
        self.assertEqual(self.fixture.closes, [])

    def test_failed_managed_update_keeps_the_deferred_notice(self):
        self.defer()
        before = self.manifest.read_bytes()
        request = self.fleet.request
        def fail(binding, operation, **params):
            if operation == 'start':
                raise UpdateError('remote_start_timeout', 'simulated timeout')
            return request(binding, operation, **params)
        self.fleet.request = fail
        self.schedule()
        self.drain()
        self.assertEqual(self.status()['job']['state'], 'attention')
        self.assertEqual(self.manifest.read_bytes(), before)
        notice = self.notice()
        self.assertEqual(notice['phase'], 'attention')
        self.assertEqual(notice['code'], 'ssh_settings_deferred')
        self.assertTrue(notice['connections_restored'])


if __name__ == '__main__':
    unittest.main()
