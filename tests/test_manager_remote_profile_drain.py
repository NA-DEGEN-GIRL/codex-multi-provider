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
            lease = json.loads(self.hooks._lease_path(gate['transaction_id']).read_text())
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
        lease = json.loads(self.hooks._lease_path(gate['transaction_id']).read_text())
        expected = lease['profiles'][0]['remotes'][0]['process']
        self.assertEqual(lease['profiles'][0]['remotes'][0]['state'], 'drain_requested')
        # Direct step fixtures do not execute _run's final worker cleanup.
        self.fx.restarts.workers.clear()
        next_job = self.schedule()
        self.assertTrue(self.step(next_job))
        self.assertEqual(self.drains[1][1]['expected_process'], expected)
        self.assertEqual(self.fx.job()['phase'], 'complete')

    def test_ordinary_open_never_authorizes_drain(self):
        shown = self.fx.open()
        self.step(shown['restart'])
        self.assertEqual(self.drains, [])

    def test_wrong_generation_and_live_competing_job_are_rejected(self):
        with self.assertRaises(UpdateError):
            self.fx.restarts.schedule_remote(self.profile['id'], expected_generation=str(uuid4()))
        job = self.schedule()
        self.assertEqual(self.schedule()['id'], job['id'])
        with self.assertRaises(UpdateError):
            self.schedule(stop_only=True)

    def test_prepared_host_missing_from_inventory_is_still_covered(self):
        self.store.mutate(lambda d:d['ssh_inventory'][self.profile['id']].update(hosts=['fixture-a']))
        job = self.schedule(stop_only=True)
        self.ready = True
        self.assertTrue(self.step(job))
        self.assertEqual({b['alias'] for b,p in self.drains}, {'fixture-a','fixture-b'})


if __name__ == '__main__':
    unittest.main()
