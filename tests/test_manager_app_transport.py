import io
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.app_transport import AppTransport, JsonLineObserver, RuntimeObserver, identity_fingerprint, utc_now


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.profile_id = str(uuid4())
        directory = self.root / 'work/control-center/profiles' / self.profile_id
        self.profile = {'id': self.profile_id, 'home': str(directory / 'codex'),
                        'ui_home': str(directory / 'ui'), 'process_id': 1001}
        self.shortcut = {'profile_id': self.profile_id, 'thread_id': str(uuid4()),
                         'host_id': 'local', 'source_store_id': 'manager:' + self.profile_id}
        self.executable = self.root / 'ChatGPT.exe'
        self.executable.touch()
        self.calls = []
        self.transport = AppTransport(self.root, popen=self.capture)

    def tearDown(self):
        self.temp.cleanup()

    def capture(self, args, **kwargs):
        self.calls.append((args, kwargs))
        return SimpleNamespace(pid=2002)

    def test_own_profile_request_is_not_selection_confirmation(self):
        result = self.transport.open_conversation(self.profile, self.shortcut, str(self.executable),
                                                  {'TEST_API_KEY': 'not-for-output', 'CODEX_HOME': 'wrong', 'CODEX_MANAGER_DESKTOP_PIPE':'codex-ipc'})
        self.assertEqual(result['state'], 'request_sent')
        self.assertFalse(result['selection_verified'])
        args, kwargs = self.calls[0]
        self.assertEqual(args[1], '--user-data-dir=' + self.profile['ui_home'])
        self.assertEqual(kwargs['env']['CODEX_HOME'], self.profile['home'])
        self.assertEqual(kwargs['env']['CODEX_MANAGER_DESKTOP_PIPE'], 'codex-manager-' + self.profile_id)
        self.assertNotIn('not-for-output', json.dumps(result))
        self.assertNotIn('shell', kwargs)

    def test_foreign_or_remote_navigation_never_opens_wrong_instance(self):
        for update, reason in (({'source_store_id': 'original:local'}, 'foreign_store_binding_required'),
                               ({'host_id': 'ssh:server'}, 'exact_remote_navigation_unverified')):
            result = self.transport.open_conversation(self.profile, {**self.shortcut, **update}, str(self.executable))
            self.assertEqual(result['reason'], reason)
        self.assertFalse(self.calls)

    def test_canonical_shortcut_waits_then_opens_without_projection_manifest(self):
        home = self.root / 'original'
        home.mkdir()
        with closing(sqlite3.connect(home / 'state_5.sqlite')) as db, db:
            db.execute('CREATE TABLE threads (id TEXT PRIMARY KEY)')
            db.execute('INSERT INTO threads VALUES (?)', (self.shortcut['thread_id'],))
        shortcut = {**self.shortcut, 'source_store_id': 'original:local'}
        environment = {'CODEX_RECORD_HOME': str(home), 'CODEX_MANAGER_SHARED_EXECUTION': '1'}
        with patch.object(self.transport, 'observe', return_value={}):
            pending = self.transport.open_conversation(self.profile, shortcut, str(self.executable), environment)
        self.assertEqual(pending['state'], 'waiting_for_reader')
        self.assertFalse(self.calls)
        ready = dict(initialized=True, connected=True, canonical_storage=dict(enabled=True), shared_execution_version=1)
        with patch.object(self.transport, 'observe', return_value=ready):
            opened = self.transport.open_conversation(self.profile, shortcut, str(self.executable), environment)
        self.assertEqual(opened['state'], 'request_sent')
        self.assertEqual(opened['uri'], 'codex://threads/' + self.shortcut['thread_id'])
        self.assertFalse(opened['readonly_projection'])
        self.assertEqual(len(self.calls), 1)

    def test_original_ui_home_cannot_be_targeted_by_forged_profile(self):
        with self.assertRaises(ValueError):
            self.transport.open_conversation({**self.profile, 'ui_home': str(self.root)},
                                              self.shortcut, str(self.executable))
        self.assertFalse(self.calls)

    def test_foreign_projection_requires_unique_local_manifest_identity(self):
        projection_id = str(uuid4())
        manifest = self.root / 'work/control-center/viewer-records.json'
        manifest.parent.mkdir(parents=True)
        entry = {'projectionThreadId': projection_id, 'threadId': self.shortcut['thread_id'],
                 'hostId': 'local', 'sourceStoreId': 'original:local'}
        catalog = {'version': 1, 'hostId': 'local', 'entries': [entry]}
        manifest.write_text(json.dumps(catalog), encoding='utf-8')
        shortcut = {**self.shortcut, 'source_store_id': 'original:local'}
        environment = {'CODEX_MANAGER_RECORD_CATALOG': str(manifest)}
        result = self.transport.open_conversation(self.profile, shortcut, str(self.executable), environment)
        self.assertTrue(result['readonly_projection'])
        self.assertTrue(result['uri'].endswith(projection_id))
        self.assertFalse(result['selection_verified'])
        catalog['entries'].append(entry)
        manifest.write_text(json.dumps(catalog), encoding='utf-8')
        result = self.transport.open_conversation(self.profile, shortcut, str(self.executable), environment)
        self.assertEqual(result['state'], 'blocked')

    def test_missing_process_does_not_launch_untracked_app(self):
        for process_id in (None, True, 0):
            result = self.transport.open_conversation({**self.profile, 'process_id': process_id}, self.shortcut, str(self.executable))
            self.assertEqual(result['reason'], 'profile_not_running')
        self.assertFalse(self.calls)

    def test_shared_projection_requires_the_current_running_generation_to_enable_it(self):
        self.profile['generation'] = str(uuid4())
        projection = str(uuid4())
        directory = self.root / 'work/control-center'
        manifest = directory / 'catalog/local-records.json'
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({'version': 1, 'hostId': 'local', 'entries': [{
            'projectionThreadId': projection, 'threadId': self.shortcut['thread_id'],
            'hostId': 'local', 'sourceStoreId': 'original:local'}]}), encoding='utf-8')
        path = directory / 'instances' / self.profile_id / 'runtime-state.json'
        path.parent.mkdir(parents=True)
        ready = {'schema_version': 1, 'profile_id': self.profile_id, 'generation': self.profile['generation'],
                 'observed_at': utc_now(), 'initialized': True, 'connected': True,
                 'shared_catalog': {'enabled': True}}
        shortcut = {**self.shortcut, 'source_store_id': 'original:local'}
        environment = {'CODEX_MANAGER_SHARED_CATALOG': str(manifest)}
        for changed in ({'generation': str(uuid4())}, {'shared_catalog': {'enabled': False}},
                        {'observed_at': '2000-01-01T00:00:00+00:00'}, {'connected': False}, {'initialized': False}):
            with self.subTest(changed=changed):
                path.write_text(json.dumps({**ready, **changed}), encoding='utf-8')
                result = self.transport.open_conversation(self.profile, shortcut, str(self.executable), environment)
                if changed == {'shared_catalog': {'enabled': False}}:
                    self.assertEqual(result['state'], 'blocked')
                    self.assertEqual(result['reason'], 'shared_catalog_runtime_unavailable')
                else:
                    self.assertEqual(result['state'], 'waiting_for_reader')
                    self.assertEqual(result['reason'], 'runtime_starting')
                self.assertFalse(self.calls)
        path.write_text(json.dumps(ready), encoding='utf-8')
        result = self.transport.open_conversation(self.profile, shortcut, str(self.executable), environment)
        self.assertEqual(result['state'], 'request_sent')
        self.assertEqual(result['uri'], 'codex://threads/' + projection)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(result['selection_verified'])

    def test_observer_missing_or_forged_identity_does_not_allow_restart(self):
        self.assertEqual(self.transport.observe(self.profile)['activity'], 'unknown')
        path = self.root / 'work/control-center/instances' / self.profile_id / 'runtime-state.json'
        RuntimeObserver(str(uuid4())).write_snapshot(path)
        self.assertEqual(self.transport.observe(self.profile)['reason'], 'observer_identity_mismatch')

    def managed_foreign_fixture(self):
        directory = self.root / 'work/control-center'
        source_profile = str(uuid4())
        source_id = 'manager:' + source_profile
        home = directory / 'profiles' / source_profile / 'codex'
        home.mkdir(parents=True)
        (home / 'managed-authority').mkdir()
        Path(self.profile['home']).mkdir(parents=True, exist_ok=True)
        Path(self.profile['ui_home']).mkdir(exist_ok=True)
        marker = home / 'managed-source.json'
        marker.write_text(json.dumps({'host_id': 'local', 'store_id': source_id}), encoding='utf-8')
        grant = {'version': 1, 'host_id': 'local', 'store_id': source_id,
                 'thread_id': self.shortcut['thread_id'], 'owner_profile_id': self.profile_id,
                 'epoch': 2, 'revision': 4}
        grant_path = home / 'managed-authority' / (self.shortcut['thread_id'] + '.json')
        grant_path.write_text(json.dumps(grant), encoding='utf-8')
        source = {'hostId': 'local', 'sourceStoreId': source_id, 'codexHome': str(home)}
        binding = {'threadId': grant['thread_id'], 'hostId': 'local', 'sourceStoreId': source_id,
                   'ownerProfileId': self.profile_id, 'ownershipEpoch': 2, 'recordRevision': 4}
        manifest = {'version': 1, 'hostId': 'local', 'profileId': self.profile_id,
                    'sources': [source], 'bindings': [binding]}
        manifest_path = Path(self.profile['home']).parent / 'managed-sources.json'
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
        state = {'version': 1, 'profiles': [self.profile, {'id': source_profile, 'home': str(home)}],
                 'sources': [{'id': source_id, 'host_id': 'local', 'home': str(home)}]}
        state_path = directory / 'state.json'
        state_path.write_text(json.dumps(state), encoding='utf-8')
        return SimpleNamespace(home=home, marker=marker, manifest=manifest, manifest_path=manifest_path,
            state=state, state_path=state_path, grant=grant, grant_path=grant_path,
            shortcut={**self.shortcut, 'source_store_id': source_id},
            environment={'CODEX_MANAGER_MANAGED_SOURCES': str(manifest_path)})

    def test_current_owned_managed_source_opens_canonical_thread(self):
        fixture = self.managed_foreign_fixture()
        result = self.transport.open_conversation(self.profile, fixture.shortcut,
                                                  str(self.executable), fixture.environment)
        self.assertEqual(result['state'], 'request_sent')
        self.assertEqual(result['uri'], 'codex://threads/' + self.shortcut['thread_id'])
        self.assertIs(result['readonly_projection'], False)
        self.assertNotIn('projection_thread_id', result)
        self.assertFalse(result['selection_verified'])

    def test_managed_source_rejects_stale_owner_epoch_and_revision(self):
        fixture = self.managed_foreign_fixture()
        for change in ({'owner_profile_id': str(uuid4())}, {'epoch': 3}, {'revision': 5}):
            with self.subTest(change=change):
                fixture.grant_path.write_text(json.dumps({**fixture.grant, **change}), encoding='utf-8')
                result = self.transport.open_conversation(self.profile, fixture.shortcut,
                                                          str(self.executable), fixture.environment)
                self.assertEqual(result['state'], 'blocked')
        self.assertFalse(self.calls)

    def test_managed_source_rejects_wrong_manifest_owner_host_source_and_bool_counter(self):
        fixture = self.managed_foreign_fixture()
        for update in ({'ownerProfileId': str(uuid4())}, {'hostId': 'ssh:example'},
                       {'sourceStoreId': 'manager:' + str(uuid4())}, {'ownershipEpoch': True}):
            with self.subTest(update=update):
                manifest = {**fixture.manifest, 'bindings': [{**fixture.manifest['bindings'][0], **update}]}
                fixture.manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
                result = self.transport.open_conversation(self.profile, fixture.shortcut,
                                                          str(self.executable), fixture.environment)
                self.assertEqual(result['state'], 'blocked')
        self.assertFalse(self.calls)

    def test_managed_source_requires_exact_registered_home_and_marker(self):
        fixture = self.managed_foreign_fixture()
        for sources in ([], [{**fixture.state['sources'][0], 'home': str(self.root)}]):
            fixture.state_path.write_text(json.dumps({**fixture.state, 'sources': sources}), encoding='utf-8')
            result = self.transport.open_conversation(self.profile, fixture.shortcut,
                                                      str(self.executable), fixture.environment)
            self.assertEqual(result['state'], 'blocked')
        fixture.state_path.write_text(json.dumps(fixture.state), encoding='utf-8')
        fixture.marker.write_text(json.dumps({'host_id': 'local', 'store_id': 'original:local'}), encoding='utf-8')
        result = self.transport.open_conversation(self.profile, fixture.shortcut,
                                                  str(self.executable), fixture.environment)
        self.assertEqual(result['state'], 'blocked')
        self.assertFalse(self.calls)

    def test_managed_source_manifest_must_be_exact_own_profile_file(self):
        fixture = self.managed_foreign_fixture()
        elsewhere = self.root / 'elsewhere.json'
        elsewhere.write_text(json.dumps(fixture.manifest), encoding='utf-8')
        for environment in ({}, {'CODEX_MANAGER_MANAGED_SOURCES': str(elsewhere)}):
            result = self.transport.open_conversation(self.profile,
                {**fixture.shortcut, 'authority_verified': True, 'writer_release_verified': True},
                str(self.executable), environment)
            self.assertEqual(result['state'], 'blocked')
        self.assertFalse(self.calls)

    def test_managed_source_rejects_duplicate_thread_binding(self):
        fixture = self.managed_foreign_fixture()
        fixture.manifest['bindings'].append(dict(fixture.manifest['bindings'][0]))
        fixture.manifest_path.write_text(json.dumps(fixture.manifest), encoding='utf-8')
        result = self.transport.open_conversation(self.profile, fixture.shortcut,
                                                  str(self.executable), fixture.environment)
        self.assertEqual(result['state'], 'blocked')
        self.assertFalse(self.calls)

    def test_managed_source_manifest_cannot_be_shared_by_hardlink(self):
        fixture = self.managed_foreign_fixture()
        try:
            os.link(fixture.manifest_path, self.root / 'linked-manifest.json')
        except OSError:
            self.skipTest('Hard links are not supported on this fixture filesystem.')
        result = self.transport.open_conversation(self.profile, fixture.shortcut,
                                                  str(self.executable), fixture.environment)
        self.assertEqual(result['state'], 'blocked')
        self.assertFalse(self.calls)

    def test_own_profile_home_cannot_alias_an_external_directory(self):
        home = Path(self.profile['home'])
        home.parent.mkdir(parents=True)
        external = self.root / 'outside'
        external.mkdir()
        try:
            home.symlink_to(external, target_is_directory=True)
        except OSError:
            self.skipTest('Directory symlinks are unavailable in this Windows test account.')
        with self.assertRaises(ValueError):
            self.transport.open_conversation(self.profile, self.shortcut, str(self.executable))
        self.assertFalse(self.calls)


