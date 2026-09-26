"""Local window availability is independent of a simulated remote cohort."""
from copy import deepcopy
import json
import threading
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

import test_manager_update_hooks as fixtures
from test_manager_remote_restore import Fleet
from manager_core.profile_restart import ProfileRestarts
from manager_core.ssh_inventory import SshInventory
from manager_core.ssh_shim import validate_binding
from manager_core.store import Store, atomic_json
from manager_core.updates import UpdateError, _lock_file, _unlock_file


class LocalFirstRemoteTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.HookFixtures()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store, self.hooks = self.fixture.store, self.fixture.hooks
        self.instances, self.profile = self.fixture.instances, self.fixture.profile
        self.fleet = Fleet(self.fixture.root, self.store, self.profile)
        # Existing tests exercise a genuine remote-input change unless opted in.
        self.fleet.binding_matches_settings = lambda profile, binding: False
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
        def show(profile_id, *, reopen_existing=True, wait_for_window=True):
            self.assertFalse(wait_for_window, 'window wait must be outside the SSH publication fence')
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
        self.instances.finish_show = lambda result: result
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

    def test_window_wait_runs_after_ssh_publication_and_global_unlock(self):
        def finish(result):
            lock = _lock_file(self.hooks.directory / 'launch-admission.lock')
            _unlock_file(lock)
            self.assertEqual(self.hooks._admission.depth, 0)
            self.assertEqual(self.gate()['generation'], result['profile']['generation'])
            return result
        self.instances.finish_show = finish
        self.assertEqual(self.open()['state'], 'launched')

    def test_same_service_queue_does_not_consume_external_lock_timeout(self):
        waiting, entered, release = threading.Event(), threading.Event(), threading.Event()
        errors = []
        clock = [0]
        self.hooks.clock = lambda: clock[0]
        acquire = self.hooks._acquire_launch_admission_lock
        def queued(*args):
            waiting.set()
            return acquire(*args)
        def second():
            try:
                with self.hooks.launch_admission(self.profile['id']):
                    entered.set()
            except Exception as error:
                errors.append(error)
            finally:
                release.set()
        worker = threading.Thread(target=second)
        with self.hooks.launch_admission(self.profile['id']):
            with patch.object(self.hooks, '_acquire_launch_admission_lock', side_effect=queued):
                worker.start()
                self.assertTrue(waiting.wait(2))
                clock[0] = 100
                self.assertFalse(entered.is_set())
        self.assertTrue(release.wait(2))
        worker.join(2)
        self.assertEqual(errors, [])
        self.assertTrue(entered.is_set())

    def test_remote_host_snapshot_and_gate_exclude_a_late_ssh_enrollment(self):
        inventory = SshInventory(self.fixture.root, identity=lambda _: {})
        inventory.prepare(self.profile['id'], self.profile['generation'])
        snapshot_saved, publish_gate, attempted = (threading.Event() for _ in range(3))
        errors, admitted, shown = [], [], []
        save_lease = self.hooks._save_lease
        mutate = inventory.store.mutate

        def pause_after_snapshot(lease):
            save_lease(lease)
            if not snapshot_saved.is_set():
                snapshot_saved.set()
                if not publish_gate.wait(3):
                    raise TimeoutError('fixture did not release gate publication')

        def observe_attempt(operation):
            attempted.set()
            return mutate(operation)

        def open_local():
            try:
                shown.append(self.hooks.open_local_for_remote_reconcile(self.profile['id']))
            except Exception as error:
                errors.append(('open', error))

        def connect_late():
            try:
                with inventory.execution(self.profile['id'], self.profile['generation'],
                                         dict(operation='native-start', alias='fixture-late')):
                    admitted.append(True)
            except Exception as error:
                errors.append(('ssh', error))

        # A host admitted before the fence must appear even while its local
        # command is still active. A competing new host must be fenced before
        # its execution body runs, rather than omitted from the saved cohort.
        with inventory.execution(self.profile['id'], self.profile['generation'],
                                 dict(operation='native-start', alias='fixture-early')):
            opener = threading.Thread(target=open_local)
            connector = threading.Thread(target=connect_late)
            try:
                with patch.object(self.hooks, '_save_lease', side_effect=pause_after_snapshot), \
                     patch.object(inventory.store, 'mutate', side_effect=observe_attempt):
                    opener.start()
                    self.assertTrue(snapshot_saved.wait(2))
                    probe = None
                    try:
                        # Prove the snapshot-to-gate interval excludes a
                        # separate file handle, without a scheduling race.
                        with self.assertRaises(UpdateError):
                            probe = _lock_file(self.store.directory / 'state.lock')
                    finally:
                        if probe is not None:
                            _unlock_file(probe)
                    connector.start()
                    self.assertTrue(attempted.wait(2))
                    publish_gate.set()
                    opener.join(3)
                    connector.join(3)
            finally:
                publish_gate.set()
                for worker in (opener, connector):
                    if worker.ident is not None:
                        worker.join(3)
            self.assertFalse(opener.is_alive() or connector.is_alive())
            self.assertEqual(admitted, [])
            self.assertEqual(len(shown), 1)
            self.assertEqual(len(errors), 1)
            source, error = errors[0]
            self.assertEqual(source, 'ssh')
            self.assertIsInstance(error, UpdateError)
            self.assertEqual(error.code, 'ssh_settings_pending')
            lease = json.loads(self.hooks._lease_path(shown[0][1]).read_text())
            aliases = {record['alias'] for record in lease['profiles'][0]['remotes']}
            self.assertEqual(aliases, {'fixture-a', 'fixture-b', 'fixture-early'})
            stored = self.store.read()['ssh_inventory'][self.profile['id']]
            self.assertEqual(set(stored['hosts']), aliases)
            self.assertEqual(self.fleet.calls, [])

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

    def unchanged_live_fleet(self):
        self.fleet.binding_matches_settings = lambda profile, binding: True
        original = self.fleet.request
        def identity_only(binding, operation, **params):
            self.assertEqual(operation, 'identity', 'unchanged active or ephemeral work must not enter idle/shutdown')
            return {**original(binding, operation, **params), 'idle': False}
        self.fleet.request = identity_only

    def test_unchanged_active_fleet_releases_gate_without_restart_or_idle_probe(self):
        self.unchanged_live_fleet()
        processes = deepcopy(self.fleet.running)
        shown = self.open()
        manifest = self.manifest.read_bytes()
        self.pending.pop()()
        self.assertEqual(self.job()['phase'], 'complete')
        self.assertEqual(self.gate()['state'], 'released')
        self.assertEqual(self.fleet.calls, [('identity', 'fixture-a'), ('identity', 'fixture-b')])
        self.assertEqual(self.fleet.running, processes)
        self.assertEqual(self.manifest.read_bytes(), manifest)
        self.assertEqual(self.store.profile(self.profile['id'])['generation'], shown['profile']['generation'])
        self.assertEqual(self.fixture.admin.calls, [])
        self.assertEqual(self.fixture.closes, [])
        lease = json.loads(self.hooks._lease_path(self.gate()['transaction_id']).read_text())
        self.assertIs(lease['reused_unchanged'], True)

    def test_unchanged_preflight_attention_can_retry_without_touching_existing_work(self):
        self.unchanged_live_fleet()
        original = self.fleet.request
        self.fleet.request = lambda *a, **k: (_ for _ in ()).throw(OSError('identity unavailable'))
        self.open()
        self.pending.pop()()
        self.assertEqual(self.gate()['state'], 'attention')
        self.fleet.request = original
        self.open()
        self.pending.pop()()
        self.assertEqual(self.gate()['state'], 'released')
        self.assertEqual(self.fleet.calls, [('identity', 'fixture-a'), ('identity', 'fixture-b')])
        self.assertEqual(self.fixture.closes, [])

    def equivalent_live_cohort(self, *, settings_match=True, live_revision='0' * 64):
        """A cohort whose live listeners publish no idle inventory."""
        self.fleet.idle_evidence = False
        self.fleet.settings_evidence = settings_match
        self.fleet.settings_files = lambda profile, binding: {'config.toml': 'c' * 64}
        self.fleet._settings_fingerprint = lambda binding, files: 'e' * 64
        extras = dict(runtime_bundle=self.fleet.runtime_bundle, host_identity=self.fleet.host_identity)
        for binding in self.fleet.bindings_by_alias.values():
            binding.update(extras)
        self.store.mutate(lambda data: self.store.profile(self.profile['id'], data).update(
            remote_bindings=[dict(binding, prepared=True) for binding in self.fleet.bindings_by_alias.values()]))
        for process in self.fleet.running.values():
            process['revision'] = live_revision

    def test_equivalent_live_revisions_release_the_gate_without_stopping_their_work(self):
        self.equivalent_live_cohort()
        live = deepcopy(self.fleet.running)
        shown = self.open()
        self.assertEqual(self.fleet.calls, [])
        self.pending.pop()()
        self.assertEqual(self.job()['phase'], 'complete')
        self.assertEqual(self.gate()['state'], 'released')
        self.assertEqual(self.fixture.closes, [])
        self.assertEqual(self.fleet.running, live)
        self.assertEqual([call for call in self.fleet.calls if call[0] not in ('inspect', 'identity')], [])
        self.assertEqual(self.instances.show_calls, [self.profile['id']])
        self.assertEqual(self.store.profile(self.profile['id'])['generation'], shown['profile']['generation'])
        stored = {b['alias']: b for b in self.store.profile(self.profile['id'])['remote_bindings']}
        self.assertEqual({b['revision'] for b in stored.values()}, {'0' * 64})
        self.assertTrue(all('settings_fingerprint' in b for b in stored.values()))
        self.assertEqual({b['revision'] for b in json.loads(self.manifest.read_text())['bindings']}, {'0' * 64})
        lease = json.loads(self.hooks._lease_path(self.gate()['transaction_id']).read_text())
        self.assertEqual(lease['reused_revisions'], {'fixture-a': '0' * 64, 'fixture-b': '0' * 64})

    def test_changed_settings_keep_the_strict_path_and_never_touch_live_work(self):
        self.equivalent_live_cohort(settings_match=False)
        live = deepcopy(self.fleet.running)
        self.open()
        manifest = self.manifest.read_bytes()
        saved = deepcopy(self.store.profile(self.profile['id'])['remote_bindings'])
        self.pending.pop()()
        self.assertEqual(self.job()['code'], 'remote_idle_status_unavailable')
        self.assertEqual(self.gate()['state'], 'attention')
        self.assertEqual(self.fleet.running, live)
        self.assertEqual(self.fixture.closes, [])
        self.assertEqual(self.store.profile(self.profile['id'])['remote_bindings'], saved)
        self.assertEqual(self.manifest.read_bytes(), manifest)
        self.assertEqual([call for call in self.fleet.calls if call[0] not in ('inspect', 'identity')], [])

    def test_bounded_repair_api_releases_only_the_verified_cohort_gate(self):
        self.equivalent_live_cohort()
        shown = self.open()
        generation = shown['profile']['generation']
        transaction = self.gate()['transaction_id']
        live = deepcopy(self.fleet.running)
        self.assertEqual(self.gate()['state'], 'held')
        # A journal that drifted from this generation's publication is refused
        # before any write; the repair cohort is rebuilt from the manifest.
        stale = [dict(record, binding=dict(record['binding'], revision='d' * 64))
                 for record in json.loads(self.hooks._lease_path(transaction).read_text())
                 ['profiles'][0]['remotes']]
        profile = self.store.profile(self.profile['id'])
        self.assertIsNone(self.fleet.reuse_equivalent(profile, stale))
        published = json.loads(self.manifest.read_text())['bindings']
        records = [dict(binding=dict(binding), alias=binding['alias'], state='unobserved')
                   for binding in published]
        adopted = self.fleet.reuse_equivalent(profile, records)
        self.assertEqual(adopted, {'fixture-a': '0' * 64, 'fixture-b': '0' * 64})
        def release(data):
            gate = data['ssh_maintenance'][self.profile['id']]
            if gate.get('transaction_id') != transaction or gate.get('generation') != generation:
                raise UpdateError('ssh_generation_changed', 'repair scope changed')
            gate.update(state='released')
        self.store.mutate(release)
        self.assertEqual(self.gate()['state'], 'released')
        self.assertEqual(self.fixture.closes, [])
        self.assertEqual(self.fleet.running, live)
        self.assertEqual(self.instances.show_calls, [self.profile['id']])
        self.assertEqual({b['revision'] for b in json.loads(self.manifest.read_text())['bindings']}, {'0' * 64})

    def test_reopened_retired_journal_rebuilds_the_cohort_from_current_bindings(self):
        transaction = str(uuid4())
        records = [dict(binding=dict(binding, revision='d' * 64), alias=alias, state='unobserved')
                   for alias, binding in sorted(self.fleet.bindings_by_alias.items())]
        atomic_json(self.hooks._lease_path(transaction), dict(
            transaction_id=transaction, ssh_only=True, state='released', profile_scope=[self.profile['id']],
            target_revision=self.profile['policy']['desired_revision'],
            profiles=[dict(profile_id=self.profile['id'], generation=str(uuid4()), remote_only=True,
                           state='released', remotes=records)]))
        self.store.mutate(lambda data: data.setdefault('ssh_maintenance', {}).update({self.profile['id']: dict(
            state='attention', transaction_id=transaction, generation=self.profile['generation'],
            remote_update=False, target_revision=self.profile['policy']['desired_revision'])}))
        self.open()
        self.assertEqual(self.fleet.calls, [])
        self.assertEqual(self.gate()['transaction_id'], transaction)
        self.assertEqual(self.gate()['state'], 'held')
        rebuilt = json.loads(self.hooks._lease_path(transaction).read_text())
        self.assertEqual(rebuilt['state'], 'held')
        self.assertEqual({record['state'] for record in rebuilt['profiles'][0]['remotes']}, {'unobserved'})
        self.assertEqual({record['binding']['revision'] for record in rebuilt['profiles'][0]['remotes']},
                         {'a' * 64})

    def test_retired_journal_is_never_reused_for_a_new_release(self):
        self.equivalent_live_cohort()
        self.open()
        self.pending.pop()()
        transaction = self.gate()['transaction_id']
        self.assertEqual(self.gate()['state'], 'released')
        calls = list(self.fleet.calls)
        with self.assertRaises(UpdateError) as raised:
            self.hooks.reconcile_opened_remotes(self.profile['id'], transaction)
        self.assertEqual(raised.exception.code, 'ssh_generation_changed')
        self.assertEqual(self.gate()['state'], 'released')
        self.assertEqual(self.fleet.calls, calls)
        self.assertEqual(self.fixture.closes, [])

    def test_generation_change_during_unchanged_identity_cannot_release_gate(self):
        self.unchanged_live_fleet()
        self.open()
        original = self.fleet.request
        def changed(binding, operation, **params):
            result = original(binding, operation, **params)
            if binding['alias'] == 'fixture-a':
                self.store.mutate(lambda data: self.store.profile(self.profile['id'], data).update(generation=str(uuid4())))
            return result
        self.fleet.request = changed
        self.pending.pop()()
        self.assertEqual(self.job()['code'], 'ssh_generation_changed')
        self.assertEqual(self.gate()['state'], 'attention')
        self.assertTrue(all(operation == 'identity' for operation, _ in self.fleet.calls))

    def test_foreign_manifest_during_unchanged_identity_cannot_release_gate(self):
        self.unchanged_live_fleet()
        self.open()
        original = self.fleet.request
        changed = json.loads(self.manifest.read_text())
        changed['bindings'][0]['revision'] = 'd' * 64
        def changed_publication(binding, operation, **params):
            result = original(binding, operation, **params)
            atomic_json(self.manifest, changed)
            return result
        self.fleet.request = changed_publication
        self.pending.pop()()
        self.assertEqual(self.job()['code'], 'remote_binding_changed')
        self.assertEqual(self.gate()['state'], 'attention')
        self.assertEqual(json.loads(self.manifest.read_text()), changed)
        self.assertTrue(all(operation == 'identity' for operation, _ in self.fleet.calls))

    def test_truthy_unimplemented_reuse_does_not_skip_real_maintenance(self):
        self.fleet.reuse_unchanged = MagicMock()
        self.open()
        self.pending.pop()()
        self.assertEqual(self.job()['phase'], 'complete')
        self.assertEqual([alias for operation, alias in self.fleet.calls if operation == 'stop'],
                         ['fixture-a', 'fixture-b'])

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

    def test_old_runtime_preflight_leaves_state_available_for_unrelated_updates(self):
        self.failed_legacy_preparation()
        peer = self.store.add_profile('unrelated profile')
        independent = Store(self.fixture.root)
        finished = threading.Event()
        errors = []
        original_live = self.hooks._live

        def update_peer():
            try:
                independent.mutate(lambda data: independent.profile(peer['id'], data).update(alias='updated peer'))
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()

        worker = threading.Thread(target=update_peer)

        def preflight(profile):
            worker.start()
            self.assertTrue(finished.wait(1), 'old-runtime preflight held the shared state lock')
            return original_live(profile)

        try:
            with patch.object(self.hooks, '_live', side_effect=preflight):
                shown, _ = self.hooks.open_local_for_remote_reconcile(self.profile['id'])
        finally:
            if worker.ident is not None:
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(shown['state'], 'launched')
        self.assertEqual(self.store.profile(peer['id'])['alias'], 'updated peer')

    def test_old_runtime_preflight_revalidates_its_profile_gates_and_saved_lease(self):
        self.failed_legacy_preparation()
        baseline = self.store.read()
        prior = baseline['profile_maintenance'][self.profile['id']]
        lease_path = self.hooks._lease_path(prior['transaction_id'])
        saved_lease = json.loads(lease_path.read_text())
        original_live = self.hooks._live

        for transition in ('generation', 'profile_gate', 'ssh_gate', 'saved_lease'):
            with self.subTest(transition=transition):
                def restore(data):
                    data.clear()
                    data.update(deepcopy(baseline))
                self.store.mutate(restore)
                atomic_json(lease_path, saved_lease)

                def preflight(profile):
                    result = original_live(profile)
                    if transition == 'saved_lease':
                        atomic_json(lease_path, {**saved_lease, 'state': 'changed'})
                    else:
                        def change(data):
                            if transition == 'generation':
                                self.store.profile(self.profile['id'], data)['generation'] = str(uuid4())
                            elif transition == 'profile_gate':
                                data['profile_maintenance'][self.profile['id']]['transaction_id'] = str(uuid4())
                            else:
                                data.setdefault('ssh_maintenance', {})[self.profile['id']] = dict(
                                    state='held', transaction_id=str(uuid4()))
                        self.store.mutate(change)
                    return result

                with patch.object(self.hooks, '_live', side_effect=preflight):
                    with self.assertRaises(UpdateError) as caught:
                        self.hooks.open_local_for_remote_reconcile(self.profile['id'])
                self.assertEqual(caught.exception.code, 'restoration_changed')
                self.assertEqual(self.instances.show_calls, [])

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
