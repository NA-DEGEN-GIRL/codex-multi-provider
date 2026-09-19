"""Local window availability is independent of a simulated remote cohort."""
from copy import deepcopy
import json
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_manager_update_hooks as fixtures
from test_manager_remote_restore import Fleet
from manager_core.profile_restart import ProfileRestarts
from manager_core.ssh_shim import validate_binding
from manager_core.store import atomic_json
from manager_core.updates import UpdateError


class LocalFirstRemoteTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.HookFixtures()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store, self.hooks = self.fixture.store, self.fixture.hooks
        self.instances, self.profile = self.fixture.instances, self.fixture.profile
        self.fleet = Fleet(self.fixture.root, self.store, self.profile)
        self.hooks.remote_maintenance = self.fleet
        self.hooks.host_inventory = lambda p: dict(complete=True, generation=p['generation'],
                                                   hosts=['local', 'fixture-a', 'fixture-b'])
        self.manifest = self.store.directory / 'profiles' / self.profile['id'] / 'ssh-bindings.json'
        atomic_json(self.manifest, dict(schema=1, profile_id=self.profile['id'],
            generation=self.profile['generation'], bindings=list(self.fleet.bindings_by_alias.values())))
        def record(data):
            self.store.profile(self.profile['id'], data)['remote_bindings'] = [
                dict(b, prepared=True) for b in self.fleet.bindings_by_alias.values()]
            data['ssh_inventory'] = {self.profile['id']: dict(hosts=['fixture-a', 'fixture-b'])}
        self.store.mutate(record)
        self.instances.close(self.profile)
        original_show = self.instances.show
        def show(profile_id, *, reopen_existing=True):
            existing = profile_id in self.instances.running
            result = original_show(profile_id)
            self.hooks.authorize_restoration_generation(result['profile'])
            if not existing:
                manifest = json.loads(self.manifest.read_text())
                manifest['generation'] = result['profile']['generation']
                manifest['bindings'] = [validate_binding(b, profile_id)
                                        for b in self.store.profile(profile_id).get('remote_bindings', [])
                                        if b.get('prepared') is True]
                atomic_json(self.manifest, manifest)
            result['state'] = 'existing' if existing else 'launched'
            return result
        self.instances.show = show
        self.pending = []
        self.restarts = ProfileRestarts(self.store, self.instances, self.hooks,
                                       spawn=self.pending.append, owner_alive=lambda _: False)

    def open(self):
        return self.restarts.open_local(self.profile['id'])

    def gate(self):
        return self.store.read()['ssh_maintenance'][self.profile['id']]

    def job(self):
        return self.restarts.status()[self.profile['id']]

    def test_unreachable_inspection_cannot_delay_or_close_local_window(self):
        shown = self.open()
        self.assertEqual(shown['state'], 'launched')
        self.assertEqual(self.fleet.calls, [])
        self.assertEqual(self.gate()['generation'], shown['profile']['generation'])
        self.fleet.request = lambda *a, **k: (_ for _ in ()).throw(OSError('private diagnostic'))
        self.pending.pop()()
        self.assertEqual(self.job()['phase'], 'attention')
        self.assertEqual(self.gate()['state'], 'attention')
        self.assertEqual(self.instances.observe(self.store.profile(self.profile['id']))['status'], 'running')
        self.assertEqual(self.fixture.closes, [])
        self.assertNotIn('private diagnostic', json.dumps(self.job()))

    def test_active_remote_waits_while_new_local_work_is_unfenced(self):
        self.open()
        original = self.fleet.request
        self.fleet.request = lambda *a, **k: {**original(*a, **k), 'idle': False}
        self.assertFalse(self.restarts.step(self.profile['id'], self.job()['id']))
        self.assertFalse(any(operation == 'stop' for operation, _ in self.fleet.calls))
        self.assertEqual(self.fixture.admin.calls, [])
        self.hooks.guard_launch(self.profile['id'])
        self.assertEqual(self.gate()['state'], 'held')

    def test_success_publishes_exact_remote_bindings_without_local_restart(self):
        shown = self.open()
        generation = shown['profile']['generation']
        self.pending.pop()()
        self.assertEqual(self.job()['phase'], 'complete')
        self.assertEqual(self.gate()['state'], 'released')
        self.assertEqual(self.store.profile(self.profile['id'])['generation'], generation)
        self.assertEqual(self.instances.show_calls, [self.profile['id']])
        self.assertEqual(self.fixture.closes, [])
        self.assertEqual({b['revision'] for b in json.loads(self.manifest.read_text())['bindings']}, {'b' * 64})

    def test_prepare_failure_preserves_local_and_retry_reprepares_old_saved_binding(self):
        self.open()
        generation = self.store.profile(self.profile['id'])['generation']
        self.fleet.fail_prepare = 'fixture-b'
        self.pending.pop()()
        self.assertEqual(self.gate()['state'], 'attention')
        self.assertTrue(self.instances.running)
        self.fleet.fail_prepare = None
        self.open()
        self.pending.pop()()
        self.assertEqual(self.gate()['state'], 'released')
        self.assertEqual(self.store.profile(self.profile['id'])['generation'], generation)
        self.assertEqual(self.fleet.calls.count(('prepare', 'fixture-a')), 2)
        self.assertEqual(self.fixture.closes, [])

    def test_closed_local_old_scoped_lease_is_adopted_without_network(self):
        snapshot = self.hooks.snapshot_instances(profile_ids=[self.profile['id']])
        lease = self.hooks.acquire_maintenance(snapshot, transaction_id=str(uuid4()), profile_scope=[self.profile['id']])
        self.assertTrue(self.hooks.close_instance(snapshot[0]))
        self.fleet.fail_prepare = 'fixture-b'
        with self.assertRaises(OSError):
            self.hooks.restore_instance(dict(profile_id=self.profile['id'], remote_only=True))
        self.fleet.calls.clear()
        self.open()
        self.assertEqual(self.fleet.calls, [])
        self.assertEqual(self.store.read()['profile_maintenance'][self.profile['id']]['adopted_by'], self.gate()['transaction_id'])
        self.fleet.fail_prepare = None
        self.pending.pop()()
        self.assertEqual(self.gate()['state'], 'released')

    def test_global_maintenance_is_never_bypassed(self):
        self.store.mutate(lambda d: d.update(update_maintenance=dict(state='held', transaction_id=str(uuid4()))))
        with self.assertRaises(UpdateError):
            self.open()
        self.assertEqual(self.instances.show_calls, [])
        self.assertEqual(self.fleet.calls, [])

    def test_new_generation_cancels_background_before_any_remote_mutation(self):
        self.open()
        self.store.mutate(lambda d: self.store.profile(self.profile['id'], d).update(generation=str(uuid4())))
        self.pending.pop()()
        self.assertEqual(self.job()['code'], 'ssh_generation_changed')
        self.assertEqual(self.fleet.calls, [])
        self.assertEqual(self.fixture.closes, [])

    def test_full_restart_cannot_take_over_ssh_background_gate(self):
        self.open()
        with self.assertRaises(UpdateError):
            self.hooks._begin_global(str(uuid4()), profile_scope=[self.profile['id']])

    def test_status_does_not_wait_for_local_window_launch(self):
        entered, release, returned = threading.Event(), threading.Event(), threading.Event()
        original = self.instances.show
        failures = []
        def slow_show(*args, **kwargs):
            entered.set()
            release.wait(5)
            return original(*args, **kwargs)
        def launch():
            try:
                self.open()
            except Exception as error:
                failures.append(error)
        self.instances.show = slow_show
        worker = threading.Thread(target=launch)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            reader = threading.Thread(target=lambda: (self.restarts.status(), returned.set()))
            reader.start()
            self.assertTrue(returned.wait(1), 'status was blocked by local window creation')
        finally:
            release.set()
            worker.join(5)
            if 'reader' in locals():
                reader.join(5)
        self.assertEqual(failures, [])

    def test_active_background_click_only_observes_existing_window(self):
        self.open()
        self.instances.show = lambda *a, **k: self.fail('active local window must not reacquire launch admission')
        shown = self.open()
        self.assertEqual(shown['state'], 'existing')
        self.assertEqual(len(self.pending), 1)

    def test_shutdown_marks_ssh_gate_attention_without_network_or_local_close(self):
        self.open()
        self.restarts.shutdown()
        self.pending.pop()()
        self.assertEqual(self.gate()['state'], 'attention')
        self.assertEqual(self.gate()['code'], 'service_stopped')
        self.assertEqual(self.fleet.calls, [])
        self.assertEqual(self.fixture.closes, [])

    def test_generation_changed_during_stop_preserves_proof_and_never_stops_next_host(self):
        self.open()
        original = self.fleet.request
        def changed(binding, operation, **params):
            result = original(binding, operation, **params)
            if operation == 'stop':
                self.store.mutate(lambda d: self.store.profile(self.profile['id'], d).update(generation=str(uuid4())))
            return result
        self.fleet.request = changed
        self.pending.pop()()
        self.assertEqual(self.job()['code'], 'ssh_generation_changed')
        self.assertEqual([alias for operation, alias in self.fleet.calls if operation == 'stop'], ['fixture-a'])
        lease = json.loads(self.hooks._lease_path(self.gate()['transaction_id']).read_text())
        self.assertEqual(lease['profiles'][0]['remotes'][0]['state'], 'closed')
        self.assertTrue(lease['profiles'][0]['remotes'][0]['exit_proof']['exited'])
        self.assertFalse(any(operation in ('prepare', 'start') for operation, _ in self.fleet.calls))

    def failed_legacy_preparation(self):
        snapshot = self.hooks.snapshot_instances(profile_ids=[self.profile['id']])
        self.hooks.acquire_maintenance(snapshot, transaction_id=str(uuid4()), profile_scope=[self.profile['id']])
        self.assertTrue(self.hooks.close_instance(snapshot[0]))
        self.fleet.fail_prepare = 'fixture-b'
        with self.assertRaises(OSError):
            self.hooks.restore_instance(dict(profile_id=self.profile['id'], remote_only=True))
        self.fleet.fail_prepare = None
        old_prepare = self.fleet.prepare
        self.fleet.prepare = lambda *a, **k: dict(old_prepare(*a, **k), revision='c' * 64)

    def test_failed_legacy_binding_can_publish_fresh_helper_after_local_manifest_regeneration(self):
        self.failed_legacy_preparation()
        self.open()
        before = {b['alias']: b['revision'] for b in json.loads(self.manifest.read_text())['bindings']}
        self.assertEqual(before, {'fixture-a': 'b' * 64, 'fixture-b': 'a' * 64})
        self.pending.pop()()
        self.assertEqual(self.job()['phase'], 'complete')
        self.assertEqual(self.gate()['state'], 'released')
        self.assertEqual({b['revision'] for b in json.loads(self.manifest.read_text())['bindings']}, {'c' * 64})

    def test_later_manifest_change_is_not_overwritten_by_recovered_legacy_binding(self):
        self.failed_legacy_preparation()
        self.open()
        changed = json.loads(self.manifest.read_text())
        changed['bindings'][0]['revision'] = 'd' * 64
        atomic_json(self.manifest, changed)
        self.pending.pop()()
        self.assertEqual(self.job()['code'], 'remote_binding_changed')
        self.assertEqual(json.loads(self.manifest.read_text()), changed)
        self.assertEqual(self.gate()['state'], 'attention')


if __name__ == '__main__':
    unittest.main()
