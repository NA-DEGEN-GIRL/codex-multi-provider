"""Real maintenance hooks/journals/manifests with a simulated two-host SSH fleet."""
from copy import deepcopy
import json
import unittest
from uuid import uuid4

import test_manager_update_hooks as fixtures
from manager_core.remote_maintenance import RemoteMaintenance
from manager_core.store import atomic_json
from manager_core.updates import UpdateError


class Fleet(RemoteMaintenance):
    def __init__(self, root, store, profile):
        super().__init__(root, store, self)
        self.calls = []
        self.fail_prepare = self.lose_start = self.empty_start = None
        self.next_pid = 100
        # Shared-catalog listeners publish no idle inventory. They still answer
        # observation-only identity, exactly like the typed remote helper.
        self.idle_evidence = True
        self.settings_evidence = True
        self.observation_code = 'remote_idle_status_unavailable'
        self.runtime_bundle = 'fixture-bundle-0123456789abcdef'
        self.host_identity = 'f' * 64
        self.bindings_by_alias = {alias: self.binding(profile, alias, 'a' * 64)
                                  for alias in ('fixture-a', 'fixture-b')}
        self.running = {alias: self.process(binding) for alias, binding in self.bindings_by_alias.items()}

    @staticmethod
    def binding(profile, alias, revision):
        return dict(profile_id=profile['id'], alias=alias, revision=revision,
                    remote_launcher='/home/test/.local/share/codex-control-center/profiles/' + profile['id'] + '/launch.py',
                    remote_python='/usr/bin/python3')

    def process(self, binding):
        self.next_pid += 1
        return dict(pid=self.next_pid, process_start=str(self.next_pid), boot_id='fixture-boot',
                    revision=binding['revision'], socket='/private/fixture.sock')

    def prepare(self, alias, profile_id, home, model_ids, **options):
        self.calls.append(('prepare', alias))
        self.prepared_options = options
        if alias == self.fail_prepare:
            raise OSError('fixture preparation offline')
        profile = self.store.profile(profile_id)
        revision = ('b' if profile['policy']['desired_revision'] == 0 else 'c') * 64
        return dict(self.binding(profile, alias, revision), prepared=True, model_ids=model_ids)

    def request(self, binding, operation, **params):
        alias = binding['alias']
        self.calls.append((operation, alias))
        process = self.running.get(alias)
        active = None
        if process is not None and process['revision'] != binding['revision']:
            if operation == 'inspect' and params.get('discover_active') is True:
                active = dict(binding, revision=process['revision'])
            else:
                raise UpdateError('remote_revision_conflict', 'fixture daemon is running a different revision')
        if process is not None and operation == 'inspect' and not self.idle_evidence:
            if params.get('observe_only') is not True:
                raise UpdateError(self.observation_code, 'fixture listener publishes no idle inventory')
            return dict(binding=deepcopy(binding), process=deepcopy(process), idle=False, exited=False,
                        revision=process['revision'], requested_revision=binding['revision'],
                        runtime_bundle=self.runtime_bundle, host_identity=self.host_identity,
                        observation_code=self.observation_code,
                        **({'active_binding': active} if active else {}))
        if operation == 'stop':
            if params['expected_process'] != process:
                raise UpdateError('fixture_process_mismatch', 'fixture stop identity changed')
            self.running[alias] = process = None
        elif operation == 'start':
            if alias != self.empty_start:
                self.running[alias] = process = process or self.process(binding)
            if alias == self.lose_start:
                self.lose_start = None
                raise OSError('fixture start reply lost after process creation')
        result = dict(binding=deepcopy(binding), process=deepcopy(process), idle=True, exited=process is None,
                      **({'active_binding': active} if active else {}))
        if 'expected_settings' in params:
            result['settings_match'] = self.settings_evidence
        return result


class RemoteRestoreTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.HookFixtures()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.hooks, self.store, self.profile = self.fixture.hooks, self.fixture.store, self.fixture.profile
        self.fleet = Fleet(self.fixture.root, self.store, self.profile)
        self.hooks.remote_maintenance = self.fleet
        self.hooks.host_inventory = lambda p: dict(complete=True, generation=p['generation'],
                                                   hosts=['local', 'fixture-a', 'fixture-b'])
        self.manifest = self.store.directory / 'profiles' / self.profile['id'] / 'ssh-bindings.json'
        atomic_json(self.manifest, dict(schema=1, profile_id=self.profile['id'], generation=self.profile['generation'],
                                       bindings=list(self.fleet.bindings_by_alias.values())))
        self.fixture.instances.close(self.profile)
        self.peer = self.store.add_profile('unaffected peer')

    def close(self):
        snapshot = self.hooks.snapshot_instances(profile_ids=[self.profile['id']])
        self.assertTrue(snapshot[0]['idle_verified'], snapshot)
        lease = self.hooks.acquire_maintenance(snapshot, transaction_id=str(uuid4()), profile_scope=[self.profile['id']])
        self.assertTrue(self.hooks.close_instance(snapshot[0]))
        return lease

    def restore(self):
        return self.hooks.restore_instance(dict(profile_id=self.profile['id'], remote_only=True))

    def records(self, lease):
        return json.loads(self.hooks._lease_path(lease['transaction_id']).read_text())['profiles'][0]['remotes']

    def test_preparation_failure_cannot_start_only_the_first_host(self):
        lease = self.close()
        self.fleet.fail_prepare = 'fixture-b'
        with self.assertRaises(OSError):
            self.restore()
        self.assertEqual([r['state'] for r in self.records(lease)], ['prepared', 'closed'])
        self.assertFalse(any(call[0] == 'start' for call in self.fleet.calls))
        self.assertTrue(all(process is None for process in self.fleet.running.values()))
        self.hooks.guard_launch(self.peer['id'])
        # Same transaction resumes preparation without redoing completed work.
        self.fleet.fail_prepare = None
        self.assertTrue(self.restore()['verified'])
        self.assertEqual(self.fleet.calls.count(('prepare', 'fixture-a')), 1)
        self.hooks.release_maintenance(lease)

    def test_late_preparation_for_old_generation_does_not_publish_or_start(self):
        lease = self.close()
        prepare = self.fleet.prepare
        def changed(*args, **kwargs):
            result = prepare(*args, **kwargs)
            self.store.mutate(lambda data: self.store.profile(self.profile['id'], data).update(generation=str(uuid4())))
            return result
        self.fleet.prepare = changed
        before = self.store.profile(self.profile['id']).get('remote_bindings')
        with self.assertRaises(UpdateError) as raised:
            self.restore()
        self.assertEqual(raised.exception.code, 'remote_generation_changed')
        self.assertEqual(self.store.profile(self.profile['id']).get('remote_bindings'), before)
        self.assertFalse(any(call[0] == 'start' for call in self.fleet.calls))

    def test_policy_edit_after_start_blocks_manifest_publication(self):
        lease = self.close()
        before = self.manifest.read_bytes()
        self.assertTrue(self.restore()['verified'])
        self.manifest.write_bytes(before)  # Simulate a result before publication.
        self.store.mutate(lambda data: self.store.profile(self.profile['id'], data)['policy'].update(desired_revision=1))
        with self.assertRaises(UpdateError) as raised:
            self.fleet.publish_started(self.store.profile(self.profile['id']), self.records(lease))
        self.assertEqual(raised.exception.code, 'policy_changed')
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_generation_change_during_first_start_keeps_proof_and_cancels_remaining_starts(self):
        lease = self.close()
        entries = self.records(lease)
        journals = []
        request = self.fleet.request
        def changed(binding, operation, **params):
            result = request(binding, operation, **params)
            if operation == 'start':
                self.store.mutate(lambda data: self.store.profile(self.profile['id'], data).update(generation=str(uuid4())))
            return result
        self.fleet.request = changed
        def current():
            if self.store.profile(self.profile['id'])['generation'] != self.profile['generation']:
                raise UpdateError('ssh_generation_changed', 'The selected launch changed.')
        with self.assertRaises(UpdateError) as raised:
            self.fleet.prepare_and_start(self.profile, entries,
                lambda: journals.append(deepcopy(entries)), lifecycle_guard=current)
        self.assertEqual(raised.exception.code, 'ssh_generation_changed')
        self.assertEqual([entry['state'] for entry in journals[-1]], ['started', 'prepared'])
        self.assertEqual(journals[-1][0]['started']['process'], self.fleet.running['fixture-a'])
        self.assertEqual([call for call in self.fleet.calls if call[0] == 'start'], [('start', 'fixture-a')])
        self.assertIsNone(self.fleet.running['fixture-b'])

    def test_gate_change_during_prepare_cannot_publish_or_start_old_worker_result(self):
        lease = self.close()
        entries = self.records(lease)
        admitted = True
        prepare = self.fleet.prepare
        def changed(*args, **kwargs):
            nonlocal admitted
            result = prepare(*args, **kwargs)
            admitted = False
            return result
        self.fleet.prepare = changed
        def current():
            if not admitted:
                raise UpdateError('ssh_generation_changed', 'The transaction changed.')
        before = self.store.profile(self.profile['id']).get('remote_bindings')
        with self.assertRaises(UpdateError):
            self.fleet.prepare_and_start(self.profile, entries, lambda: None, lifecycle_guard=current)
        self.assertEqual(self.store.profile(self.profile['id']).get('remote_bindings'), before)
        self.assertEqual([entry['state'] for entry in entries], ['closed', 'closed'])
        self.assertEqual([call for call in self.fleet.calls if call[0] == 'prepare'], [('prepare', 'fixture-a')])
        self.assertFalse(any(call[0] == 'start' for call in self.fleet.calls))

    def test_closed_local_profile_with_older_listeners_can_complete_normal_restart(self):
        for process in self.fleet.running.values():
            process['revision'] = '0' * 64
        original = deepcopy(self.fleet.running)
        lease = self.close()
        records = self.records(lease)
        for record in records:
            self.assertEqual(record['binding']['revision'], 'a' * 64)
            self.assertEqual(record['active_binding']['revision'], '0' * 64)
            self.assertEqual(record['process'], original[record['binding']['alias']])
        self.assertTrue(self.restore()['verified'])
        self.hooks.release_maintenance(lease)
        self.assertEqual({b['revision'] for b in json.loads(self.manifest.read_text())['bindings']}, {'b' * 64})
        self.hooks.guard_launch(self.profile['id'])

    def test_pending_policy_binding_can_be_restarted_and_published(self):
        self.store.mutate(lambda data: self.store.profile(self.profile['id'], data).update(
            remote_bindings=[dict(b, prepared=True) for b in self.fleet.bindings_by_alias.values()]))
        manifest = json.loads(self.manifest.read_text())
        manifest['bindings'] = manifest['bindings'][:1]
        manifest['pending_policy_hosts'] = ['fixture-b']
        atomic_json(self.manifest, manifest)
        lease = self.close()
        self.assertTrue(self.restore()['verified'])
        self.hooks.release_maintenance(lease)
        applied = json.loads(self.manifest.read_text())
        self.assertEqual({b['alias'] for b in applied['bindings']}, {'fixture-a', 'fixture-b'})
        self.assertEqual(applied['pending_policy_hosts'], [])

    def test_restart_forwards_external_only_model_options(self):
        self.store.mutate(lambda data: self.store.profile(self.profile['id'], data)['policy'].update(
            selection_mode='external_only'))
        lease = self.close()
        self.assertTrue(self.restore()['verified'])
        self.assertEqual(self.fleet.prepared_options, {'selection_mode': 'external_only'})
        self.hooks.release_maintenance(lease)

    def test_lost_second_start_reply_is_reconciled_without_restarting_either_host(self):
        lease = self.close()
        self.fleet.lose_start = 'fixture-b'
        with self.assertRaises(OSError):
            self.restore()
        before = deepcopy(self.fleet.running)
        self.assertEqual([r['state'] for r in self.records(lease)], ['started', 'start_requested'])
        self.assertTrue(self.restore()['verified'])
        self.assertEqual(self.fleet.running, before)
        self.assertEqual([c for c in self.fleet.calls if c[0] == 'start'],
                         [('start', 'fixture-a'), ('start', 'fixture-b')])
        self.hooks.release_maintenance(lease)
        self.hooks.guard_launch(self.profile['id'])

    def test_applied_manifest_allows_a_second_settings_cycle(self):
        lease = self.close()
        self.assertTrue(self.restore()['verified'])
        self.hooks.release_maintenance(lease)
        first = deepcopy(self.fleet.running)
        stored = json.loads(self.manifest.read_text())
        self.assertEqual({b['revision'] for b in stored['bindings']}, {'b' * 64})
        self.store.mutate(lambda data: self.store.profile(self.profile['id'], data)['policy'].update(desired_revision=1))
        lease = self.close()
        self.assertTrue(self.restore()['verified'])
        self.hooks.release_maintenance(lease)
        self.assertNotEqual(self.fleet.running, first)
        stored = json.loads(self.manifest.read_text())
        self.assertEqual({b['revision'] for b in stored['bindings']}, {'c' * 64})
        self.assertEqual(stored['generation'], self.profile['generation'])
        self.assertFalse(self.fixture.instances.show_calls)

    def test_absent_process_after_start_is_not_success(self):
        lease = self.close()
        self.fleet.empty_start = 'fixture-b'
        with self.assertRaisesRegex(UpdateError, 'SSH'):
            self.restore()
        self.assertEqual([r['state'] for r in self.records(lease)], ['started', 'start_requested'])
        with self.assertRaises(UpdateError):
            self.hooks.release_maintenance(lease)
        with self.assertRaises(UpdateError):
            self.restore()
        self.assertEqual(self.fleet.calls.count(('start', 'fixture-b')), 1)
        self.hooks.guard_launch(self.peer['id'])

    def test_reconciliation_publishes_lost_start_before_releasing_gate(self):
        lease = self.close()
        self.fleet.lose_start = 'fixture-b'
        with self.assertRaises(OSError):
            self.restore()
        self.hooks.release_maintenance(lease)
        self.assertEqual({b['revision'] for b in json.loads(self.manifest.read_text())['bindings']}, {'b' * 64})
        snapshot = self.hooks.snapshot_instances(profile_ids=[self.profile['id']])
        self.assertTrue(snapshot[0]['idle_verified'], snapshot)
        self.assertEqual(self.fleet.calls.count(('start', 'fixture-b')), 1)

    def test_manifest_change_is_preserved_and_prevents_gate_release(self):
        lease = self.close()
        self.assertTrue(self.restore()['verified'])
        manifest = json.loads(self.manifest.read_text())
        manifest['bindings'][0]['revision'] = 'f' * 64
        atomic_json(self.manifest, manifest)
        with self.assertRaises(UpdateError):
            self.hooks.release_maintenance(lease)
        self.assertEqual(json.loads(self.manifest.read_text()), manifest)
        self.hooks.guard_launch(self.peer['id'])


if __name__ == '__main__':
    unittest.main()
