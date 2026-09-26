"""SSH admission metadata and the runtime's lazy diagnostic registration."""
from contextlib import contextmanager
from copy import deepcopy
import importlib.util
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, call, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from manager_core.remote_maintenance import ALLOWED_REMOTE_CODES, BOOTSTRAP, RemoteMaintenance
from manager_core.store import Store, atomic_json
from manager_core.updates import UpdateError

ROOT = Path(__file__).resolve().parents[1]


class RemoteMaintenanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = Store(self.root)
        self.profile = self.store.add_profile('fixture')
        self.profile['generation'] = str(uuid4())
        self.binding = dict(profile_id=self.profile['id'], alias='remote-dev', revision='a' * 64,
            remote_launcher='/home/test/.local/share/codex-control-center/profiles/' + self.profile['id'] + '/launch.py',
            remote_python='/usr/bin/python3')
        self.path = self.store.directory / 'profiles' / self.profile['id'] / 'ssh-bindings.json'
        atomic_json(self.path, dict(profile_id=self.profile['id'], generation=self.profile['generation'], bindings=[self.binding]))
        self.coverage = dict(complete=False, maintenance_complete=True,
            generation=self.profile['generation'], hosts=['local', 'remote-dev'], operations=[
                dict(operation='native-proxy', alias='remote-dev', revision='a' * 64, generation=self.profile['generation'])])
        self.service = RemoteMaintenance(self.root, self.store, None)

    def test_new_saved_binding_does_not_replace_the_live_generation_binding(self):
        self.profile['remote_bindings'] = [dict(self.binding, revision='b' * 64)]
        self.assertEqual(self.service.bindings(self.profile, self.coverage), [self.binding])
        stale = dict(self.coverage, generation=str(uuid4()))
        with self.assertRaises(UpdateError):
            self.service.bindings(self.profile, stale)

    def test_closed_profile_only_queues_maintenance_when_tracked_ssh_settings_changed(self):
        self.profile['policy']['launched_revision'] = self.profile['policy']['desired_revision']
        self.profile['remote_bindings'] = [dict(self.binding, prepared=True)]
        self.service.remote = MagicMock()
        self.service.remote.binding_matches_settings.return_value = True
        self.store.mutate(lambda data: data.update(ssh_inventory={
            self.profile['id']: {'hosts': ['remote-dev']}}))
        with patch('manager_core.release_code.runtime_revision', side_effect=AssertionError('manager revision is not a remote input')):
            self.assertFalse(self.service.pending_on_open(self.profile))
            self.service.remote.binding_matches_settings.return_value = False
            self.assertTrue(self.service.pending_on_open(self.profile))
            self.service.remote.binding_matches_settings.return_value = True
            self.profile['remote_bindings'] = [dict(self.binding, prepared=True, revision='b' * 64)]
            self.assertTrue(self.service.pending_on_open(self.profile))
            self.profile['remote_bindings'] = [dict(self.binding, prepared=True)]
            atomic_json(self.path, dict(profile_id=self.profile['id'], generation=self.profile['generation'],
                bindings=[self.binding], pending_policy_hosts=['remote-dev']))
            self.assertTrue(self.service.pending_on_open(self.profile))
        self.service.remote._run.assert_not_called()
        self.store.mutate(lambda data: data.update(ssh_inventory={}))
        self.assertFalse(self.service.pending_on_open(self.profile))

    def unchanged_records(self):
        self.profile['remote_bindings'] = [dict(self.binding, prepared=True)]
        self.service.remote = MagicMock()
        self.service.remote.binding_matches_settings.return_value = True
        self.service.request = MagicMock(return_value=dict(process={'pid': 12}, idle=False, exited=False))
        return [dict(binding=deepcopy(self.binding), publication_binding=deepcopy(self.binding), state='unobserved')]

    def test_reuse_requires_identity_but_never_idle_or_lifecycle_requests(self):
        records = self.unchanged_records()
        before = deepcopy(records)
        self.assertIs(self.service.reuse_unchanged(self.profile, records), True)
        self.service.request.assert_called_once_with(self.binding, 'identity')
        self.assertEqual(records, before)

    def test_reuse_rejects_partial_journals_before_any_remote_request(self):
        clean = self.unchanged_records()[0]
        changes = [dict(state=state) for state in ('observed', 'stop_requested', 'closed',
                    'prepared', 'start_requested', 'started')]
        changes += [dict(reinspect=True), dict(process=None), dict(next_binding=self.binding),
                    dict(exit_proof={'exited': True}), dict(started={}),
                    dict(publication_binding=dict(self.binding, revision='b' * 64))]
        for change in changes:
            with self.subTest(change=change):
                self.assertIs(self.service.reuse_unchanged(self.profile, [{**clean, **change}]), False)
        self.assertFalse(self.service.reuse_unchanged(self.profile, []))
        self.service.request.assert_not_called()

    def test_reuse_rejects_changed_settings_and_saved_binding_without_observation(self):
        records = self.unchanged_records()
        self.service.remote.binding_matches_settings.return_value = False
        self.assertFalse(self.service.reuse_unchanged(self.profile, records))
        self.service.remote.binding_matches_settings.return_value = True
        self.profile['remote_bindings'][0]['revision'] = 'b' * 64
        self.assertFalse(self.service.reuse_unchanged(self.profile, records))
        self.service.request.assert_not_called()

    def test_reuse_cannot_release_changed_generation_or_foreign_publication(self):
        records = self.unchanged_records()
        original = json.loads(self.path.read_text())
        variants = [dict(original, generation=str(uuid4())), dict(original, profile_id=str(uuid4())),
                    dict(original, pending_policy_hosts=['remote-dev']),
                    dict(original, bindings=[dict(self.binding, revision='b' * 64)]),
                    dict(original, bindings=[self.binding, self.binding]),
                    dict(original, bindings=[self.binding, dict(self.binding, revision='b' * 64)])]
        for manifest in variants:
            with self.subTest(manifest=manifest):
                atomic_json(self.path, original)
                self.service.request.side_effect = lambda *a, **k: atomic_json(self.path, manifest)
                with self.assertRaises(UpdateError):
                    self.service.reuse_unchanged(self.profile, records)

    def test_another_live_revision_hands_the_cohort_to_the_equivalence_path(self):
        records = self.unchanged_records()
        self.service.request.side_effect = UpdateError('remote_revision_conflict', 'fixture conflict')
        self.assertIs(self.service.reuse_unchanged(self.profile, records), False)
        self.service.request.side_effect = UpdateError('fixture_transport', 'fixture transport failure')
        with self.assertRaises(UpdateError):
            self.service.reuse_unchanged(self.profile, records)

    def test_bootstrap_keeps_only_typed_maintenance_codes(self):
        cases = [(code, code) for code in ALLOWED_REMOTE_CODES]
        cases.append(('private text from remote', 'remote_maintenance_unverified'))
        for code, expected in cases:
            with self.subTest(code=code):
                source = ('def dispatch(request):\n e=RuntimeError("boom")\n e.code='
                          + repr(code) + '\n raise e\n')
                payload = {'modules': {'launch': '', 'ws_client': '', 'native_controller': '',
                                       'maintenance': source}, 'request': {}}
                result = subprocess.run([sys.executable, '-c', BOOTSTRAP], input=json.dumps(payload).encode(),
                                        capture_output=True, check=False)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(json.loads(result.stdout), {'ok': False, 'code': expected})

    def test_typed_maintenance_codes_survive_the_response_boundary(self):
        self.service.root = ROOT
        self.service.remote = MagicMock()
        for code in ('remote_idle_status_unavailable', 'remote_shutdown_unavailable',
                     'remote_revision_conflict'):
            with self.subTest(code=code):
                self.service.remote._run.return_value = types.SimpleNamespace(returncode=2,
                    stdout=json.dumps(dict(ok=False, code=code, detail='secret-value')).encode())
                with self.assertRaises(UpdateError) as raised:
                    self.service.request(self.binding, 'inspect')
                self.assertEqual(raised.exception.code, code)
                self.assertNotIn('SSH 실행 상태를 확인하지 못해', str(raised.exception))
                self.assertNotIn('secret-value', str(raised.exception))

    def test_idle_status_observation_is_accepted_without_claiming_idle_or_exit(self):
        self.service.root = ROOT
        self.service.remote = MagicMock()
        process = dict(pid=12, process_start='34', boot_id='fixture', socket='/private.sock',
                       revision='a' * 64)
        value = dict(revision='a' * 64, process=process, idle=False, exited=False,
                     runtime_bundle='fixture-bundle-0123456789abcdef', host_identity='f' * 64,
                     observation_code='remote_idle_status_unavailable')
        self.service.remote._run.return_value = types.SimpleNamespace(returncode=0,
            stdout=json.dumps(dict(ok=True, result=value)).encode())
        result = self.service.request(self.binding, 'inspect', observe_only=True)
        self.assertFalse(result['idle'])
        self.assertEqual(result['observation_code'], 'remote_idle_status_unavailable')
        with self.assertRaises(UpdateError):
            self.service.request(self.binding, 'inspect')

    def test_legacy_settings_backfill_requires_remote_bytes_and_stable_local_binding(self):
        binding = dict(self.binding, prepared=True, runtime_bundle='old-runtime', host_identity='f' * 64)
        self.profile['remote_bindings'] = [binding]
        self.store.mutate(lambda data: self.store.profile(self.profile['id'], data).update(
            generation=self.profile['generation'], remote_bindings=[deepcopy(binding)]))
        self.service.remote = MagicMock()
        self.service.remote.binding_matches_settings.return_value = False
        self.service.remote.settings_files.return_value = {'config.toml': 'c' * 64}
        self.service.remote._settings_fingerprint.return_value = 'd' * 64
        self.service.request = MagicMock(return_value={'settings_match': False})
        self.assertFalse(self.service.verify_settings(self.profile, binding))
        self.assertNotIn('settings_fingerprint', self.store.profile(self.profile['id'])['remote_bindings'][0])
        self.service.request.return_value = {'settings_match': True}
        self.assertTrue(self.service.verify_settings(self.profile, binding))
        self.assertEqual(self.store.profile(self.profile['id'])['remote_bindings'][0]['settings_fingerprint'], 'd' * 64)
        self.service.request.assert_called_with(binding, 'identity', expected_settings={'config.toml': 'c' * 64},
            expected_host_identity='f' * 64, expected_runtime_bundle='old-runtime')
        self.service.remote.prepare.assert_not_called()
        # A stale result cannot replace a newer foreground choice.
        with self.assertRaises(UpdateError):
            self.service.verify_settings(self.profile, binding)

    def test_existing_settings_fingerprint_mismatch_never_backfills_over_config_change(self):
        self.service.remote = MagicMock()
        self.service.remote.binding_matches_settings.return_value = False
        self.service.request = MagicMock()
        self.assertFalse(self.service.verify_settings(self.profile,
            dict(self.binding, prepared=True, settings_fingerprint='a' * 64)))
        self.service.request.assert_not_called()

    def equivalent_fixture(self, *, settings_match=True, live_revision='b' * 64, published_revision=None):
        saved = dict(self.binding, prepared=True, runtime_bundle='fixture-bundle-0123456789abcdef',
                     host_identity='f' * 64)
        self.profile['remote_bindings'] = [deepcopy(saved)]
        self.store.mutate(lambda data: self.store.profile(self.profile['id'], data).update(
            generation=self.profile['generation'], remote_bindings=[deepcopy(saved)]))
        if published_revision is not None:
            atomic_json(self.path, dict(profile_id=self.profile['id'], generation=self.profile['generation'],
                bindings=[dict(self.binding, revision=published_revision)]))
        self.service.remote = MagicMock()
        self.service.remote.binding_matches_settings.return_value = False
        self.service.remote.settings_files.return_value = {'config.toml': 'c' * 64}
        self.service.remote._settings_fingerprint.return_value = 'd' * 64
        process = dict(pid=12, process_start='34', boot_id='fixture', socket='/private.sock',
                       revision=live_revision)
        live = dict(self.binding, revision=live_revision)
        def request(binding, operation, **params):
            if operation == 'inspect':
                return dict(binding=deepcopy(binding), process=deepcopy(process), idle=False, exited=False,
                            revision=live_revision, requested_revision=binding['revision'],
                            runtime_bundle=saved['runtime_bundle'], host_identity=saved['host_identity'],
                            observation_code='remote_idle_status_unavailable',
                            **({'active_binding': deepcopy(live)} if live_revision != binding['revision'] else {}))
            return dict(binding=deepcopy(live), process=deepcopy(process), idle=False, exited=False,
                        settings_match=settings_match)
        self.service.request = MagicMock(side_effect=request)
        return saved

    def equivalent_records(self, binding):
        return [dict(binding=deepcopy(binding), alias=binding['alias'],
                     publication_binding=deepcopy(binding), state='unobserved')]

    def test_equivalent_live_revision_is_adopted_and_published_without_lifecycle_requests(self):
        saved = self.equivalent_fixture()
        records = self.equivalent_records(self.binding)
        self.assertEqual(self.service.reuse_equivalent(self.profile, records), {'remote-dev': 'b' * 64})
        self.assertEqual([call.args[1] for call in self.service.request.call_args_list], ['inspect', 'identity'])
        self.assertEqual(self.service.request.call_args_list[0].kwargs,
                         dict(discover_active=True, observe_only=True))
        self.assertEqual(self.service.request.call_args_list[1].args[0]['revision'], 'b' * 64)
        stored = self.store.profile(self.profile['id'])['remote_bindings'][0]
        self.assertEqual(stored['revision'], 'b' * 64)
        self.assertEqual(stored['settings_fingerprint'], 'd' * 64)
        self.assertEqual(stored['runtime_bundle'], saved['runtime_bundle'])
        self.assertEqual([b['revision'] for b in json.loads(self.path.read_text())['bindings']], ['b' * 64])
        self.service.remote.prepare.assert_not_called()

    def test_equivalent_reuse_of_the_prepared_revision_needs_settings_evidence_only(self):
        self.equivalent_fixture(live_revision=self.binding['revision'])
        self.service.remote.binding_matches_settings.return_value = True
        records = self.equivalent_records(self.binding)
        self.assertEqual(self.service.reuse_equivalent(self.profile, records), {})
        self.assertEqual([call.args[1] for call in self.service.request.call_args_list], ['inspect'])
        self.assertEqual(json.loads(self.path.read_text())['bindings'][0]['revision'], self.binding['revision'])
        self.assertEqual(self.store.profile(self.profile['id'])['remote_bindings'][0]['revision'],
                         self.binding['revision'])

    def test_changed_settings_are_never_adopted_or_republished(self):
        self.equivalent_fixture(settings_match=False)
        records = self.equivalent_records(self.binding)
        manifest = self.path.read_bytes()
        self.assertIsNone(self.service.reuse_equivalent(self.profile, records))
        self.assertEqual(self.store.profile(self.profile['id'])['remote_bindings'][0]['revision'], 'a' * 64)
        self.assertEqual(self.path.read_bytes(), manifest)

    def test_equivalent_reuse_rejects_partial_journals_before_observation(self):
        self.equivalent_fixture()
        clean = self.equivalent_records(self.binding)[0]
        for change in (dict(state='observed'), dict(reinspect=True), dict(exit_proof={}), dict(started={}),
                       dict(process={'pid': 1}), dict(next_binding=self.binding)):
            with self.subTest(change=change):
                self.assertIsNone(self.service.reuse_equivalent(self.profile, [{**clean, **change}]))
        self.assertIsNone(self.service.reuse_equivalent(self.profile, []))
        self.service.request.assert_not_called()

    def test_equivalent_reuse_repoints_a_stale_publication_at_the_verified_binding(self):
        self.equivalent_fixture(live_revision=self.binding['revision'], published_revision='b' * 64)
        self.service.remote.binding_matches_settings.return_value = True
        published = dict(self.binding, revision='b' * 64)
        records = [dict(binding=deepcopy(published), alias=published['alias'],
                        publication_binding=deepcopy(published), state='unobserved')]
        self.assertEqual(self.service.reuse_equivalent(self.profile, records),
                         {published['alias']: self.binding['revision']})
        self.assertEqual([call.args[1] for call in self.service.request.call_args_list], ['inspect'])
        self.assertEqual(json.loads(self.path.read_text())['bindings'][0]['revision'], self.binding['revision'])
        stored = self.store.profile(self.profile['id'])['remote_bindings'][0]
        self.assertEqual(stored['revision'], self.binding['revision'])
        self.assertEqual(stored['settings_fingerprint'], 'd' * 64)
        self.service.remote.prepare.assert_not_called()

    def test_equivalent_reuse_never_releases_a_host_the_manifest_does_not_publish(self):
        self.equivalent_fixture()
        records = self.equivalent_records(self.binding)
        variants = [dict(bindings=[]),
                    dict(bindings=[], pending_policy_hosts=[self.binding['alias']]),
                    dict(bindings=[dict(self.binding, revision='c' * 64)]),
                    dict(bindings=[self.binding, self.binding])]
        for variant in variants:
            with self.subTest(variant=variant):
                manifest = dict(profile_id=self.profile['id'], generation=self.profile['generation'])
                manifest.update(variant)
                atomic_json(self.path, manifest)
                before = self.path.read_bytes()
                self.assertIsNone(self.service.reuse_equivalent(self.profile, records))
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(self.store.profile(self.profile['id'])['remote_bindings'][0]['revision'], 'a' * 64)
        self.service.remote.prepare.assert_not_called()

    def test_equivalent_reuse_fails_closed_on_a_concurrent_settings_change(self):
        self.equivalent_fixture()
        records = self.equivalent_records(self.binding)
        verified = dict(self.service.remote.settings_files.return_value)
        for files_changed in (False, True):
            with self.subTest(files_changed=files_changed):
                self.store.mutate(lambda data: self.store.profile(self.profile['id'], data)['policy'].update(
                    desired_revision=self.profile['policy']['desired_revision']))
                manifest = self.path.read_bytes()
                saved = deepcopy(self.store.profile(self.profile['id'])['remote_bindings'])
                calls = []
                def settings_files(profile, binding, *, changed=files_changed, calls=calls):
                    calls.append(binding['alias'])
                    if len(calls) == 2:
                        self.store.mutate(lambda data: self.store.profile(
                            self.profile['id'], data)['policy'].update(desired_revision=5))
                    return dict(verified, config_toml='e' * 64) if changed else dict(verified)
                self.service.remote.settings_files = MagicMock(side_effect=settings_files)
                with self.assertRaises(UpdateError) as raised:
                    self.service.reuse_equivalent(self.profile, records)
                self.assertEqual(raised.exception.code, 'policy_changed')
                self.assertEqual(self.store.profile(self.profile['id'])['remote_bindings'], saved)
                self.assertEqual(self.path.read_bytes(), manifest)
                self.service.remote.prepare.assert_not_called()

    def identity_helper(self, process):
        native = types.SimpleNamespace(_running=MagicMock(return_value=process),
                                       _instance_lock_released=MagicMock(return_value=True))
        spec = importlib.util.spec_from_file_location('maintenance_identity_fixture', ROOT / 'scripts/remote_helpers/maintenance.py')
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'native_controller': native,
                'ws_client': types.SimpleNamespace(WebSocketPipe=MagicMock()),
                'fcntl': types.SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=MagicMock())}):
            spec.loader.exec_module(module)
        return module, native

    def test_live_identity_accepts_busy_or_ephemeral_actors_without_asserting_idle(self):
        process = dict(pid=12, revision='a' * 64, process_start='34', boot_id='fixture', socket='/private.sock')
        module, native = self.identity_helper(process)
        methods = []
        @contextmanager
        def connection(profile):
            def request(method, params):
                methods.append(method)
                self.assertEqual(method, 'server/diagnostics', 'identity must not inspect or unload ephemeral actors')
                return dict(process={'id': 12}, gauges=[dict(name='app.managed.requests.pending_completion', value=8)])
            yield request
        with patch.object(module, 'connection', connection):
            result = module.identity(self.root, process['revision'])
        self.assertEqual(result, dict(process=process, revision=process['revision'], idle=False, exited=False))
        self.assertEqual(methods, ['server/diagnostics'])
        self.assertEqual(native._running.call_count, 2)
        native._instance_lock_released.assert_not_called()

    def test_live_identity_rejects_diagnostics_pid_or_process_identity_change(self):
        process = dict(pid=12, revision='a' * 64, process_start='34', boot_id='fixture', socket='/private.sock')
        for changed in ({'pid': 13}, {'process_start': '35'}, {'boot_id': 'new-boot'}, {'socket': '/new.sock'}):
            with self.subTest(changed=changed):
                module, native = self.identity_helper(process)
                native._running.side_effect = [process, {**process, **changed}]
                connection = MagicMock()
                connection.return_value.__enter__.return_value.return_value = {'process': {'id': 12}}
                with patch.object(module, 'connection', connection), self.assertRaises(RuntimeError):
                    module.identity(self.root, process['revision'])
        module, _ = self.identity_helper(process)
        connection = MagicMock()
        connection.return_value.__enter__.return_value.return_value = {'process': {'id': 99}}
        with patch.object(module, 'connection', connection), self.assertRaises(RuntimeError):
            module.identity(self.root, process['revision'])

    def test_unknown_proxy_revision_or_another_operation_cannot_claim_coverage(self):
        for changes in ({'revision': 'b' * 64}, {'operation': 'native-start'}, {'generation': str(uuid4())}):
            with self.subTest(changes=changes), self.assertRaises(UpdateError):
                self.service.bindings(self.profile, dict(self.coverage,
                    operations=[dict(self.coverage['operations'][0], **changes)]))

    def test_pending_policy_host_uses_saved_binding_only_for_disconnected_coverage(self):
        self.profile['remote_bindings'] = [dict(self.binding, prepared=True)]
        atomic_json(self.path, dict(profile_id=self.profile['id'], generation=self.profile['generation'],
            bindings=[], pending_policy_hosts=['remote-dev']))
        self.assertEqual(self.service.bindings(self.profile, dict(self.coverage, operations=[])), [self.binding])
        with self.assertRaises(UpdateError):
            self.service.bindings(self.profile, dict(self.coverage, operations=[
                dict(self.coverage['operations'][0], revision='c' * 64)]))

    def test_snapshot_tracks_actual_revision_separately_and_shutdown_targets_it(self):
        self.service.root = ROOT
        self.service.remote = MagicMock()
        process = dict(pid=12, process_start='34', boot_id='fixture', socket='/private.sock', revision='b' * 64)
        value = dict(revision='b' * 64, requested_revision='a' * 64, process=process, idle=True, exited=False)
        self.service.remote._run.return_value = types.SimpleNamespace(returncode=0,
            stdout=json.dumps(dict(ok=True, result=value)).encode())
        entry = self.service.snapshot(self.profile, self.coverage)[0]
        self.assertEqual(entry['binding'], self.binding)
        self.assertEqual(entry['active_binding'], dict(self.binding, revision='b' * 64))
        self.assertEqual(entry['process'], process)
        with self.assertRaises(UpdateError):
            self.service.request(self.binding, 'inspect')  # Recovery of lost starts remains strict.
        self.service.remote._run.return_value.stdout = json.dumps(dict(ok=True,
            result=dict(revision='b' * 64, process=None, idle=True, exited=True))).encode()
        self.service.stop(entry)
        request = json.loads(self.service.remote._run.call_args.kwargs['input'])['request']
        self.assertEqual(request['binding']['revision'], 'b' * 64)
        self.assertEqual(request['expected_process'], process)

    def test_discovery_cannot_mislabel_a_process_or_accept_an_unbound_revision(self):
        self.service.root = ROOT
        self.service.remote = MagicMock()
        process = dict(pid=12, process_start='34', boot_id='fixture', socket='/private.sock', revision='b' * 64)
        base = dict(revision='b' * 64, requested_revision='a' * 64, process=process, idle=True, exited=False)
        for change in (dict(requested_revision='c' * 64), dict(revision='bad'),
                       dict(process=dict(process, revision='c' * 64)), dict(process=None, exited=True)):
            with self.subTest(change=change), self.assertRaises(UpdateError):
                self.service.remote._run.return_value = types.SimpleNamespace(returncode=0,
                    stdout=json.dumps(dict(ok=True, result={**base, **change})).encode())
                self.service.snapshot(self.profile, self.coverage)

    def test_version_metadata_belongs_to_observed_running_revision(self):
        self.service.root = ROOT
        self.service.remote = MagicMock()
        process = dict(pid=12, process_start='34', boot_id='fixture', socket='/private.sock', revision='b' * 64)
        value = dict(revision='b' * 64, requested_revision='a' * 64, process=process,
                     idle=False, exited=False, runtime_bundle='1.0-abcdef0123456789', host_identity='f' * 64)
        self.service.remote._run.return_value = types.SimpleNamespace(returncode=0,
            stdout=json.dumps(dict(ok=True, result=value)).encode())
        actual = self.service.request(self.binding, 'inspect', discover_active=True)
        self.assertEqual(actual['runtime_bundle'], value['runtime_bundle'])
        self.assertEqual(actual['host_identity'], 'f' * 64)
        self.assertEqual(actual['active_binding']['revision'], 'b' * 64)
        for changes in (dict(runtime_bundle='../outside'), dict(host_identity='bad'),
                        dict(process=None, exited=True, revision='a' * 64)):
            self.service.remote._run.return_value.stdout = json.dumps(dict(ok=True, result={**value, **changes})).encode()
            with self.subTest(changes=changes), self.assertRaises(UpdateError):
                self.service.request(self.binding, 'inspect', discover_active=True)

    def test_observation_only_version_proof_cannot_be_used_as_idle_or_lifecycle_proof(self):
        self.service.root = ROOT
        self.service.remote = MagicMock()
        process = dict(pid=12, process_start='34', boot_id='fixture', socket='/private.sock', revision='a' * 64)
        value = dict(revision='a' * 64, process=process, idle=False, exited=False,
                     runtime_bundle='old-runtime', host_identity='f' * 64,
                     observation_code='remote_listener_unavailable')
        self.service.remote._run.return_value = types.SimpleNamespace(returncode=0,
            stdout=json.dumps(dict(ok=True, result=value)).encode())
        result = self.service.request(self.binding, 'inspect', observe_only=True)
        self.assertFalse(result['idle'])
        self.assertEqual(result['runtime_bundle'], 'old-runtime')
        with self.assertRaises(UpdateError):
            self.service.request(self.binding, 'inspect')
        value['idle'] = True
        self.service.remote._run.return_value.stdout = json.dumps(dict(ok=True, result=value)).encode()
        with self.assertRaises(UpdateError):
            self.service.request(self.binding, 'inspect', observe_only=True)

    def test_observation_fallback_requires_exact_executable_and_unchanged_birth_identity(self):
        process = dict(pid=12, process_start='34', boot_id='fixture', socket='/private.sock', revision='a' * 64)
        module, native = self.identity_helper(process)
        class MaintenanceError(RuntimeError):
            code = 'remote_listener_unavailable'
        native.RemoteMaintenanceError = MaintenanceError
        native._descriptor = MagicMock(return_value={'runtime': str(self.root / 'runtime')})
        binary = self.root / 'runtime/codex'
        binary.parent.mkdir()
        binary.write_bytes(b'fixture')
        resolve = Path.resolve
        def resolved(path, *args, **kwargs):
            return binary if str(path).replace('\\', '/') == '/proc/12/exe' else resolve(path, *args, **kwargs)
        with patch.object(module, 'inspect', side_effect=MaintenanceError()), patch.object(module.Path, 'resolve', resolved):
            result = module.observe(self.root, 'a' * 64)
            self.assertFalse(result['idle'])
            self.assertFalse(result['exited'])
            self.assertEqual(result['observation_code'], 'remote_listener_unavailable')
            native._running.side_effect = [process, dict(process, process_start='changed')]
            with self.assertRaises(RuntimeError):
                module.observe(self.root, 'a' * 64)
        native._running.side_effect = None
        with patch.object(module, 'inspect', side_effect=MaintenanceError()), patch.object(module.Path, 'resolve',
                lambda path, **kw: self.root / ('wrong' if str(path).replace('\\', '/') == '/proc/12/exe' else 'right')):
            with self.assertRaises(RuntimeError):
                module.observe(self.root, 'a' * 64)

    def test_immutable_settings_comparison_rejects_changed_missing_extra_and_escaped_files(self):
        module, native = self.identity_helper(None)
        directory = self.root / 'definitions' / ('a' * 64)
        directory.mkdir(parents=True)
        content = b'model = "fixture"\n'
        (directory / 'config.toml').write_bytes(content)
        native._descriptor = MagicMock(return_value={'definition': str(directory)})
        expected = {'config.toml': hashlib.sha256(content).hexdigest()}
        self.assertTrue(module.settings_match(self.root, 'a' * 64, expected))
        (directory / 'config.toml').write_bytes(content + b'# changed')
        self.assertFalse(module.settings_match(self.root, 'a' * 64, expected))
        (directory / 'config.toml').write_bytes(content)
        (directory / 'extra.json').write_text('{}')
        self.assertFalse(module.settings_match(self.root, 'a' * 64, expected))
        (directory / 'extra.json').unlink()
        (directory / 'config.toml').unlink()
        self.assertFalse(module.settings_match(self.root, 'a' * 64, expected))
        native._descriptor.return_value = {'definition': str(self.root / 'outside')}
        with self.assertRaises(ValueError):
            module.settings_match(self.root, 'a' * 64, expected)

    def test_dispatch_reads_version_from_actual_descriptor_not_prepared_revision(self):
        native = types.SimpleNamespace(_descriptor=MagicMock(side_effect=lambda p, revision: dict(
            runtime='/private/runtime/' + ('old-1234567890abcdef' if revision == 'b' * 64 else 'new-1234567890abcdef'),
            host_identity='f' * 64)))
        spec = importlib.util.spec_from_file_location('maintenance_version_fixture', ROOT / 'scripts/remote_helpers/maintenance.py')
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'native_controller': native,
                'ws_client': types.SimpleNamespace(WebSocketPipe=MagicMock()),
                'fcntl': types.SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=MagicMock())}):
            spec.loader.exec_module(module)
        directory = self.root / '.local/share/codex-control-center/profiles' / self.profile['id']
        directory.mkdir(parents=True)
        binding = dict(self.binding, remote_launcher=str(directory / 'launch.py'))
        actual = dict(process={'pid': 12, 'revision': 'b' * 64}, idle=False, exited=False, revision='b' * 64)
        with patch.object(module.Path, 'home', return_value=self.root), patch.object(module, 'inspect', return_value=actual):
            result = module.dispatch(dict(binding=binding, operation='inspect', discover_active=True))
        self.assertEqual(result['runtime_bundle'], 'old-1234567890abcdef')
        self.assertEqual(native._descriptor.call_args.args[1], 'b' * 64)

    def test_start_failure_surfaces_only_allowlisted_diagnostic_code(self):
        self.service.root = ROOT
        self.service.remote = MagicMock()
        for code, expected in [('remote_configuration_changed', 'remote_configuration_changed'),
                               ('remote_runtime_exited', 'remote_runtime_exited'),
                               ('private text from remote', 'remote_maintenance_unverified')]:
            self.service.remote._run.return_value = types.SimpleNamespace(returncode=2,
                stdout=json.dumps(dict(ok=False, code=code, detail='secret-value')).encode())
            with self.subTest(code=code), self.assertRaises(UpdateError) as raised:
                self.service.request(self.binding, 'start')
            self.assertEqual(raised.exception.code, expected)
            self.assertNotIn('secret-value', str(raised.exception))
            self.assertNotIn('private text', str(raised.exception))

    def test_read_only_reconciliation_never_replays_a_lost_mutation(self):
        self.service.request = MagicMock(return_value={'exited': False, 'idle': True, 'process': {'pid': 12}})
        entry = dict(state='stop_requested', binding=self.binding)
        self.service.reconcile(entry)
        self.assertEqual(entry['state'], 'stop_requested')
        self.service.request.return_value = {'exited': True, 'idle': True, 'process': None}
        self.service.reconcile(entry)
        self.assertEqual(entry['state'], 'closed')
        self.assertEqual([c.args[1] for c in self.service.request.call_args_list], ['inspect', 'inspect'])

    def test_lost_start_reply_settles_on_exact_read_only_identity_without_idle(self):
        self.service.request = MagicMock(return_value=dict(
            binding=deepcopy(self.binding), process={'pid': 12, 'revision': self.binding['revision']},
            idle=False, exited=False, runtime_bundle='fixture-bundle-0123456789abcdef',
            host_identity='f' * 64, observation_code='remote_idle_status_unavailable'))
        entry = dict(state='start_requested', binding=self.binding, next_binding=deepcopy(self.binding))
        self.service.reconcile(entry)
        self.assertEqual(entry['state'], 'started')
        self.assertFalse(entry['started']['idle'])
        self.assertEqual(self.service.request.call_args,
                         call(self.binding, 'inspect', discover_active=True, observe_only=True))
        self.service.request.return_value = dict(self.service.request.return_value,
                                                 active_binding=dict(self.binding, revision='c' * 64))
        other = dict(state='start_requested', binding=self.binding, next_binding=deepcopy(self.binding))
        self.service.reconcile(other)
        self.assertEqual(other['state'], 'start_requested')

    def test_unused_diagnostic_gauges_are_optional_but_current_request_is_required(self):
        class MissingEvidence(RuntimeError):
            def __init__(self, code):
                self.code = code
        native = types.SimpleNamespace(_running=MagicMock(return_value={'pid': 12}),
                                       _instance_lock_released=MagicMock(return_value=True),
                                       RemoteMaintenanceError=MissingEvidence)
        module_spec = importlib.util.spec_from_file_location('maintenance_fixture', ROOT / 'scripts/remote_helpers/maintenance.py')
        module = importlib.util.module_from_spec(module_spec)
        with patch.dict(sys.modules, {'native_controller': native,
                'ws_client': types.SimpleNamespace(WebSocketPipe=MagicMock()),
                'fcntl': types.SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=MagicMock())}):
            module_spec.loader.exec_module(module)
        (self.root / 'native-start.lock').write_bytes(b'')
        for values, idle in [({'requests.pending_completion': 1}, True),
                             ({}, None), ({'requests.pending_completion': 2}, False),
                             ({'requests.pending_completion': 1, 'logins.running': 1}, False),
                             ({'requests.pending_completion': 1, 'processes.running': 1}, False)]:
            @contextmanager
            def connection(profile):
                def request(method, params):
                    if method == 'server/diagnostics':
                        return {'process': {'id': 12}, 'gauges': [dict(name='app.managed.' + name, value=value)
                            for name, value in values.items()]}
                    if method == 'thread/loaded/list':
                        return {'data': [], 'nextCursor': None}
                    raise AssertionError(method)
                yield request
            with self.subTest(values=values), patch.object(module, 'connection', connection):
                if idle is None:
                    with self.assertRaises(MissingEvidence) as raised:
                        module.inspect(self.root, 'a' * 64)
                    self.assertEqual(raised.exception.code, 'remote_idle_diagnostics_unavailable')
                else:
                    self.assertEqual(module.inspect(self.root, 'a' * 64)['idle'], idle)

    def test_remote_discovery_validates_actual_descriptor_before_opening_runtime(self):
        process = dict(pid=12, revision='b' * 64)
        native = types.SimpleNamespace(_running=MagicMock(return_value=process), _descriptor=MagicMock())
        spec = importlib.util.spec_from_file_location('maintenance_discovery_fixture', ROOT / 'scripts/remote_helpers/maintenance.py')
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'native_controller': native,
                'ws_client': types.SimpleNamespace(WebSocketPipe=MagicMock()),
                'fcntl': types.SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=MagicMock())}):
            spec.loader.exec_module(module)
        (self.root / 'native-start.lock').write_bytes(b'')
        connection = MagicMock()
        connection.return_value.__enter__.return_value.side_effect = [
            dict(process={'id': 12}, gauges=[dict(name='app.managed.requests.pending_completion', value=1)]),
            dict(data=[], nextCursor=None)]
        with patch.object(module, 'connection', connection):
            result = module.inspect(self.root, 'a' * 64, discover_active=True)
            self.assertEqual(result['revision'], 'b' * 64)
            self.assertEqual(result['requested_revision'], 'a' * 64)
            native._descriptor.assert_called_once_with(self.root, 'b' * 64)
            native._running.assert_called_with(self.root, 'b' * 64)
            connection.reset_mock()
            native._descriptor.side_effect = ValueError('foreign profile descriptor')
            with self.assertRaisesRegex(ValueError, 'foreign profile'):
                module.inspect(self.root, 'a' * 64, discover_active=True)
            connection.assert_not_called()


if __name__ == '__main__':
    unittest.main()
