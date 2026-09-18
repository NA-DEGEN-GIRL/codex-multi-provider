import copy
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from control_center import ControlCenter
from manager_core.app_transport import AppTransport
from manager_core.catalog_refresh import CatalogRefresh
from manager_core.managed_sources import mark
from manager_core.runtime_admin import AdminError, sanitize_result
from manager_core.shared_catalog import environment
from manager_core import source_catalog
from manager_core.store import Store, atomic_json


CAPABILITIES = dict(native_record_catalog=True, shared_record_catalog=True,
                    paginated_record_catalog=True, mixed_source_catalog=True)


class SourceCatalogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = Store(self.root)
        self.one = self.store.add_profile('01')
        Path(self.one['home']).mkdir(parents=True)
        mark(self.one)
        self.legacy = self.root / 'original'
        self.legacy.mkdir()
        self.store.mutate(lambda data: Store._source(data, self.legacy, 'original:local', '기존 기록'))
        self.refresh = CatalogRefresh(self.root, builder=source_catalog.build)
        self.old = CatalogRefresh(self.root)

    def publish(self):
        return environment(self.store, self.one, CAPABILITIES, self.old, self.refresh)

    def projected(self, sid='original:local'):
        env = self.publish()
        shortcut = dict(thread_id=str(uuid4()), source_store_id=sid, host_id='local')
        path = Path(env['CODEX_MANAGER_SHARED_CATALOG'])
        projection = AppTransport(self.root)._projection(self.one, shortcut, env)
        native = next(key for key, source in source_catalog.registered(
            self.root, path, self.store.read()['sources']).items() if source['id'] == sid)
        thread = dict(id=projection, status={'type': 'notLoaded'}, path=None, canAcceptDirectInput=False,
                      extra={'managedRecord': dict(hostId='local', sourceStoreId=native,
                                                  canonicalThreadId=shortcut['thread_id'])})
        return path, shortcut, thread

    def test_real_instances_use_same_modern_descriptor_without_scanning_or_rewriting_sources(self):
        center = ControlCenter(self.root)
        sentinel = self.legacy / 'auth.json'
        sentinel.write_text('must not be read', encoding='utf-8')
        with patch('manager_core.record_catalog.metadata', side_effect=AssertionError('No scan')):
            one = environment(self.store, self.one, CAPABILITIES, center.instances.catalog_refresh,
                              center.instances.source_catalog_refresh)
            two = environment(self.store, {'id': str(uuid4())}, CAPABILITIES, center.instances.catalog_refresh,
                              center.instances.source_catalog_refresh)
        self.assertEqual(one, two)
        document = json.loads(Path(one['CODEX_MANAGER_SHARED_CATALOG']).read_text(encoding='utf-8'))
        self.assertEqual(document['version'], 3)
        self.assertEqual(len(document['sources']), 1)
        self.assertEqual(len(document['legacySources']), 1)
        self.assertEqual(sentinel.read_text(encoding='utf-8'), 'must not be read')
        self.assertEqual(sorted(p.name for p in self.legacy.iterdir()), ['auth.json'])

    def test_link_identity_round_trip_and_current_alias_survive_native_id_mapping(self):
        for sid in ('original:local', 'manager:' + self.one['id']):
            path, shortcut, thread = self.projected(sid)
            self.assertNotEqual(thread['id'], shortcut['thread_id'])
            self.store.mutate(lambda data: next(s for s in data['sources'] if s['id'] == sid).update(alias='수정한 별칭'))
            result = source_catalog.resolve(self.root, path, self.store.read()['sources'], thread)
            self.assertEqual(result['source_store_id'], sid)
            self.assertEqual(result['thread_id'], shortcut['thread_id'])
            self.assertEqual(result['source_alias'], '수정한 별칭')
            before = path.stat().st_mtime_ns
            self.publish()
            self.assertEqual(path.stat().st_mtime_ns, before)

    def test_old_runtime_and_running_generation_keep_separate_compatible_descriptors(self):
        old_env = environment(self.store, self.one, {**CAPABILITIES, 'mixed_source_catalog': False}, self.old)
        modern_env = self.publish()
        self.assertNotEqual(old_env, modern_env)
        running = {**self.one, 'generation': str(uuid4()), 'process_id': 7}
        self.assertEqual(environment(self.store, running, CAPABILITIES, self.old, self.refresh), old_env)
        running['shared_catalog_path'] = modern_env['CODEX_MANAGER_SHARED_CATALOG']
        self.assertEqual(environment(self.store, running, CAPABILITIES, self.old, self.refresh), modern_env)
        self.assertEqual(json.loads(Path(old_env['CODEX_MANAGER_SHARED_CATALOG']).read_text())['version'], 1)

    def test_new_profile_enrollment_refreshes_without_ui_requests_or_starting_work(self):
        center = ControlCenter(self.root)
        center.source_catalog_refresh.interval = .02
        with patch('control_center.runtime_build', return_value={'capabilities': CAPABILITIES}):
            center.source_catalog_refresh.start(center.native_catalog_sources)
            try:
                second = self.store.add_profile('02')
                self.assertFalse(Path(second['home']).exists())
                Path(second['home']).mkdir(parents=True)
                mark(second)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    path = source_catalog.catalog_path(self.root)
                    if path.exists() and len(json.loads(path.read_text())['sources']) == 2:
                        break
                    time.sleep(.02)
                else:
                    self.fail('Source was not enrolled without a UI request.')
                self.assertIsNone(self.store.profile(second['id'])['process_id'])
            finally:
                center.source_catalog_refresh.stop()

    def test_selected_record_resolves_using_only_verified_native_metadata(self):
        path, shortcut, thread = self.projected()
        self.store.mutate(lambda data: self.store.profile(self.one['id'], data).update(
            generation=str(uuid4()), shared_catalog_path=str(path)))
        center = ControlCenter(self.root)
        with patch('control_center.runtime_build', return_value={'capabilities': CAPABILITIES}), \
                patch('manager_core.runtime_admin.AdminClient') as admin:
            admin.return_value.request.return_value = {'thread': thread}
            result = center.dispatch('catalog.resolve', dict(viewer_profile_id=self.one['id'],
                                    projection_thread_id=thread['id']))
            self.assertEqual(result['thread_id'], shortcut['thread_id'])
            self.assertEqual(result['source_store_id'], shortcut['source_store_id'])
            admin.return_value.request.assert_called_once_with('thread/read',
                {'threadId': thread['id'], 'includeTurns': False})
            admin.return_value.request.return_value = {'thread': {**thread, 'id': str(uuid4())}}
            with self.assertRaises(ValueError):
                center.dispatch('catalog.resolve', dict(viewer_profile_id=self.one['id'], projection_thread_id=thread['id']))

    def test_unregistered_origin_modified_projection_and_marker_are_rejected(self):
        path, _, thread = self.projected()
        for mutation in ({'id': str(uuid4())}, {'canAcceptDirectInput': True}, {'path': 'somewhere'}):
            with self.assertRaises(ValueError):
                source_catalog.resolve(self.root, path, self.store.read()['sources'], {**thread, **mutation})
        sources = [s for s in self.store.read()['sources'] if s['id'] != 'original:local']
        with self.assertRaises(ValueError):
            source_catalog.resolve(self.root, path, sources, thread)
        before = path.read_bytes()
        atomic_json(Path(self.one['home']) / 'managed-source.json', {'host_id': 'local', 'store_id': 'manager:' + str(uuid4())})
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(path.read_bytes(), before)

    def test_admin_exposes_origin_but_never_history_or_extra_private_fields(self):
        _, _, thread = self.projected()
        raw = {**thread, 'turns': [{'secret': 'do not return'}], 'preview': 'private',
               'extra': {**thread['extra'], 'credential': 'private'}}
        clean = sanitize_result('thread/read', {'threadId': thread['id']}, {'thread': raw})['thread']
        self.assertEqual(clean, thread)
        malformed = copy.deepcopy(raw)
        malformed['extra']['managedRecord']['unexpected'] = 'private'
        with self.assertRaises(AdminError):
            sanitize_result('thread/read', {'threadId': thread['id']}, {'thread': malformed})
