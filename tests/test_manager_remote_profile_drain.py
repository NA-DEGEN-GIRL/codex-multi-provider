"""Explicit per-profile SSH drain with durable process identity and live peers."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch
from uuid import uuid4
import test_manager_local_first_remote as local_first
from manager_core.updates import UpdateError


class RemoteProfileDrainTests(unittest.TestCase):
    def setUp(self):
        self.fx = local_first.LocalFirstRemoteTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.store, self.hooks = self.fx.store, self.fx.hooks
        self.profile, self.fleet = self.fx.profile, self.fx.fleet
        self.fleet.idle_evidence = False
        self.ready = False
        self.lost = False
        self.drains = []
        original = self.fleet.request
        def request(binding, operation, **params):
            if operation != 'drain':
                return original(binding, operation, **params)
            alias = binding['alias']
            self.drains.append((deepcopy(binding), deepcopy(params)))
            process = self.fleet.running.get(alias)
            if process is not None and params['expected_process'] != process:
                raise UpdateError('process_changed', 'fixture identity changed')
            gate = self.store.read()['ssh_maintenance'][self.profile['id']]
            lease = json.loads(self.hooks._lease_path(gate['transaction_id']).read_text(encoding='utf-8'))
            record = next(r for r in lease['profiles'][0]['remotes'] if r['alias'] == alias)
            self.assertEqual(record['state'], 'drain_requested')
            self.assertEqual(record['process'], params['expected_process'])
            if self.ready:
                self.fleet.running[alias] = process = None
            if self.lost:
                self.lost = False
                raise OSError('fixture lost reply')
            return dict(binding=binding, process=deepcopy(process), idle=process is None, exited=process is None)
        self.fleet.request = request

    def schedule(self, stop_only=False):
        p = self.store.profile(self.profile['id'])
        return self.fx.restarts.schedule_remote(p['id'], expected_generation=p['generation'], stop_only=stop_only)

    def step(self, job):
        return self.fx.restarts.step(self.profile['id'], job['id'])

    def test_explicit_request_waits_then_applies_without_touching_desktop_or_peer(self):
        peer = self.store.add_profile('peer')
        before = deepcopy(peer)
        job = self.schedule()
        self.assertFalse(self.step(job))
        self.assertEqual(len(self.drains), 1)
        self.assertFalse(any(c[0] in ('prepare', 'start', 'stop') for c in self.fleet.calls))
        self.ready = True
        self.assertTrue(self.step(job))
        self.assertEqual(self.fx.job()['phase'], 'complete')
        self.assertEqual(self.store.profile(peer['id']), before)
        self.assertEqual(self.fx.fixture.closes, [])
        self.assertEqual([c for c in self.fleet.calls if c[0] == 'start'],
                         [('start', 'fixture-a'), ('start', 'fixture-b')])

    def test_stop_only_verifies_exit_but_never_prepares_or_restarts(self):
        job = self.schedule(stop_only=True)
        self.ready = True
        self.assertTrue(self.step(job))
        self.assertEqual(self.fx.job()['phase'], 'complete')
        self.assertTrue(all(p is None for p in self.fleet.running.values()))
        self.assertFalse(any(c[0] in ('prepare', 'start', 'stop') for c in self.fleet.calls))

    def test_other_remote_is_not_started_until_entire_cohort_exits(self):
        job = self.schedule()
        self.ready = True
        original = self.fleet.request
        def partial(binding, operation, **params):
            if operation == 'drain' and binding['alias'] == 'fixture-b':
                self.ready = False
            return original(binding, operation, **params)
        self.fleet.request = partial
        self.assertFalse(self.step(job))
        self.assertIsNone(self.fleet.running['fixture-a'])
        self.assertIsNotNone(self.fleet.running['fixture-b'])
        self.assertFalse(any(c[0] in ('prepare', 'start') for c in self.fleet.calls))

    def test_new_generation_prevents_signal(self):
        job = self.schedule()
        self.store.mutate(lambda d:self.store.profile(self.profile['id'],d).update(generation=str(uuid4())))
        self.assertTrue(self.step(job))
        self.assertEqual(self.drains, [])
        self.assertEqual(self.fx.job()['phase'], 'attention')

    def test_new_policy_prevents_signal_and_publication(self):
        job = self.schedule()
        self.store.mutate(lambda d:self.store.profile(self.profile['id'],d)['policy'].update(desired_revision=99))
        self.assertTrue(self.step(job))
        self.assertEqual(self.drains, [])
        self.assertEqual(self.fx.job()['phase'], 'attention')

    def test_reused_pid_cannot_be_drained_or_marked_complete(self):
        job = self.schedule()
        self.assertFalse(self.step(job))
        self.fleet.running['fixture-a']['process_start'] = 'different'
        self.ready = True
        self.assertTrue(self.step(job))
        self.assertEqual(self.fx.job()['phase'], 'attention')
        self.assertFalse(any(c[0] == 'prepare' for c in self.fleet.calls))

    def test_lost_reply_keeps_exact_journal_for_explicit_retry(self):
        job = self.schedule()
        self.ready, self.lost = True, True
        self.assertTrue(self.step(job))
        self.assertEqual(self.fx.job()['phase'], 'attention')
        gate = self.fx.gate()
        lease = json.loads(self.hooks._lease_path(gate['transaction_id']).read_text(encoding='utf-8'))
        expected = lease['profiles'][0]['remotes'][0]['process']
        self.assertEqual(lease['profiles'][0]['remotes'][0]['state'], 'drain_requested')
        # Direct step fixtures do not execute _run's final worker cleanup.
        self.fx.restarts.workers.clear()
        next_job = self.schedule()
        self.assertTrue(self.step(next_job))
        self.assertEqual(self.drains[1][1]['expected_process'], expected)
        self.assertEqual(self.fx.job()['phase'], 'complete')

    def test_stop_skips_an_unreachable_host_and_still_drains_the_others(self):
        self.fleet.unreachable = {'fixture-a'}
        job = self.schedule(stop_only=True)
        self.ready = True
        self.assertTrue(self.step(job))
        job = self.fx.job()
        self.assertEqual((job['phase'], job['code']), ('attention', 'ssh_hosts_unreachable'))
        self.assertIn('fixture-a', job['message'])
        self.assertEqual([binding['alias'] for binding, _ in self.drains], ['fixture-b'])
        self.assertIsNone(self.fleet.running['fixture-b'])
        self.assertIsNotNone(self.fleet.running['fixture-a'])
        self.assertEqual(self.fx.gate()['state'], 'released')
        self.assertFalse(any(c[0] in ('prepare', 'start', 'stop') for c in self.fleet.calls))

    def test_open_that_adopts_an_unfinished_stop_reports_unreachable_hosts_not_deferred_settings(self):
        # A full exit started a stop that never finished (the service went away).
        self.fleet.unreachable = {'fixture-a'}
        job = self.schedule(stop_only=True)
        self.assertFalse(self.step(job))
        self.fx.restarts.workers.clear()
        self.ready = True
        self.fx.open()
        self.fx.pending.pop()()
        job = self.fx.job()
        self.assertEqual((job['phase'], job['code'], job.get('connections_restored')),
                         ('attention', 'ssh_hosts_unreachable', True))
        gate = self.fx.gate()
        self.assertEqual(gate['state'], 'released')
        self.assertNotIn('settings_deferred', gate)
        self.assertIsNone(self.fleet.running['fixture-b'])
        self.assertFalse(any(c[0] in ('prepare', 'start') for c in self.fleet.calls))

    def test_unreachable_host_does_not_block_a_settings_apply_on_the_others(self):
        before = {b['alias']: b['revision'] for b in json.loads(self.fx.manifest.read_text())['bindings']}
        self.fleet.unreachable = {'fixture-a'}
        job = self.schedule()
        self.ready = True
        self.assertTrue(self.step(job))
        job = self.fx.job()
        self.assertEqual((job['phase'], job['code'], job['connections_restored']),
                         ('attention', 'ssh_settings_deferred', True))
        self.assertEqual([c for c in self.fleet.calls if c[0] in ('prepare', 'start')],
                         [('prepare', 'fixture-b'), ('start', 'fixture-b')])
        manifest = json.loads(self.fx.manifest.read_text())
        after = {b['alias']: b['revision'] for b in manifest['bindings']}
        self.assertEqual(after['fixture-a'], before['fixture-a'])
        self.assertNotEqual(after['fixture-b'], before['fixture-b'])
        self.assertEqual(manifest['deferred_policy_hosts'], ['fixture-a'])
        # Once reachable, an explicit apply inspects it again and clears the deferral.
        self.fleet.unreachable = set()
        self.fx.restarts.workers.clear()
        job = self.schedule()
        self.assertTrue(self.step(job))
        self.assertEqual(self.fx.job()['phase'], 'complete')
        manifest = json.loads(self.fx.manifest.read_text())
        self.assertEqual(manifest.get('deferred_policy_hosts'), [])
        self.assertEqual(manifest.get('pending_policy_hosts'), [])

    def test_ordinary_open_never_authorizes_drain(self):
        shown = self.fx.open()
        self.step(shown['restart'])
        self.assertEqual(self.drains, [])

    def test_wrong_generation_and_live_competing_job_are_rejected(self):
        with self.assertRaises(UpdateError):
            self.fx.restarts.schedule_remote(self.profile['id'], expected_generation=str(uuid4()))
        job = self.schedule(stop_only=True)
        self.assertEqual(self.schedule(stop_only=True)['id'], job['id'])
        # A pending stop is never turned back into a reopen.
        with self.assertRaises(UpdateError) as raised:
            self.schedule()
        self.assertEqual(raised.exception.code, 'profile_prepare_busy')
        self.assertTrue(self.fx.job()['stop_only'])
        self.assertIsNone(self.fx.gate().get('stop_only'))
        self.assertTrue(self.hooks.downgrade_remote_reconcile_to_stop(self.profile['id'], job['transaction_id']))
        self.ready = True
        self.assertTrue(self.step(job))
        self.assertFalse(any(c[0] in ('prepare', 'start') for c in self.fleet.calls))

    def lease(self, transaction_id):
        return json.loads(self.hooks._lease_path(transaction_id).read_text(encoding='utf-8'))

    def test_full_exit_turns_waiting_reopen_drain_into_verified_stop(self):
        job = self.schedule()
        self.assertFalse(self.step(job))
        stopped = self.schedule(stop_only=True)
        self.assertEqual(stopped['id'], job['id'])
        self.assertTrue(stopped['stop_only'])
        self.assertTrue(self.fx.gate()['stop_only'])
        self.assertEqual(self.fx.gate()['transaction_id'], job['transaction_id'])
        self.ready = True
        self.assertTrue(self.step(job))
        done = self.fx.job()
        self.assertEqual((done['id'], done['phase']), (job['id'], 'complete'))
        self.assertIn('종료를 확인했습니다', done['message'])
        self.assertTrue(all(p is None for p in self.fleet.running.values()))
        self.assertFalse(any(c[0] in ('prepare', 'start', 'stop') for c in self.fleet.calls))
        lease = self.lease(job['transaction_id'])
        self.assertEqual((lease['stop_only'], lease['state']), (True, 'released'))
        self.assertEqual(self.fx.gate()['state'], 'released')
        self.assertNotIn('reopen_committed', self.fx.gate())

    def test_stop_during_reopen_start_drains_the_reopened_listeners(self):
        job = self.schedule()
        self.ready = True
        converted = []
        prepare = self.fleet.prepare
        def prepare_then_full_exit(alias, *args, **kwargs):
            if not converted:
                converted.append(self.schedule(stop_only=True))
            return prepare(alias, *args, **kwargs)
        self.fleet.prepare = prepare_then_full_exit
        self.assertFalse(self.step(job), 'a reopen alone must not complete a stop request')
        self.assertEqual(converted[0]['id'], job['id'])
        self.assertFalse(converted[0]['stop_only'])
        self.assertTrue(self.lease(job['transaction_id']).get('stop_only') is False)
        started = [c for c in self.fleet.calls if c[0] == 'start']
        self.assertEqual(started, [('start', 'fixture-a'), ('start', 'fixture-b')])
        waiting = self.fx.job()
        self.assertEqual((waiting['phase'], waiting['stop_only']), ('waiting', True))
        self.assertNotEqual(waiting['transaction_id'], job['transaction_id'])
        self.assertEqual(self.fx.gate()['transaction_id'], waiting['transaction_id'])
        self.assertTrue(self.lease(waiting['transaction_id'])['stop_only'])
        reopened = {alias: deepcopy(p) for alias, p in self.fleet.running.items()}
        self.assertTrue(all(reopened.values()))
        self.assertTrue(self.step(job))
        done = self.fx.job()
        self.assertEqual((done['id'], done['phase']), (job['id'], 'complete'))
        self.assertTrue(all(p is None for p in self.fleet.running.values()))
        # The second graceful drain targeted exactly the listeners it reopened.
        self.assertEqual({b['alias']: p['expected_process'] for b, p in self.drains[2:]}, reopened)
        self.assertEqual([c for c in self.fleet.calls if c[0] == 'start'], started)
        self.assertEqual(self.fx.gate()['state'], 'released')

    def test_prepared_host_missing_from_inventory_is_still_covered(self):
        self.store.mutate(lambda d:d['ssh_inventory'][self.profile['id']].update(hosts=['fixture-a']))
        job = self.schedule(stop_only=True)
        self.ready = True
        self.assertTrue(self.step(job))
        self.assertEqual({b['alias'] for b,p in self.drains}, {'fixture-a','fixture-b'})


if __name__ == '__main__':
    unittest.main()
