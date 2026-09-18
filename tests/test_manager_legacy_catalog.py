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
        self.usage = self.root / 'usage/profile'
        self.usage.mkdir(parents=True)
        self.registry = self.root / '.config/llm-usage/config.json'
        self.accounts = [dict(id=str(uuid4()), provider='codex', alias='04', profile_dir=str(self.usage))]
        self.save_registry()

    def save_registry(self, version=3):
        atomic_json(self.registry, dict(schema_version=version, accounts=self.accounts))

    def discover(self):
        return legacy.discover(self.root, [self.managed], environ={})

    def test_stock_and_registry_versions_are_read_without_account_or_config_access(self):
        # Only the registry is opened; login/config/rollout files are not read.
        original = Path.open
        opened = []
        def open_path(path, *args, **kwargs):
            opened.append(path)
            self.assertEqual(path, self.registry)
            return original(path, *args, **kwargs)
        for version in (1, 2, 3):
            self.save_registry(version)
            with patch.object(Path, 'open', open_path):
                result = self.discover()
            self.assertEqual(result['errors'], [])
            self.assertEqual({s['home'] for s in result['sources']}, {str(self.stock), str(self.usage)})
        self.assertEqual(len(opened), 3)

    def test_other_providers_duplicate_homes_and_managed_homes_are_not_reenrolled(self):
        self.accounts.extend([
            dict(provider='claude', alias='Ignored', profile_dir='/not/read'),
            dict(provider='codex', alias='Same store', profile_dir=str(self.stock)),
            dict(provider='codex', alias='Managed', profile_dir=self.managed['home']),
        ])
        self.save_registry()
        self.assertEqual(len(self.discover()['sources']), 2)

    def test_custom_xdg_registry_and_unavailable_profile_have_stable_paths(self):
        old = self.registry
        self.registry = self.root / 'custom/llm-usage/config.json'
        self.accounts[0]['profile_dir'] = str(self.root / 'unavailable')
        self.save_registry()
        result = legacy.discover(self.root, [], environ={'XDG_CONFIG_HOME': str(self.root / 'custom')})
        self.assertFalse(result['errors'])
        self.assertIn(str(self.root / 'unavailable'), {s['home'] for s in result['sources']})
        self.assertTrue(old.is_file())

    def test_malformed_relative_or_oversize_registry_is_not_successful_deletion(self):
        for value in ([], {'schema_version': 99}, {'schema_version': 3, 'accounts': {}}):
            atomic_json(self.registry, value)
            self.assertEqual(self.discover()['errors'], ['llm_usage'])
        self.accounts[0]['profile_dir'] = 'relative/path'
        self.save_registry()
        self.assertEqual(self.discover()['errors'], ['llm_usage'])
        self.registry.write_bytes(b' ' * (1024 * 1024 + 1))
        self.assertEqual(self.discover()['errors'], ['llm_usage'])

    def test_rename_keeps_identity_and_a_changed_home_does_not_retarget_it(self):
        before = self.discover()['sources'][1]
        self.accounts[0]['alias'] = 'Personal'
        self.save_registry()
        renamed = self.discover()['sources'][1]
        self.assertEqual(before['id'], renamed['id'])
        self.assertEqual(renamed['alias'], 'Personal')
        self.accounts[0]['profile_dir'] = str(self.root / 'replacement')
        self.save_registry()
        self.assertNotEqual(before['id'], self.discover()['sources'][1]['id'])

    def test_shared_session_links_map_both_database_and_compact_index_to_original(self):
        for name in ('sessions', 'archived_sessions'):
            (self.stock / name).mkdir()
            try:
                (self.usage / name).symlink_to(self.stock / name, target_is_directory=True)
            except OSError as error:
                self.skipTest('Directory symlinks unavailable: ' + str(error))
        path = self.stock / 'sessions/fixture.jsonl'
        path.write_text('History must not be opened by origin resolution.', encoding='utf-8')
        sources = self.discover()['sources']
        stock, usage = sources
        homes, roots = legacy.origins(sources)
        self.assertEqual(legacy.origin_for({'rollout_path': str(path)}, usage['id'], homes, roots), stock['id'])
        self.assertEqual(legacy.origin_for({}, usage['id'], homes, roots), stock['id'])
        self.assertFalse((self.stock / 'managed-source.json').exists())

    def test_helper_reads_existing_legacy_rows_without_changing_source_bytes(self):
        profile_id = str(uuid4())
        managed_home = self.root / '.local/share/codex-control-center/profiles' / profile_id / 'codex'
        managed_home.mkdir(parents=True)
        managed = dict(id='manager:' + profile_id, home=str(managed_home))
        atomic_json(managed_home / 'managed-source.json', dict(host_id='local', store_id=managed['id']))
        tid = str(uuid4())
        index = self.usage / 'session_index.jsonl'
        index.write_text(json.dumps(dict(id=tid, title='Existing work', updated_at=1789300000)) + '\n', encoding='utf-8')
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = helper.read(dict(host_identity='a' * 64, sources=[managed], discover_legacy=True),
                             user_home=self.root, identity='a' * 64, environ={})
        self.assertFalse(result['errors'])
        self.assertFalse(result['discovery_errors'])
        self.assertEqual(result['conversations'][0]['thread_id'], tid)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})


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