class ObserverTests(unittest.TestCase):
    def test_header_tracks_successful_open_and_rename_not_background_lists(self):
        self.observer.consume('client', {'id': 101, 'method': 'thread/resume', 'params': {'threadId': self.thread}})
        self.observer.consume('server', {'id': 101, 'result': {'thread': {'id': self.thread, 'name': 'selected task', 'status': {'type': 'idle'}}}})
        self.observer.consume('client', {'id': 102, 'method': 'thread/list', 'params': {}})
        self.observer.consume('server', {'id': 102, 'result': {'data': [{'id': str(uuid4()), 'name': 'background', 'status': {'type': 'idle'}}]}})
        self.assertEqual(self.observer.snapshot()['opened_task']['title'], 'selected task')
        self.send('thread/name/updated', {'threadId': self.thread, 'threadName': 'renamed task'})
        self.assertEqual(self.observer.snapshot()['opened_task']['title'], 'renamed task')
        self.observer.consume('client', {'id': 103, 'method': 'thread/resume', 'params': {'threadId': 'failed-task'}})
        self.observer.consume('server', {'id': 103, 'error': {'code': -32600}})
        self.assertEqual(self.observer.snapshot()['opened_task']['thread_id'], self.thread)

    def test_windows_setup_diagnostics_include_outcomes_without_paths_or_raw_errors(self):
        observer = RuntimeObserver(str(uuid4()))
        for i, status in enumerate(('ready', 'notConfigured', 'updateRequired')):
            observer.consume('client', {'id': i, 'method': 'windowsSandbox/readiness'})
            observer.consume('server', {'id': i, 'result': {'status': status, 'private': 'private-token'}})
            self.assertEqual(observer.snapshot()['recent_diagnostics'][-1]['reason'], 'windows_sandbox_' + status)
        observer.consume('client', {'id': 10, 'method': 'windowsSandbox/setupStart', 'params': {'cwd': 'private-path'}})
        observer.consume('server', {'id': 10, 'result': {'started': True}})
        self.assertEqual(observer.snapshot()['recent_diagnostics'][-1]['reason'], 'windows_setup_started')
        observer.consume('server', {'method': 'windowsSandbox/setupCompleted', 'params': {
            'success': False, 'error': 'private-token private-path'}})
        self.assertEqual(observer.snapshot()['recent_diagnostics'][-1]['reason'], 'windows_setup_failed')
        observer.consume('server', {'method': 'windowsSandbox/setupCompleted', 'params': {'success': True}})
        self.assertEqual(observer.snapshot()['recent_diagnostics'][-1]['reason'], 'windows_setup_succeeded')
        observer.consume('client', {'id': 11, 'method': 'windowsSandbox/setupStart'})
        observer.consume('server', {'id': 11, 'error': {'code': -32600, 'message': 'private-token'}})
        self.assertEqual(observer.snapshot()['recent_diagnostics'][-1]['reason'], 'windows_setup_request_failed')
        self.assertNotIn('private', json.dumps(observer.snapshot()))
        self.assertTrue(observer.snapshot()['stream_complete'])

    def test_support_diagnostics_are_bounded_and_do_not_include_payloads(self):
        observer = RuntimeObserver(str(uuid4()))
        for i in range(40):
            observer.consume('client', {'id': i, 'method': 'thread/resume', 'params': {'prompt': 'private-prompt'}})
            observer.consume('server', {'id': i, 'error': {'code': -32600, 'message': 'private-token'}})
        events = observer.snapshot()['recent_diagnostics']
        self.assertEqual(len(events), 60)
        self.assertEqual(events[-1]['state'], 'failed')
        self.assertEqual(events[-1]['sequence'], 80)
        self.assertEqual(events[-1]['code'], -32600)
        self.assertNotIn('private', json.dumps(events))
        observer.consume('client', {'id': 50, 'method': 'thread/read'})
        observer.consume('server', {'id': 50, 'error': {'message': 'record catalog or history query changed; restart listing'}})
        self.assertEqual(observer.snapshot()['recent_diagnostics'][-1]['reason'], 'history_cursor_changed')

    def setUp(self):
        self.observer = RuntimeObserver(str(uuid4()))
        self.thread = str(uuid4())
        self.turn = str(uuid4())

    def send(self, method, params):
        self.observer.consume('server', {'method': method, 'params': params})

    def start(self):
        self.send('turn/started', {'threadId': self.thread, 'turn': {'id': self.turn, 'status': 'inProgress'}})

    def end(self, turn=None):
        self.send('turn/completed', {'threadId': self.thread, 'turn': {'id': turn or self.turn, 'status': 'completed'}})

    def test_initialize_success_is_ready_without_optional_notification(self):
        self.observer.consume('client', {'method': 'initialize', 'id': 1, 'params': {}})
        self.assertFalse(self.observer.snapshot()['initialized'])
        self.observer.consume('server', {'id': 1, 'result': {'userAgent': 'fixture'}})
        self.assertTrue(self.observer.snapshot()['initialized'])
        self.observer.consume('client', {'method': 'initialized'})
        self.assertTrue(self.observer.snapshot()['initialized'])

    def test_opt_out_events_taints_completeness(self):
        self.observer.consume('client', {'method': 'initialize', 'id': 1,
            'params': {'capabilities': {'optOutNotificationMethods': ['turn/started']}}})
        self.assertFalse(self.observer.snapshot()['stream_complete'])

    def test_different_turn_completion_does_not_clear_work(self):
        self.start()
        self.end(str(uuid4()))
        self.assertEqual(self.observer.snapshot()['active_turn_count'], 1)
        self.end()
        self.assertEqual(self.observer.snapshot()['active_turn_count'], 0)
        self.assertFalse(self.observer.snapshot()['safe_to_restart'])

    def test_parent_completed_leaves_running_child_active(self):
        self.start()
        child = str(uuid4())
        item = {'id': 'spawn-item', 'type': 'collabAgentToolCall',
                'agentsStates': {child: {'status': 'running', 'message': 'PRIVATE TEXT'}}}
        self.send('item/started', {'threadId': self.thread, 'item': item})
        self.send('item/completed', {'threadId': self.thread, 'item': item})
        self.end()
        state = self.observer.snapshot()
        self.assertEqual(state['activity'], 'active')
        self.assertEqual(state['active_child_count'], 1)
        self.assertNotIn('PRIVATE TEXT', json.dumps(state))

    def test_client_and_server_request_ids_do_not_collide(self):
        self.observer.consume('client', {'method': 'command/exec', 'id': 5, 'params': {'command': 'secret'}})
        self.observer.consume('server', {'method': 'item/tool/requestUserInput', 'id': 5, 'params': {}})
        self.observer.consume('server', {'id': 5, 'result': {}})
        self.assertEqual(self.observer.snapshot()['pending_server_request_count'], 1)
        self.observer.consume('client', {'id': 5, 'result': {}})
        self.assertEqual(self.observer.snapshot()['pending_server_request_count'], 0)

    def test_process_kill_response_does_not_count_as_exit(self):
        self.observer.consume('client', {'method': 'process/spawn', 'id': 1, 'params': {'processHandle': 'proc-1'}})
        self.observer.consume('server', {'id': 1, 'result': {}})
        self.observer.consume('client', {'method': 'process/kill', 'id': 2, 'params': {'processHandle': 'proc-1'}})
        self.observer.consume('server', {'id': 2, 'result': {}})
        self.assertEqual(self.observer.snapshot()['active_process_count'], 1)
        self.send('process/exited', {'processHandle': 'proc-1', 'exitCode': 0})
        self.assertEqual(self.observer.snapshot()['active_process_count'], 0)

    def test_thread_read_cannot_confirm_ui_selection_and_redacts_account(self):
        self.observer.consume('client', {'method': 'account/read', 'id': 'secret-opaque-id', 'params': {}})
        self.observer.consume('server', {'id': 'secret-opaque-id', 'result': {'account': {
            'type': 'chatgpt', 'email': 'Person@example.test', 'planType': 'pro', 'accessToken': 'SECRET'}}})
        self.observer.consume('client', {'method': 'thread/read', 'id': 2, 'params': {'threadId': self.thread}})
        self.observer.consume('server', {'id': 2, 'result': {'thread': {'id': self.thread, 'turns': []}}})
        state = self.observer.snapshot()
        self.assertEqual(state['last_thread_read'], self.thread)
        self.assertFalse(state['selection_verified'])
        self.assertFalse(state['account']['workspace_verified'])
        self.assertEqual(state['account']['email_fingerprint'], identity_fingerprint('person@example.test'))
        for secret in ('Person@example.test', 'SECRET', 'secret-opaque-id'):
            self.assertNotIn(secret, json.dumps(state))
        self.send('account/updated', {'authMode': 'chatgpt'})
        self.assertEqual(self.observer.snapshot()['account']['state'], 'unknown')

    def test_disconnection_or_idle_event_cannot_hide_outstanding_tool(self):
        self.send('item/started', {'threadId': self.thread, 'item': {'id': 'tool-1', 'type': 'mcpToolCall'}})
        self.send('thread/status/changed', {'threadId': self.thread, 'status': {'type': 'idle'}})
        self.assertEqual(self.observer.snapshot()['activity'], 'active')
        self.observer.disconnected()
        self.assertEqual(self.observer.snapshot()['activity'], 'unknown')

    def test_split_oversize_and_invalid_frames_are_bounded(self):
        decoder = JsonLineObserver(self.observer, 'client', limit=90)
        decoder.feed(b'{"method":"initialized"')
        self.assertEqual(self.observer.snapshot()['observed_message_count'], 0)
        decoder.feed(b'}\n')
        self.assertEqual(self.observer.snapshot()['observed_message_count'], 1)
        decoder.feed(b'x' * 1000)
        self.assertLessEqual(len(decoder.pending), 90)
        decoder.feed(b'\n')
        self.assertFalse(self.observer.snapshot()['stream_complete'])


if __name__ == '__main__':
    unittest.main()
