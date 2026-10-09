"""Exercise the normal open command with real authority/manifests and fake RPCs.

Only GUI process creation and runtime transport are substituted. No real profile,
model, SSH host or original Codex window is touched.
"""
from contextlib import ExitStack
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from control_center import ControlCenter
from manager_core import authority
from manager_core.app_transport import utc_now
from manager_core.conversation_open import CONNECT_CAPABILITIES
from manager_core.handoff import HandoffManager
from manager_core.managed_sources import manifest, mark
from manager_core.store import Store, atomic_json
from test_manager_handoff import FakeAdmin


class ConversationOpenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.center = ControlCenter(self.root)
        self.store = self.center.store
        self.exe = self.root / 'ChatGPT.exe'
        self.exe.touch()
        self.a, self.b = (self.store.add_profile(alias) for alias in ('04', '02'))
        for index, profile in enumerate((self.a, self.b)):
            profile.update(generation=str(uuid4()), process_id=1000 + index,
                           executable_path=str(self.exe), runtime_channel='managed')
            Path(profile['home']).mkdir(parents=True)
            Path(profile['ui_home']).mkdir()
            mark(profile)
            self.store.mutate(lambda data, p=profile: self.store.profile(p['id'], data).update(p))
            atomic_json(self.store.directory / 'instances' / profile['id'] / 'runtime-state.json', {
                'schema_version': 1, 'profile_id': profile['id'], 'generation': profile['generation'],
                'observed_at': utc_now(), 'initialized': True, 'connected': True,
                'shared_catalog': {'enabled': True},
            })
        self.tid, self.child, self.peer = (str(uuid4()) for _ in range(3))
        self.scope = [self.tid, self.child]
        self.parents = {self.tid: None, self.child: self.tid, self.peer: None}
        self.home = Path(self.a['home'])
        self.ref = dict(thread_id=self.tid, host_id='local', source_store_id='manager:' + self.a['id'])
        for tid in self.scope:
            atomic_json(self.home / 'managed-authority' / (tid + '.json'), dict(
                version=1, host_id='local', store_id=self.ref['source_store_id'],
                thread_id=tid, owner_profile_id=self.a['id'], epoch=1, revision=1))
        for profile in (self.a, self.b):
            manifest(self.store, profile)
        self.admins = {p['id']: FakeAdmin(self, p, i + 10) for i, p in enumerate((self.a, self.b))}
        self.source, self.target = self.admins[self.a['id']], self.admins[self.b['id']]
        self.source.loaded = {*self.scope, self.peer}
        self.target.loaded = {str(uuid4())}
        self.target_peer = set(self.target.loaded)
        self.center.handoffs = HandoffManager(self.root, self.store,
                                             admin_factory=lambda p: self.admins[p['id']])
        self.link = self.store.shortcut_add('shared work', self.b['id'], **self.ref)
        self.projection = str(uuid4())
        self.catalog = self.store.directory / 'catalog/local-records.json'
        self.set_catalog(self.ref)
        self.launches, self.shown, self.window_waits = [], [], []
        stack = self.enterContext(ExitStack())
        self.capabilities = {key: True for key in (*CONNECT_CAPABILITIES,
                             'native_record_catalog', 'shared_record_catalog')}
        stack.enter_context(patch('control_center.runtime_build', return_value={'capabilities': self.capabilities}))
        stack.enter_context(patch.object(self.center.instances, 'show', side_effect=self.show))
        stack.enter_context(patch.object(self.center.instances, 'observe', side_effect=lambda p: {'status': 'running'}))
        stack.enter_context(patch.object(self.center.instances, 'environment', side_effect=self.environment))
        stack.enter_context(patch.object(self.center, '_wait_runtime'))
        stack.enter_context(patch('manager_core.app_transport.subprocess.Popen', side_effect=self.launch))

    def set_catalog(self, ref):
        atomic_json(self.catalog, dict(version=1, hostId='local', entries=[dict(
            projectionThreadId=self.projection, threadId=ref['thread_id'],
            sourceStoreId=ref['source_store_id'], hostId=ref['host_id'])]))

    def show(self, profile_id, *, reopen_existing=True, wait_for_window=True):
        self.assertFalse(reopen_existing, 'thread navigation must not send a separate window reopen')
        self.shown.append(profile_id)
        self.window_waits.append(wait_for_window)
        return dict(state='existing', profile=self.store.profile(profile_id))

    def environment(self, profile):
        return dict(CODEX_MANAGER_MANAGED_SOURCES=str(manifest(self.store, profile)),
                    CODEX_MANAGER_SHARED_CATALOG=str(self.catalog))

    def launch(self, args, **kwargs):
        self.launches.append((args, kwargs))
        return SimpleNamespace(pid=2000)

    def open(self):
        response = self.center.request(dict(id='open', command='conversation.open',
                                            args={'shortcut_id': self.link['id']}))
        self.assertTrue(response['ok'], response)
        return response['result']

    def assert_no_close(self, admin):
        self.assertFalse(any(method == 'thread/managedCloseIdle' for method, _ in admin.calls))

    def warmup(self):
        self.store.mutate(lambda data: Store._source(data, self.root / 'legacy', 'original:local', 'old'))
        self.ref = dict(thread_id=self.tid, host_id='local', source_store_id='original:local')
        self.set_catalog(self.ref)
        self.link = self.store.shortcut_add('cold start', self.b['id'], **self.ref)
        self.status_path = self.store.directory / 'instances' / self.b['id'] / 'runtime-state.json'
        self.ready_snapshot = __import__('json').loads(self.status_path.read_text())
        self.ready_snapshot.update(initialized=True, connected=True)
        atomic_json(self.status_path, {**self.ready_snapshot, 'initialized': False})
        result = self.open()
        self.assertEqual(result['state'], 'waiting_for_reader')
        self.assertFalse(self.launches)
        return result['navigation_id']

    def navigate(self, token):
        response = self.center.request(dict(id='navigate', command='conversation.navigate', args={'navigation_id': token}))
        self.assertTrue(response['ok'], response)
        return response['result']

    def test_cold_start_keeps_intent_and_sends_exact_link_once_after_initialize(self):
        token = self.warmup()
        for _ in range(4):
            self.assertEqual(self.navigate(token)['state'], 'waiting_for_reader')
        self.assertFalse(self.launches)
        self.assertEqual(len(self.shown), 1)
        atomic_json(self.status_path, {**self.ready_snapshot, 'stream_complete': False})
        result = self.navigate(token)
        self.assertEqual(result['state'], 'request_sent')
        self.assertEqual(result['uri'], 'codex://threads/' + self.projection)
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(self.navigate(token)['state'], 'blocked')
        self.assertEqual(len(self.launches), 1)
        self.assertFalse(any(a.calls for a in self.admins.values()))

    def test_open_reports_its_launch_phases_and_records_one_outcome_without_text(self):
        import json
        def launched(profile_id, **_):
            self.shown.append(profile_id)
            return dict(state='launched', profile=self.store.profile(profile_id))
        self.center.instances.show.side_effect = launched
        result = self.open()
        self.assertEqual(result['state'], 'request_sent')
        self.assertEqual(result['launch']['state'], 'launched')
        self.assertIsInstance(result['launch']['admission_ms'], int)
        self.assertIsInstance(result['launch']['launch_ms'], int)
        self.launches.clear()
        token = self.warmup()
        self.assertEqual(self.navigate(token)['launch']['state'], 'launched')
        path = self.store.directory / 'logs' / 'profile-launch.performance.jsonl'
        events = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        opens = [e for e in events if e['phase'] == 'conversation_open']
        self.assertEqual([e['code'] for e in opens], ['request_sent', 'waiting_for_reader:runtime_starting'])
        self.assertTrue(all(isinstance(e['launch_ms'], int) and e['profile_id'] == self.b['id'] for e in opens))
        # A poll that keeps waiting is not an outcome; nothing but fixed codes and numbers is kept.
        self.assertFalse([e for e in events if e['phase'] == 'conversation_navigate'])
        self.assertNotIn('message', json.dumps(events, ensure_ascii=False))
        self.assertNotIn(self.tid, path.read_text(encoding='utf-8'))

    def shared_execution(self):
        import json
        self.capabilities['shared_record_execution'] = True
        self.center.instances.environment.side_effect = lambda p: dict(
            CODEX_MANAGER_SHARED_CATALOG=str(self.catalog), CODEX_MANAGER_SHARED_EXECUTION='1')
        status = self.store.directory / 'instances' / self.b['id'] / 'runtime-state.json'
        atomic_json(status, {**json.loads(status.read_text()), 'shared_execution_version': 1})
        return status

    def test_click_on_closed_profile_leads_warmup_and_holds_new_launches_until_spawned(self):
        self.shared_execution()
        events = []
        warmup = self.center.profile_warmup
        stack = self.enterContext(ExitStack())
        stack.enter_context(patch.object(warmup, 'status', return_value={'worker_active': True}))
        stack.enter_context(patch.object(warmup, 'prioritize', side_effect=lambda p: events.append(('prioritize', p))))
        stack.enter_context(patch.object(warmup, 'hold', side_effect=lambda p: events.append(('hold', p)) or 'token'))
        stack.enter_context(patch.object(warmup, 'release', side_effect=lambda t: events.append(('release', t))))
        stack.enter_context(patch.object(self.center.update_hooks, 'prioritize_launch',
                                         side_effect=lambda p: events.append(('launch_queue', p))))
        def launched(profile_id, **options):
            events.append(('show', options.get('wait_for_window')))
            return dict(state='launched', profile=self.store.profile(profile_id))
        self.center.instances.show.side_effect = launched
        self.center.instances.observe.side_effect = lambda p: {'status': 'not_started'}
        result = self.open()
        self.assertEqual(result['state'], 'request_sent', result)
        # Led and held before the launch; released as soon as the process exists,
        # without the fixed window wait (the reader wait gates the link).
        self.assertEqual(events[:4], [('prioritize', self.b['id']), ('launch_queue', self.b['id']),
                                      ('hold', self.b['id']), ('show', False)])
        self.assertEqual(events[4], ('release', 'token'))

    def test_click_on_running_profile_never_holds_the_warmup(self):
        self.shared_execution()
        warmup = self.center.profile_warmup
        with patch.object(warmup, 'status', return_value={'worker_active': True}), \
                patch.object(warmup, 'hold') as hold, patch.object(warmup, 'prioritize') as prioritize:
            self.assertEqual(self.open()['state'], 'request_sent')
        hold.assert_not_called()
        prioritize.assert_not_called()
        self.assertEqual(self.window_waits, [False])

    def test_handoff_mode_keeps_the_window_wait_before_its_immediate_link(self):
        self.open()
        self.assertEqual(self.window_waits, [True])

    def test_moved_shortcut_rejects_captured_profile_before_any_open_or_handoff(self):
        expected = self.link['profile_id']
        self.store.shortcut_move(self.link['id'], self.a['id'])
        response = self.center.request(dict(id='open-captured', command='conversation.open',
            args={'shortcut_id': self.link['id'], 'expected_profile_id': expected}))
        self.assertTrue(response['ok'], response)
        self.assertEqual('shortcut_profile_changed', response['result']['reason'])
        self.assertEqual(expected, response['result']['profile_id'])
        self.assertFalse(self.shown)
        self.assertFalse(self.launches)
        self.assertFalse(any(a.calls for a in self.admins.values()))

    def test_captured_profile_allows_matching_shortcut(self):
        response = self.center.request(dict(id='open-captured', command='conversation.open',
            args={'shortcut_id': self.link['id'], 'expected_profile_id': self.b['id']}))
        self.assertTrue(response['ok'], response)
        self.assertEqual('request_sent', response['result']['state'])
        self.assertEqual([self.b['id']], self.shown)

    def test_shared_editing_opens_canonical_task_without_closing_other_account(self):
        self.capabilities['shared_record_execution'] = True
        self.source.busy = True
        self.center.instances.environment.side_effect = lambda p: dict(
            CODEX_MANAGER_SHARED_CATALOG=str(self.catalog), CODEX_MANAGER_SHARED_EXECUTION='1')
        status = self.store.directory / 'instances' / self.b['id'] / 'runtime-state.json'
        import json
        data = json.loads(status.read_text())
        atomic_json(status, {**data, 'shared_execution_version': 1})
        result = self.open()
        self.assertEqual(result['access_mode'], 'ready', result)
        self.assertEqual(result['uri'], 'codex://threads/' + self.tid)
        self.assertFalse(result['readonly_projection'])
        self.assertFalse(any(a.calls for a in self.admins.values()))
        self.assertEqual(self.source.loaded, {*self.scope, self.peer})

    def test_changed_profile_or_shortcut_cancels_waiting_navigation(self):
        for change in ('generation', 'assignment', 'delete'):
            with self.subTest(change=change):
                token = self.warmup()
                if change == 'generation':
                    self.store.mutate(lambda data: self.store.profile(self.b['id'], data).update(generation=str(uuid4())))
                elif change == 'assignment':
                    self.store.shortcut_move(self.link['id'], self.a['id'])
                else:
                    self.store.shortcut_delete(self.link['id'])
                self.assertEqual(self.navigate(token)['reason'], 'navigation_changed')
                self.assertFalse(self.launches)

    def test_expired_or_uncertain_navigation_is_not_replayed(self):
        token = self.warmup()
        self.center.navigations.pending[token]['expires'] = 0
        self.assertEqual(self.navigate(token)['reason'], 'navigation_expired')
        token = self.warmup()
        atomic_json(self.status_path, self.ready_snapshot)
        with patch('manager_core.app_transport.subprocess.Popen', side_effect=OSError('fixture uncertain launch')):
            response = self.center.request(dict(id='fail', command='conversation.navigate', args={'navigation_id': token}))
        self.assertFalse(response['ok'])
        self.assertEqual(self.navigate(token)['reason'], 'navigation_expired')
        self.assertFalse(self.launches)

    def test_normal_open_connects_same_parent_and_child_once_and_preserves_peers(self):
        result = self.open()
        self.assertEqual(result['access_mode'], 'ready', result)
        self.assertEqual(result['state'], 'request_sent')
        self.assertFalse(result['selection_verified'])
        self.assertEqual(result['profile_id'], self.b['id'])
        for tid in self.scope:
            self.assertEqual(authority.read(self.home, tid)['owner_profile_id'], self.b['id'])
        self.assertEqual(self.source.loaded, {self.peer})
        self.assertEqual(self.target.loaded, self.target_peer)
        self.assertEqual(self.launches[-1][0][-1], 'codex://threads/' + self.tid)
        grants = [authority.read(self.home, tid) for tid in self.scope]
        repeated = self.open()
        self.assertEqual(repeated['access_mode'], 'ready')
        self.assertEqual(grants, [authority.read(self.home, tid) for tid in self.scope])
        self.assertEqual(sum(method == 'thread/managedCloseIdle' for method, _ in self.source.calls), 1)
        # FakeAdmin rejects every unexpected RPC, including turn/start and interrupt.
        self.assertFalse(any(method.startswith('turn/') for a in self.admins.values() for method, _ in a.calls))

    def test_busy_open_uses_live_projection_without_stopping_or_changing_writer(self):
        self.source.idle = False
        before = {tid: authority.read(self.home, tid) for tid in self.scope}
        result = self.open()
        self.assertEqual(result['access_mode'], 'view_only', result)
        self.assertEqual(result['view_reason'], 'source_busy')
        self.assertEqual(result['profile_id'], self.b['id'])
        self.assertEqual(self.launches[0][0][-1], 'codex://threads/' + self.projection)
        self.assertEqual(self.launches[0][1]['env']['CODEX_HOME'], self.b['home'])
        self.assertEqual(before, {tid: authority.read(self.home, tid) for tid in self.scope})
        self.assertEqual(self.source.loaded, {*self.scope, self.peer})
        self.assert_no_close(self.source)
        self.assertFalse((self.store.directory / 'handoffs').exists())

    def test_return_to_storage_profile_is_readonly_while_other_account_is_busy(self):
        self.open()
        self.target.loaded.update(self.scope)
        self.target.idle = False
        self.center.dispatch('shortcut.move', dict(shortcut_id=self.link['id'], profile_id=self.a['id']))
        busy = self.open()
        self.assertEqual(busy['access_mode'], 'view_only', busy)
        self.assertEqual(self.launches[-1][0][-1], 'codex://threads/' + self.projection)
        self.assertEqual(authority.read(self.home, self.tid)['owner_profile_id'], self.b['id'])
        self.assert_no_close(self.target)
        self.target.idle = True
        ready = self.open()
        self.assertEqual(ready['access_mode'], 'ready', ready)
        self.assertEqual(self.launches[-1][0][-1], 'codex://threads/' + self.tid)
        self.assertEqual(self.source.loaded, {self.peer})
        self.assertEqual(self.target.loaded, self.target_peer)
        for tid in self.scope:
            self.assertEqual(authority.read(self.home, tid)['owner_profile_id'], self.a['id'])

    def test_list_and_assignment_changes_do_not_contact_runtimes(self):
        before = [authority.read(self.home, tid) for tid in self.scope]
        self.center.dispatch('catalog.list', {})
        self.center.dispatch('shortcut.move', dict(shortcut_id=self.link['id'], profile_id=self.a['id']))
        self.assertEqual(before, [authority.read(self.home, tid) for tid in self.scope])
        self.assertFalse(self.launches or self.shown)
        self.assertFalse(any(a.calls for a in self.admins.values()))

    def test_unavailable_writer_is_only_viewed_and_never_started(self):
        self.source.ready = False
        result = self.open()
        self.assertEqual(result['access_mode'], 'view_only', result)
        self.assertEqual(self.shown, [self.b['id']])
        self.assertEqual(authority.read(self.home, self.tid)['owner_profile_id'], self.a['id'])
        self.assert_no_close(self.source)

    def test_uncertain_close_is_viewed_without_replaying_the_operation(self):
        self.source.close_error = RuntimeError('fixture uncertain close')
        first = self.open()
        self.assertEqual(first['access_mode'], 'view_only', first)
        self.assertEqual(first['account_connection']['status'], 'recovery_required')
        repeated = self.open()
        self.assertEqual(repeated['view_reason'], 'recovery_required')
        self.assertEqual(sum(method == 'thread/managedCloseIdle' for method, _ in self.source.calls), 1)
        self.assertEqual(authority.read(self.home, self.tid)['owner_profile_id'], self.a['id'])

    def test_missing_projection_does_not_fall_back_to_a_canonical_busy_thread(self):
        self.source.idle = False
        self.catalog.unlink()
        result = self.open()
        self.assertEqual(result['access_mode'], 'unavailable', result)
        self.assertEqual(result['state'], 'blocked')
        self.assertFalse(self.launches)
        self.assert_no_close(self.source)

    def test_legacy_record_view_does_not_attempt_execution_transfer(self):
        self.store.mutate(lambda data: Store._source(data, self.root / 'legacy', 'original:local', 'old'))
        self.ref = dict(thread_id=self.tid, host_id='local', source_store_id='original:local')
        self.set_catalog(self.ref)
        self.link = self.store.shortcut_add('legacy', self.b['id'], **self.ref)
        result = self.open()
        self.center._wait_runtime.assert_not_called()
        self.assertEqual(result['access_mode'], 'view_only', result)
        self.assertFalse(any(a.calls for a in self.admins.values()))

    def test_failed_native_link_does_not_report_success_or_repeat_a_completed_connection(self):
        with patch('manager_core.app_transport.subprocess.Popen', side_effect=OSError('fixture missing app')):
            response = self.center.request(dict(id='open', command='conversation.open',
                                               args={'shortcut_id': self.link['id']}))
        self.assertFalse(response['ok'])
        self.assertEqual(authority.read(self.home, self.tid)['owner_profile_id'], self.b['id'])
        self.assertEqual(self.open()['access_mode'], 'ready')
        self.assertEqual(sum(method == 'thread/managedCloseIdle' for method, _ in self.source.calls), 1)

    def test_unverified_ssh_shortcut_does_not_launch_or_change_execution(self):
        remote_ref = {**self.ref, 'host_id': 'ssh:remote-dev', 'source_store_id': 'remote-fixture'}
        self.store.mutate(lambda data: data['sources'].append(dict(
            id=remote_ref['source_store_id'], host_id=remote_ref['host_id'],
            home='/home/fixture/.codex', alias='SSH fixture')))
        self.link = self.store.shortcut_add('remote work', self.b['id'], **remote_ref)
        result = self.open()
        # Without shared execution the chosen profile is shown, but no thread
        # link is sent and no handoff or viewer touches another store.
        self.assertEqual(result['reason'], 'exact_remote_navigation_unverified')
        self.assertFalse(self.launches)
        self.assertFalse(any(a.calls for a in self.admins.values()))

    def shared_ssh_shortcut(self):
        """A shared-execution SSH task of profile b whose desktop declares the host."""
        import json
        self.capabilities['shared_record_execution'] = True
        self.center.instances.environment.side_effect = lambda p: dict(
            CODEX_MANAGER_SHARED_CATALOG=str(self.catalog), CODEX_MANAGER_SHARED_EXECUTION='1')
        status = self.store.directory / 'instances' / self.b['id'] / 'runtime-state.json'
        atomic_json(status, {**json.loads(status.read_text()), 'shared_execution_version': 1})
        atomic_json(Path(self.b['home']) / '.codex-global-state.json', {'codex-managed-remote-connections': [
            {'hostId': 'remote-ssh-discovered:remote-dev', 'alias': 'remote-dev'}]})
        remote_ref = {**self.ref, 'host_id': 'ssh:remote-dev', 'source_store_id': 'remote-fixture'}
        self.store.mutate(lambda data: data['sources'].append(dict(
            id=remote_ref['source_store_id'], host_id=remote_ref['host_id'],
            home='/home/fixture/.codex', alias='SSH fixture')))
        self.link = self.store.shortcut_add('remote work', self.b['id'], **remote_ref)
        return self.store.directory / 'instances' / self.b['id'] / 'ssh-activity-remote-dev-4242.json'

    def test_ssh_task_link_waits_for_the_desktops_connection_to_that_host(self):
        # Incident: the link reached a starting desktop before it registered
        # the SSH host and 26.930 quit with "Connection for host ID ... not found".
        activity = self.shared_ssh_shortcut()
        result = self.open()
        self.assertEqual(result['state'], 'waiting_for_reader')
        self.assertEqual(result['reason'], 'remote_connection_starting')
        self.assertEqual(result['access_mode'], 'preparing')
        token = result['navigation_id']
        # SSH preparation can follow the local runtime by more than the local wait.
        self.assertGreater(self.center.navigations.pending[token]['expires'] - self.center.navigations.clock(), 60)
        for _ in range(3):
            self.assertEqual(self.navigate(token)['reason'], 'remote_connection_starting')
        self.assertFalse(self.launches)
        # The native SSH proxy of this launch runs the desktop's connection.
        atomic_json(activity, {'schema': 1, 'generation': self.b['generation'],
                               'observed_at': time.time(), 'thread_activity': {}})
        result = self.navigate(token)
        self.assertEqual(result['state'], 'request_sent', result)
        self.assertEqual(result['uri'], 'codex://threads/' + self.tid + '?hostId=remote-ssh-discovered%3Aremote-dev')
        self.assertEqual(len(self.launches), 1)
        self.assertFalse(any(a.calls for a in self.admins.values()))

    def test_ssh_task_link_is_not_sent_when_the_connection_never_starts(self):
        self.shared_ssh_shortcut()
        token = self.open()['navigation_id']
        self.center.navigations.pending[token]['expires'] = 0
        result = self.navigate(token)
        self.assertEqual(result['state'], 'blocked')
        self.assertEqual(result['reason'], 'remote_connection_timeout')
        self.assertFalse(self.launches)

    def test_packaged_profile_keeps_ordinary_own_conversation_open(self):
        self.store.mutate(lambda data: self.store.profile(self.a['id'], data).update(runtime_channel='packaged'))
        self.store.shortcut_move(self.link['id'], self.a['id'])
        result = self.open()
        self.assertEqual(result['access_mode'], 'ready', result)
        self.assertEqual(self.launches[0][0][-1], 'codex://threads/' + self.tid)
        self.assertFalse(any(a.calls for a in self.admins.values()))

    def test_missing_idle_capability_allows_viewing_without_attempting_connection(self):
        self.capabilities.pop('managed_idle_status')
        result = self.open()
        self.assertEqual(result['access_mode'], 'view_only', result)
        self.assertFalse(any(a.calls for a in self.admins.values()))


if __name__ == '__main__':
    unittest.main()
