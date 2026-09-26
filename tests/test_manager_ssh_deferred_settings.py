"""Unsupported remote maintenance preserves connections, never claims apply."""
from copy import deepcopy
import json
import unittest
from uuid import uuid4

import test_manager_local_first_remote as local_first
from manager_core.remote_maintenance import retire_deferred_settings_notice
from manager_core.ssh_deferred_settings import resume_previous
from manager_core.store import atomic_json
from manager_core.updates import UpdateError


class DeferredSshTests(unittest.TestCase):
    def setUp(self):
        self.f = local_first.LocalFirstRemoteTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.fleet.idle_evidence = False
        self.f.fleet.observation_code = 'remote_idle_diagnostics_unavailable'
        self.f.open()
        self.path = self.f.hooks._lease_path(self.f.gate()['transaction_id'])
        manifest = json.loads(self.f.manifest.read_text())
        manifest.update(bindings=[], pending_policy_hosts=['fixture-a', 'fixture-b'])
        atomic_json(self.f.manifest, manifest)

    def resume(self):
        return resume_previous(self.f.store, self.f.fleet, self.f.store.profile(self.f.profile['id']),
            json.loads(self.path.read_text()), 'remote_idle_diagnostics_unavailable', journal_path=self.path)

    def test_empty_unsupported_cohort_reconnects_all_hosts_without_policy_promotion(self):
        before = self.f.store.profile(self.f.profile['id'])
        running = deepcopy(self.f.fleet.running)
        self.f.pending.pop()()
        self.assertEqual(self.f.job()['phase'], 'attention')
        self.assertEqual(self.f.job()['code'], 'ssh_settings_deferred')
        self.assertTrue(self.f.job()['connections_restored'])
        self.assertEqual(self.f.gate()['state'], 'released')
        self.assertTrue(self.f.gate()['settings_deferred'])
        self.assertEqual(self.f.store.profile(before['id'])['policy'], before['policy'])
        self.assertEqual(self.f.store.profile(before['id'])['remote_bindings'], before['remote_bindings'])
        self.assertEqual(self.f.fleet.running, running)
        manifest = json.loads(self.f.manifest.read_text())
        self.assertEqual({b['alias'] for b in manifest['bindings']}, {'fixture-a', 'fixture-b'})
        self.assertEqual(manifest['pending_policy_hosts'], ['fixture-a', 'fixture-b'])
        self.assertEqual(manifest['deferred_policy_hosts'], ['fixture-a', 'fixture-b'])
        self.assertTrue(all(op in ('inspect', 'identity') for op, alias in self.f.fleet.calls))
        self.assertEqual(self.f.fixture.closes, [])
        # Released admission lets the native SSH child progress immediately.
        from manager_core.ssh_connection_wait import wait_for_settings
        wait_for_settings(['fixture-a', 'true'], dict(manifest, inventory_root=str(self.f.fixture.root)),
                          store=self.f.store, sleep=lambda _: self.fail('must not poll'))

    def test_partial_lifecycle_journal_is_never_resumed_as_previous_settings(self):
        baseline = json.loads(self.path.read_text())
        for state in ('closed', 'stop_requested', 'prepared', 'start_requested', 'started'):
            lease = deepcopy(baseline)
            lease['profiles'][0]['remotes'][0]['state'] = state
            atomic_json(self.path, lease)
            self.assertFalse(self.resume(), state)
        self.assertEqual(self.f.fleet.calls, [])
        self.assertEqual(self.f.gate()['state'], 'held')

    def test_stopped_or_changed_runtime_never_releases_gate(self):
        self.f.fleet.running['fixture-b'] = None
        before = self.f.manifest.read_bytes()
        with self.assertRaises(UpdateError):
            self.resume()
        self.assertEqual(self.f.manifest.read_bytes(), before)
        self.assertEqual(self.f.gate()['state'], 'held')

    def test_concurrent_policy_generation_manifest_and_gate_changes_preserve_new_state(self):
        for kind in ('policy', 'generation', 'manifest', 'gate', 'journal'):
            with self.subTest(kind=kind):
                f = DeferredSshTests()
                f.setUp()
                try:
                    previous = f.f.fleet.request
                    def change(*args, **kwargs):
                        result = previous(*args, **kwargs)
                        if args[0]['alias'] != 'fixture-b':
                            return result
                        if kind == 'manifest':
                            m = json.loads(f.f.manifest.read_text())
                            m['generation'] = str(uuid4())
                            atomic_json(f.f.manifest, m)
                        elif kind == 'journal':
                            j = json.loads(f.path.read_text())
                            j['profiles'][0]['remotes'][0]['state'] = 'stop_requested'
                            atomic_json(f.path, j)
                        else:
                            def edit(data):
                                p = f.f.store.profile(f.f.profile['id'], data)
                                if kind == 'policy': p['policy']['desired_revision'] += 1
                                elif kind == 'generation': p['generation'] = str(uuid4())
                                else: data['ssh_maintenance'][p['id']]['transaction_id'] = str(uuid4())
                            f.f.store.mutate(edit)
                        return result
                    f.f.fleet.request = change
                    with self.assertRaises(UpdateError):
                        f.resume()
                    self.assertEqual(f.f.gate()['state'], 'held')
                    self.assertEqual(json.loads(f.f.manifest.read_text())['bindings'], [])
                    self.assertTrue(all(op == 'identity' for op, _ in f.f.fleet.calls))
                finally:
                    f.doCleanups()

    def test_runtime_update_keeps_its_strict_lifecycle(self):
        lease = json.loads(self.path.read_text())
        lease['force_runtime_update'] = True
        atomic_json(self.path, lease)
        self.assertFalse(self.resume())
        self.assertEqual(self.f.fleet.calls, [])

    def test_recovery_retires_old_worker_and_cannot_be_undone_by_its_failure(self):
        from recover_ssh_settings import recover
        old_job = self.f.job()
        old_transaction = self.f.gate()['transaction_id']
        result = recover(self.f.store, self.f.fleet, self.f.profile['id'])
        self.assertTrue(result['connections_restored'])
        self.assertNotEqual(self.f.gate()['transaction_id'], old_transaction)
        self.assertTrue(self.f.restarts.step(self.f.profile['id'], old_job['id']))
        self.f.hooks.remote_open_failed(self.f.profile['id'], old_transaction, 'ssh_generation_changed')
        self.assertEqual(self.f.gate()['state'], 'released')
        self.assertTrue(self.f.job()['connections_restored'])
        self.assertEqual(self.f.job()['code'], 'ssh_settings_deferred')
        self.assertTrue(all(op in ('inspect', 'identity') for op, _ in self.f.fleet.calls))

    def test_recovery_never_replaces_a_real_busy_wait(self):
        from recover_ssh_settings import recover
        self.f.fleet.idle_evidence = True
        before = self.f.gate()
        with self.assertRaises(UpdateError) as raised:
            recover(self.f.store, self.f.fleet, self.f.profile['id'])
        self.assertEqual(raised.exception.code, 'ssh_recovery_not_unsupported')
        self.assertEqual(self.f.gate(), before)
        self.assertEqual(json.loads(self.f.manifest.read_text())['bindings'], [])

    def defer(self):
        """Advance the opened local window into its deferred notice state."""
        self.f.pending.pop()()
        self.assertEqual(self.f.job()['phase'], 'attention')
        self.assertEqual(self.f.job()['code'], 'ssh_settings_deferred')
        self.assertTrue(self.f.gate()['settings_deferred'])
        manifest = json.loads(self.f.manifest.read_text())
        self.assertEqual(manifest['deferred_policy_hosts'], ['fixture-a', 'fixture-b'])
        self.assertEqual({b['alias'] for b in manifest['bindings']}, {'fixture-a', 'fixture-b'})
        return manifest

    def started(self, aliases=None, revision='b' * 64):
        """One verified new-settings start per published host."""
        profile = self.f.store.profile(self.f.profile['id'])
        entries = []
        for binding in json.loads(self.f.manifest.read_text())['bindings']:
            if aliases is not None and binding['alias'] not in aliases:
                continue
            entries.append(dict(alias=binding['alias'], binding=deepcopy(binding), state='started',
                next_binding={**binding, 'revision': revision},
                target_policy_revision=profile['policy']['desired_revision'],
                started=dict(process=dict(pid=7, revision=revision), exited=False)))
        return entries

    def publish(self, entries):
        self.f.fleet.publish_started(self.f.store.profile(self.f.profile['id']), entries)

    def test_applied_cohort_retires_the_deferred_notice_without_local_restart(self):
        self.defer()
        before = self.f.store.profile(self.f.profile['id'])
        self.publish(self.started())
        job = self.f.job()
        self.assertEqual(job['phase'], 'complete')
        self.assertNotIn('code', job)
        self.assertNotIn('connections_restored', job)
        gate = self.f.gate()
        self.assertEqual(gate['state'], 'released')
        for field in ('settings_deferred', 'deferred_reason', 'deferred_policy_hosts', 'code', 'message'):
            self.assertNotIn(field, gate)
        manifest = json.loads(self.f.manifest.read_text())
        self.assertEqual(manifest['pending_policy_hosts'], [])
        self.assertEqual(manifest['deferred_policy_hosts'], [])
        self.assertEqual({b['revision'] for b in manifest['bindings']}, {'b' * 64})
        # The publication never promotes policy, rewrites saved bindings or
        # closes the local window the deferred reconnect reopened.
        self.assertEqual(self.f.store.profile(before['id'])['policy'], before['policy'])
        self.assertEqual(self.f.store.profile(before['id'])['remote_bindings'], before['remote_bindings'])
        self.assertEqual(self.f.fixture.closes, [])
        self.assertTrue(all(op in ('inspect', 'identity') for op, _ in self.f.fleet.calls))

    def test_partial_publication_keeps_the_deferred_notice_until_the_last_host(self):
        self.defer()
        self.publish(self.started(aliases={'fixture-a'}))
        self.assertEqual(self.f.job()['code'], 'ssh_settings_deferred')
        self.assertTrue(self.f.gate()['settings_deferred'])
        manifest = json.loads(self.f.manifest.read_text())
        self.assertEqual(manifest['pending_policy_hosts'], ['fixture-b'])
        self.assertEqual(manifest['deferred_policy_hosts'], ['fixture-b'])
        self.publish(self.started(aliases={'fixture-b'}))
        self.assertEqual(self.f.job()['phase'], 'complete')
        self.assertNotIn('settings_deferred', self.f.gate())
        manifest = json.loads(self.f.manifest.read_text())
        self.assertEqual(manifest['pending_policy_hosts'], [])
        self.assertEqual(manifest['deferred_policy_hosts'], [])

    def test_unverified_start_proof_keeps_the_deferred_notice(self):
        self.defer()
        manifest_before = self.f.manifest.read_bytes()
        entries = self.started()
        entries[0]['started']['process']['revision'] = 'c' * 64
        with self.assertRaises(UpdateError) as raised:
            self.publish(entries)
        self.assertEqual(raised.exception.code, 'remote_start_unverified')
        self.assertEqual(self.f.manifest.read_bytes(), manifest_before)
        self.assertEqual(self.f.job()['code'], 'ssh_settings_deferred')
        self.assertTrue(self.f.gate()['settings_deferred'])

    def test_drain_only_or_partial_lease_never_retires_the_deferred_notice(self):
        self.defer()
        drained = self.started()
        for entry in drained:
            entry['state'] = 'drain_requested'
            entry.pop('started')
            entry.pop('next_binding')
        self.publish(drained)
        self.assertEqual(self.f.job()['code'], 'ssh_settings_deferred')
        self.assertEqual(json.loads(self.f.manifest.read_text())['pending_policy_hosts'],
                         ['fixture-a', 'fixture-b'])
        # One verified start is not a cohort: the drained host still waits.
        self.publish(self.started(aliases={'fixture-a'}) + [dict(alias='fixture-b', state='drain_requested')])
        self.assertEqual(self.f.job()['code'], 'ssh_settings_deferred')
        self.assertTrue(self.f.gate()['settings_deferred'])
        manifest = json.loads(self.f.manifest.read_text())
        self.assertEqual(manifest['pending_policy_hosts'], ['fixture-b'])
        self.assertEqual(manifest['deferred_policy_hosts'], ['fixture-b'])

    def test_notice_owned_by_another_deferral_is_left_for_review(self):
        self.defer()
        profile_id = self.f.profile['id']
        # A newer deferral re-owned the gate; the older attention notice is not
        # this publication's to retire.
        self.f.store.mutate(lambda data: data['ssh_maintenance'][profile_id].update(transaction_id=str(uuid4())))
        self.publish(self.started())
        self.assertEqual(self.f.job()['code'], 'ssh_settings_deferred')
        self.assertTrue(self.f.gate()['settings_deferred'])
        self.assertEqual(json.loads(self.f.manifest.read_text())['deferred_policy_hosts'], [])

    def test_notice_for_another_desired_revision_is_left_for_review(self):
        self.defer()
        profile_id = self.f.profile['id']
        newer = self.f.store.profile(profile_id)['policy']['desired_revision'] + 1
        self.f.store.mutate(lambda data: data['ssh_maintenance'][profile_id].update(target_revision=newer))
        self.publish(self.started())
        self.assertEqual(self.f.job()['code'], 'ssh_settings_deferred')
        self.assertTrue(self.f.gate()['settings_deferred'])

    def test_newer_notice_is_left_while_the_applied_deferral_gate_is_retired(self):
        self.defer()
        profile_id = self.f.profile['id']
        self.f.store.mutate(lambda data: data['profile_restarts'][profile_id].update(
            generation=str(uuid4()), phase='waiting', code='ssh_settings_deferred'))
        self.publish(self.started())
        # The released gate this publication proved applied is retired, but a
        # newer notice is not this generation's to clear.
        self.assertNotIn('settings_deferred', self.f.gate())
        notice = self.f.store.read()['profile_restarts'][profile_id]
        self.assertEqual(notice['phase'], 'waiting')
        self.assertEqual(notice['code'], 'ssh_settings_deferred')

    def test_absence_snapshot_never_clears_a_notice_that_appeared_later(self):
        self.defer()
        profile_id = self.f.profile['id']
        profile = self.f.store.profile(profile_id)
        # A publication pinned an absent notice; the deferral notice exists now,
        # so the pairing is unverified and neither notice may be retired.
        self.assertFalse(retire_deferred_settings_notice(self.f.store, profile_id,
            generation=profile['generation'], revision=profile['policy']['desired_revision'],
            gate=self.f.gate(), job=None))
        self.assertEqual(self.f.job()['code'], 'ssh_settings_deferred')
        gate = self.f.gate()
        self.assertTrue(gate['settings_deferred'])
        self.assertEqual(gate['deferred_policy_hosts'], ['fixture-a', 'fixture-b'])


if __name__ == '__main__':
    unittest.main()
