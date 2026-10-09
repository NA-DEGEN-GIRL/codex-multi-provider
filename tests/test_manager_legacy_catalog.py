"""Legacy SSH history discovery and shortcut identity behavior."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from control_center import ControlCenter
from manager_core import catalog
from manager_core.remote_catalog import RemoteCatalog
from manager_core.store import atomic_json
from test_manager_remote_catalog import register

spec = importlib.util.spec_from_file_location('catalog_legacy', ROOT / 'scripts/remote_helpers/catalog_legacy.py')
legacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy)
spec = importlib.util.spec_from_file_location('legacy_catalog_reader_test', ROOT / 'scripts/remote_helpers/catalog_reader.py')
helper = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, catalog=catalog, catalog_legacy=legacy):
    spec.loader.exec_module(helper)


class LegacyDiscoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.managed = dict(id='manager:' + str(uuid4()), home=str(self.root / 'managed'))
        self.stock = self.root / '.codex'
        self.stock.mkdir()
        self.imported = self.root / 'imported/profile'
        self.imported.mkdir(parents=True)

    def discover(self):
        return legacy.discover(self.root, [self.managed])

    def enroll(self, *homes):
        """Write the host's mixed catalog as a helper of an older release left it."""
        entries = []
        for home in homes:
            item = legacy.source(home, 'unused')
            entries.append(dict(hostId='local', sourceStoreId=item['id'], codexHome=item['home']))
        path = self.root / legacy.ENROLLED_CATALOG
        atomic_json(path, dict(version=3, hostId='local', sources=[], legacySources=entries,
                               managedSourcesPath=str(path.parent / 'catalog-sources.json')))
        return path

    def test_stock_home_is_found_without_opening_any_file(self):
        def open_path(path, *args, **kwargs):
            raise AssertionError('Discovery must not open ' + str(path))
        with patch.object(Path, 'open', open_path):
            result = self.discover()
        self.assertEqual(result['errors'], [])
        self.assertEqual([s['home'] for s in result['sources']], [str(self.stock)])
        self.assertEqual(result['sources'][0]['alias'], '기존 Codex')

    def test_missing_stock_home_is_an_empty_successful_discovery(self):
        self.stock.rmdir()
        self.assertEqual(self.discover(), dict(sources=[], errors=[]))

    def test_old_account_registry_is_ignored_whether_valid_invalid_or_oversize(self):
        # An old account registry under the default or a custom config root
        # never adds a home and never reports a discovery error.
        valid = dict(schema_version=3, accounts=[
            dict(id=str(uuid4()), provider='codex', alias='04', profile_dir=str(self.imported))])
        for config_root in (self.root / '.config', self.root / 'custom'):
            registry = config_root / 'llm-usage/config.json'
            for content in (json.dumps(valid).encode(), json.dumps({'schema_version': 99}).encode(),
                            b'not json', b' ' * (1024 * 1024 + 1)):
                with self.subTest(root=config_root.name, size=len(content)), \
                        patch.dict('os.environ', {'XDG_CONFIG_HOME': str(config_root)}):
                    registry.parent.mkdir(parents=True, exist_ok=True)
                    registry.write_bytes(content)
                    result = self.discover()
                    self.assertEqual(result['errors'], [])
                    self.assertEqual([s['home'] for s in result['sources']], [str(self.stock)])

    def test_homes_an_older_release_enrolled_stay_while_their_folders_exist(self):
        gone = self.root / 'imported/removed'
        gone.mkdir()
        self.enroll(self.stock, self.imported, gone)
        gone.rmdir()
        result = self.discover()
        self.assertEqual(result['errors'], [])
        self.assertEqual([(s['home'], s['alias']) for s in result['sources']],
                         [(str(self.stock), '기존 Codex'), (str(self.imported), '이전 연결 Codex · profile')])
        self.assertEqual(result['sources'][1]['id'], legacy.source(self.imported, '')['id'])
        # An enrolled home that is now a managed home is not listed again.
        managed = dict(id='manager:' + str(uuid4()), home=str(self.imported))
        self.assertEqual([s['home'] for s in legacy.discover(self.root, [managed])['sources']], [str(self.stock)])

    def test_unreadable_enrollment_is_reported_and_keeps_the_stock_home(self):
        path = self.enroll(self.imported)
        valid = json.loads(path.read_text(encoding='utf-8'))
        entry = valid['legacySources'][0]
        for name, content in (
                ('not json', b'not json'),
                ('version', json.dumps({**valid, 'version': 2}).encode()),
                ('host', json.dumps({**valid, 'hostId': 'other'}).encode()),
                ('identity', json.dumps({**valid, 'legacySources': [{**entry, 'sourceStoreId': 'legacy:' + '0' * 64}]}).encode()),
                ('relative', json.dumps({**valid, 'legacySources': [{**entry, 'codexHome': 'relative/home'}]}).encode()),
                ('extra field', json.dumps({**valid, 'legacySources': [{**entry, 'alias': 'x'}]}).encode()),
                ('oversize', b' ' * (legacy.MAX_ENROLLED_BYTES + 1))):
            with self.subTest(name):
                path.write_bytes(content)
                result = self.discover()
                self.assertEqual(result['errors'], ['llm_usage'])
                self.assertEqual([s['home'] for s in result['sources']], [str(self.stock)])

    def test_managed_stock_home_is_not_reenrolled(self):
        managed = dict(id='manager:' + str(uuid4()), home=str(self.stock))
        self.assertEqual(legacy.discover(self.root, [managed]), dict(sources=[], errors=[]))

    def test_identity_follows_the_resolved_home(self):
        first = self.discover()['sources'][0]
        self.assertEqual(first, self.discover()['sources'][0])
        other_home = self.root / 'other-user'
        (other_home / '.codex').mkdir(parents=True)
        moved = legacy.discover(other_home, [])['sources'][0]
        self.assertNotEqual(first['id'], moved['id'])

    def test_stock_error_is_reported_without_sources(self):
        real = Path.stat
        def stat(path, *args, **kwargs):
            if path == self.stock:
                raise PermissionError('fixture')
            return real(path, *args, **kwargs)
        with patch.object(Path, 'stat', stat):
            self.assertEqual(self.discover(), dict(sources=[], errors=['stock']))

    def test_shared_session_links_map_both_database_and_compact_index_to_original(self):
        managed_home = Path(self.managed['home'])
        managed_home.mkdir()
        for name in ('sessions', 'archived_sessions'):
            (self.stock / name).mkdir()
            try:
                (managed_home / name).symlink_to(self.stock / name, target_is_directory=True)
            except OSError as error:
                self.skipTest('Directory symlinks unavailable: ' + str(error))
        path = self.stock / 'sessions/fixture.jsonl'
        path.write_text('History must not be opened by origin resolution.', encoding='utf-8')
        stock, = self.discover()['sources']
        homes, roots = legacy.origins([self.managed, stock])
        self.assertEqual(legacy.origin_for({'rollout_path': str(path)}, self.managed['id'], homes, roots), stock['id'])
        self.assertEqual(legacy.origin_for({}, self.managed['id'], homes, roots), stock['id'])
        self.assertFalse((self.stock / 'managed-source.json').exists())

    def test_helper_reads_existing_legacy_rows_without_changing_source_bytes(self):
        profile_id = str(uuid4())
        managed_home = self.root / '.local/share/codex-control-center/profiles' / profile_id / 'codex'
        managed_home.mkdir(parents=True)
        managed = dict(id='manager:' + profile_id, home=str(managed_home))
        atomic_json(managed_home / 'managed-source.json', dict(host_id='local', store_id=managed['id']))
        tid = str(uuid4())
        index = self.stock / 'session_index.jsonl'
        index.write_text(json.dumps(dict(id=tid, title='Existing work', updated_at=1789300000)) + '\n', encoding='utf-8')
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = helper.read(dict(host_identity='a' * 64, sources=[managed], discover_legacy=True),
                             user_home=self.root, identity='a' * 64)
        self.assertFalse(result['errors'])
        self.assertFalse(result['discovery_errors'])
        self.assertEqual(result['conversations'][0]['thread_id'], tid)
        self.assertEqual(result['conversations'][0]['source_store_id'], legacy.source(self.stock, '')['id'])
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_helper_lists_rows_of_a_home_an_older_release_enrolled(self):
        # The manager's list and the runtime's mixed catalog name the same homes.
        profile_id = str(uuid4())
        managed_home = self.root / '.local/share/codex-control-center/profiles' / profile_id / 'codex'
        managed_home.mkdir(parents=True)
        managed = dict(id='manager:' + profile_id, home=str(managed_home))
        atomic_json(managed_home / 'managed-source.json', dict(host_id='local', store_id=managed['id']))
        self.enroll(self.stock, self.imported)
        tid = str(uuid4())
        (self.imported / 'session_index.jsonl').write_text(
            json.dumps(dict(id=tid, title='Imported work', updated_at=1789300000)) + '\n', encoding='utf-8')
        result = helper.read(dict(host_identity='a' * 64, sources=[managed], discover_legacy=True),
                             user_home=self.root, identity='a' * 64)
        imported = legacy.source(self.imported, '')['id']
        self.assertFalse(result['errors'])
        self.assertFalse(result['discovery_errors'])
        self.assertIn(imported, {s['id'] for s in result['discovered_sources']})
        self.assertEqual([(r['thread_id'], r['source_store_id']) for r in result['conversations']], [(tid, imported)])


class LegacyCacheTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.center = ControlCenter(self.root)
        self.store = self.center.store
        self.profile = self.store.add_profile('04')
        register(self.store, self.profile)
        home = '/home/fixture/.codex'
        self.source = dict(id='legacy:' + hashlib.sha256(home.encode()).hexdigest(), home=home, alias='기존 Codex')
        self.row = dict(thread_id=str(uuid4()), source_store_id=self.source['id'], title='Earlier work', updated_at=1)
        self.value = dict(conversations=[self.row], errors=[], discovered_sources=[self.source], discovery_errors=[])
        self.cache = RemoteCatalog(self.root, self.store, object(), reader=lambda _: copy.deepcopy(self.value), interval=.01)
        self.center.remote_catalog = self.cache
        self.addCleanup(self.cache.stop)

    def refresh(self):
        with self.cache._lock:
            self.cache._next.clear()
        self.cache.refresh()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self.cache._lock:
                if not self.cache._workers:
                    return
            time.sleep(.005)
        self.fail('Refresh did not finish')

    def add(self, row=None):
        row = row or self.row
        return self.center.dispatch('shortcut.add', dict(alias='My work', profile_id=self.profile['id'],
            host_id='ssh:remote-dev', thread_id=row['thread_id'], source_store_id=row['source_store_id']))

    def test_refresh_stays_passive_then_shortcut_pins_source_and_delete_only_removes_link(self):
        before = self.store.read()
        self.refresh()
        self.assertEqual(self.store.read(), before)
        self.assertEqual(self.center.dispatch('catalog.list', {})['conversations'][0]['source_alias'], '기존 Codex')
        link = self.add()
        source = next(s for s in self.store.read()['sources'] if s['id'] == self.source['id'])
        self.assertTrue(source['read_only'])
        self.assertEqual(source['host_identity'], 'a' * 64)
        self.center.dispatch('shortcut.delete', {'shortcut_id': link['id']})
        self.assertEqual(len(self.cache.snapshot()['conversations']), 1)

    def test_discovery_failure_and_offline_restart_keep_previous_records(self):
        self.refresh()
        self.value = dict(conversations=[], errors=[], discovered_sources=[], discovery_errors=['llm_usage'])
        self.refresh()
        restarted = RemoteCatalog(self.root, self.store, object())
        snapshot = restarted.snapshot()
        self.assertEqual(len(snapshot['conversations']), 1)
        self.assertTrue(snapshot['hosts'][0]['stale'])

    def test_successful_removal_updates_list_but_preserves_existing_task_link(self):
        self.refresh()
        link = self.add()
        self.value = dict(conversations=[], errors=[], discovered_sources=[], discovery_errors=[])
        self.refresh()
        self.assertFalse(self.cache.snapshot()['conversations'])
        self.assertEqual(self.store.read()['shortcuts'], [link])

    def test_title_and_alias_refresh_do_not_change_link_assignment(self):
        self.refresh()
        link = self.add()
        self.source['alias'] = 'Personal'
        self.row['title'] = 'Renamed work'
        self.refresh()
        row = self.cache.snapshot()['conversations'][0]
        self.assertEqual((row['title'], row['source_alias']), ('Renamed work', 'Personal'))
        self.assertEqual(self.store.read()['shortcuts'], [link])

    def test_unknown_legacy_source_or_mismatched_home_is_rejected_without_erasing_cache(self):
        self.refresh()
        self.source['home'] = '/home/fixture/replacement'
        self.refresh()
        self.assertTrue(self.cache.snapshot()['hosts'][0]['stale'])
        self.assertEqual(self.cache.snapshot()['conversations'][0]['source_home'], '/home/fixture/.codex')
        self.value['discovered_sources'] = []
        self.refresh()
        self.assertEqual(len(self.cache.snapshot()['conversations']), 1)

    def test_only_a_visible_qualified_thread_can_pin_an_unregistered_source(self):
        self.refresh()
        before = self.store.read()
        with self.assertRaises(ValueError):
            self.add({**self.row, 'thread_id': str(uuid4())})
        self.assertEqual(self.store.read(), before)

    def test_same_alias_on_replaced_host_cannot_retarget_a_pinned_source(self):
        self.refresh()
        link = self.add()
        self.store.mutate(lambda data: data['profiles'][0]['remote_bindings'][0].update(host_identity='b' * 64))
        self.refresh()
        with self.assertRaises(ValueError):
            self.add()
        self.assertEqual(self.store.read()['shortcuts'], [link])

    def test_host_change_between_selection_and_save_cannot_pin_the_old_host(self):
        self.refresh()
        source = self.cache.shortcut_source('ssh:remote-dev', self.source['id'], self.row['thread_id'])
        self.store.mutate(lambda data: data['profiles'][0]['remote_bindings'][0].update(host_identity='b' * 64))
        before = self.store.read()
        with self.assertRaises(ValueError):
            self.store.shortcut_add('Stale selection', self.profile['id'], self.row['thread_id'],
                'ssh:remote-dev', self.source['id'], catalog_source=source)
        self.assertEqual(self.store.read(), before)


if __name__ == '__main__':
    unittest.main()
