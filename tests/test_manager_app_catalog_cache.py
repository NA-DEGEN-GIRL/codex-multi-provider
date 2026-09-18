import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.app_catalog_cache import prepare


class AppCatalogCacheTests(unittest.TestCase):
    def test_canonical_storage_rebuilds_once_without_a_federated_catalog(self):
        prepare(self.home, self.env)
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("INSERT INTO local_thread_catalog VALUES('local','projection','Old')")
        direct = {'CODEX_RECORD_HOME': str(self.home/'canonical')}
        self.assertEqual(prepare(self.home,direct)['removed_cache_rows'], 1)
        self.assertEqual(prepare(self.home,direct)['state'], 'unchanged')

    def test_editable_identity_change_rebuilds_projection_cache_once(self):
        prepare(self.home, self.env)
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("INSERT INTO local_thread_catalog VALUES('local','projection','Task')")
        result = prepare(self.home, {**self.env, 'CODEX_MANAGER_SHARED_EXECUTION': '1'})
        self.assertEqual(result['removed_cache_rows'], 1)
        self.assertEqual(prepare(self.home, {**self.env, 'CODEX_MANAGER_SHARED_EXECUTION': '1'})['state'], 'unchanged')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / 'sqlite').mkdir()
        self.db = self.home / 'sqlite/codex-dev.db'
        with closing(sqlite3.connect(self.db)) as c, c:
            c.executescript('''
                CREATE TABLE local_thread_catalog(host_id TEXT, thread_id TEXT, display_title TEXT);
                CREATE TABLE local_thread_catalog_sync_state(host_id TEXT, watermark_updated_at INTEGER);
                CREATE TABLE local_thread_catalog_scan_checkpoints(host_id TEXT, checkpoint TEXT);
                CREATE TABLE local_thread_catalog_scan_entries(host_id TEXT, thread_id TEXT);
                CREATE TABLE local_thread_catalog_metadata(id INTEGER, catalog_revision INTEGER);
                CREATE TABLE settings(value TEXT);
                INSERT INTO settings VALUES ('preserved');
                INSERT INTO local_thread_catalog_metadata VALUES(1,7);
                INSERT INTO local_thread_catalog VALUES('local','old-projection','Same title');
                INSERT INTO local_thread_catalog VALUES('local','new-projection','Same title');
                INSERT INTO local_thread_catalog VALUES('ssh:host','remote','Remote title');
                INSERT INTO local_thread_catalog VALUES('chatgpt:account','chat','Chat title');
                INSERT INTO local_thread_catalog_sync_state VALUES('local',100);
                INSERT INTO local_thread_catalog_scan_checkpoints VALUES('local','old-cursor');
                INSERT INTO local_thread_catalog_scan_entries VALUES('local','old-projection');
            ''')
        self.catalog = self.home / 'catalog.json'
        self.catalog.write_text(json.dumps(dict(version=3, sources=[], legacySources=[])))
        self.env = dict(CODEX_MANAGER_SHARED_CATALOG=str(self.catalog))

    def rows(self, table):
        with closing(sqlite3.connect(self.db)) as c:
            return c.execute('SELECT * FROM ' + table).fetchall()

    def test_old_and_new_projection_cache_is_rebuilt_without_removing_other_data(self):
        result = prepare(self.home, self.env)
        self.assertEqual(result['removed_cache_rows'], 2)
        self.assertEqual(self.rows('local_thread_catalog'), [
            ('ssh:host', 'remote', 'Remote title'), ('chatgpt:account', 'chat', 'Chat title')])
        for table in ('local_thread_catalog_sync_state', 'local_thread_catalog_scan_entries', 'local_thread_catalog_scan_checkpoints'):
            self.assertEqual(self.rows(table), [])
        self.assertEqual(self.rows('local_thread_catalog_metadata'), [(1, 8)])
        self.assertEqual(self.rows('settings'), [('preserved',)])
        with closing(sqlite3.connect(result['databases'][0]['backup'])) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM local_thread_catalog').fetchone(), (4,))

    def test_restart_keeps_new_cache_but_record_mode_change_rebuilds_it(self):
        prepare(self.home, self.env)
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("INSERT INTO local_thread_catalog VALUES('local','new','New title')")
        self.assertEqual(prepare(self.home, self.env)['state'], 'unchanged')
        self.assertEqual(len(self.rows('local_thread_catalog')), 3)
        self.assertEqual(prepare(self.home, {})['removed_cache_rows'], 1)

    def test_identity_source_change_invalidates_cached_ids(self):
        prepare(self.home, self.env)
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("INSERT INTO local_thread_catalog VALUES('local','old','Old title')")
        self.catalog.write_text(json.dumps(dict(version=1, entries=[])))
        self.assertEqual(prepare(self.home, self.env)['removed_cache_rows'], 1)

    def test_schema_change_leaves_cache_and_settings_untouched(self):
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute('ALTER TABLE local_thread_catalog_scan_entries RENAME COLUMN host_id TO unknown_host')
        before = self.db.read_bytes()
        with self.assertRaises(ValueError):
            prepare(self.home, self.env)
        self.assertEqual(self.db.read_bytes(), before)

    def test_original_home_is_rejected(self):
        original = self.home / '.codex'
        original.mkdir()
        with patch('manager_core.app_catalog_cache.Path.home', return_value=self.home):
            with self.assertRaises(ValueError):
                prepare(original, self.env)

    def test_shared_database_file_is_not_modified(self):
        os.link(self.db, self.home / 'other-copy.db')
        before = self.db.read_bytes()
        with self.assertRaises(ValueError):
            prepare(self.home, self.env)
        self.assertEqual(self.db.read_bytes(), before)

    def test_showing_a_running_profile_does_not_touch_its_cache(self):
        from manager_core.instances import Instances
        instance = Instances.__new__(Instances)
        instance.store = SimpleNamespace(profile=lambda _: dict(id='profile'))
        instance.observe = lambda _: dict(status='running', window_handle=123)
        with patch('manager_core.app_catalog_cache.prepare') as migration:
            self.assertEqual(instance._show('profile', reopen_existing=False)['state'], 'existing')
            migration.assert_not_called()


if __name__ == '__main__':
    unittest.main()
