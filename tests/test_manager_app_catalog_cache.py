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
from manager_core.app_catalog_cache import SSH_CANONICAL_EPOCH


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

    def remote_rows(self, host):
        return [row for row in self.rows('local_thread_catalog') if row[0] == host]

    def seed_ssh_host(self, host, *, title='Remote title'):
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("INSERT INTO local_thread_catalog VALUES(?,?,?)", (host, host + ':1', title))
            c.execute("INSERT INTO local_thread_catalog_sync_state VALUES(?,42)", (host,))
            c.execute("INSERT INTO local_thread_catalog_scan_checkpoints VALUES(?,?)", (host, 'cursor-' + host))
            c.execute("INSERT INTO local_thread_catalog_scan_entries VALUES(?,?)", (host, host + ':1'))

    def test_managed_ssh_host_is_cleared_once_scope_only_and_backed_up(self):
        host = 'remote-ssh-discovered:ubuntu-dev'
        prepare(self.home, self.env)
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("INSERT INTO local_thread_catalog VALUES('local','kept','Local kept')")
        self.seed_ssh_host(host)
        result = prepare(self.home, self.env, ssh_hosts=[host])
        self.assertEqual(result['removed_cache_rows_local'], 0)
        self.assertEqual(result['removed_cache_rows_remote'], 1)
        self.assertEqual(result['hosts'], {host: 1})
        self.assertEqual(result['skipped_hosts'], [])
        self.assertIn(('local', 'kept', 'Local kept'), self.rows('local_thread_catalog'))
        self.assertEqual(self.remote_rows(host), [])
        self.assertEqual(self.remote_rows('ssh:host'), [('ssh:host', 'remote', 'Remote title')])
        self.assertEqual(self.remote_rows('chatgpt:account'), [('chatgpt:account', 'chat', 'Chat title')])
        for table in ('local_thread_catalog_sync_state', 'local_thread_catalog_scan_checkpoints',
                      'local_thread_catalog_scan_entries'):
            self.assertEqual([row for row in self.rows(table) if row[0] == host], [])
        self.assertEqual(self.rows('local_thread_catalog_metadata'), [(1, 9)])
        self.assertEqual(self.rows('settings'), [('preserved',)])
        with closing(sqlite3.connect(result['databases'][0]['backup'])) as backup:
            self.assertIn((host, host + ':1', 'Remote title'),
                          backup.execute('SELECT * FROM local_thread_catalog').fetchall())
        again = prepare(self.home, self.env, ssh_hosts=[host])
        self.assertEqual(again['state'], 'unchanged')
        self.assertEqual(again['databases'], [])
        self.assertEqual(again['removed_cache_rows_remote'], 0)
        self.assertEqual(self.rows('local_thread_catalog_metadata'), [(1, 9)])

    def test_local_signature_change_does_not_reclear_managed_ssh_hosts(self):
        host = 'remote-ssh-discovered:ubuntu-dev'
        prepare(self.home, self.env, ssh_hosts=[host])
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("INSERT INTO local_thread_catalog VALUES('local','after','Local after')")
            c.execute("INSERT INTO local_thread_catalog VALUES(?,?,?)", (host, 'after', 'Remote after'))
        # A canonical-storage launch without the federated catalog descriptor
        # changes only the local identity signature.
        result = prepare(self.home, {'CODEX_RECORD_HOME': str(self.home / 'canonical')}, ssh_hosts=[host])
        self.assertEqual(result['removed_cache_rows_local'], 1)
        self.assertEqual(result['removed_cache_rows_remote'], 0)
        self.assertEqual(self.remote_rows('local'), [])
        self.assertEqual(self.remote_rows(host), [(host, 'after', 'Remote after')])

    def test_host_discovered_later_is_cleared_exactly_once(self):
        first, later = 'remote-ssh-discovered:alpha', 'remote-ssh-discovered:beta'
        prepare(self.home, self.env, ssh_hosts=[first])
        self.seed_ssh_host(later)
        result = prepare(self.home, self.env, ssh_hosts=[first, later])
        self.assertEqual(result['removed_cache_rows_remote'], 1)
        self.assertEqual(result['hosts'], {later: 1})
        self.assertEqual(self.remote_rows(later), [])
        self.assertEqual([row for row in self.rows('local_thread_catalog_sync_state') if row[0] == later], [])
        self.assertEqual(prepare(self.home, self.env, ssh_hosts=[first, later])['state'], 'unchanged')

    def test_unmanaged_and_invalid_hosts_are_skipped_without_changes(self):
        before = {table: self.rows(table) for table in ('settings', 'local_thread_catalog')}
        result = prepare(self.home, self.env,
                         ssh_hosts=['local', 'chatgpt:account', 'other-host', '', None, 7, 'x' * 257])
        self.assertEqual(result['removed_cache_rows_remote'], 0)
        self.assertEqual(result['state'], 'rebuilt')  # only the first local rebuild
        self.assertEqual(result['removed_cache_rows_local'], 2)
        self.assertEqual({item['reason'] for item in result['skipped_hosts']}, {'unmanaged_host'})
        self.assertEqual(len(result['skipped_hosts']), 7)
        self.assertEqual(self.rows('settings'), before['settings'])
        self.assertEqual(self.remote_rows('ssh:host'), [('ssh:host', 'remote', 'Remote title')])
        self.assertEqual(self.remote_rows('chatgpt:account'), [('chatgpt:account', 'chat', 'Chat title')])

    def test_legacy_flat_marker_upgrades_without_reclearing_local(self):
        host = 'remote-ssh-discovered:ubuntu-dev'
        # A marker written before SSH epochs existed: one flat local signature.
        prepare(self.home, self.env)
        marker = self.home / '.manager-sidebar-cache.json'
        saved = json.loads(marker.read_text(encoding='utf-8'))
        signature = saved[self.db.name]
        self.assertIsInstance(signature, str)
        self.assertNotIn('__ssh_canonical__', saved)
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("INSERT INTO local_thread_catalog VALUES('local','kept','Local kept')")
        self.seed_ssh_host(host)
        result = prepare(self.home, self.env, ssh_hosts=[host])
        self.assertEqual(result['removed_cache_rows_local'], 0)
        self.assertEqual(result['removed_cache_rows_remote'], 1)
        self.assertIn(('local', 'kept', 'Local kept'), self.rows('local_thread_catalog'))
        rewritten = json.loads(marker.read_text(encoding='utf-8'))
        self.assertEqual(rewritten[self.db.name], signature)
        self.assertEqual(rewritten['__ssh_canonical__'][self.db.name][host], SSH_CANONICAL_EPOCH)
        self.assertEqual(prepare(self.home, self.env, ssh_hosts=[host])['state'], 'unchanged')

    def test_flat_marker_stays_readable_for_the_older_running_service(self):
        host = 'remote-ssh-discovered:ubuntu-dev'
        prepare(self.home, self.env, ssh_hosts=[host])
        marker = self.home / '.manager-sidebar-cache.json'
        saved = json.loads(marker.read_text(encoding='utf-8'))
        signature = saved[self.db.name]
        # The older cold-launch service writes this value back verbatim and
        # passes unknown top-level keys through untouched.
        saved[self.db.name] = signature
        saved['__other_service__'] = {'keep': True}
        marker.write_text(json.dumps(saved), encoding='utf-8')
        result = prepare(self.home, self.env, ssh_hosts=[host])
        self.assertEqual(result['state'], 'unchanged')
        self.assertEqual(result['removed_cache_rows_local'], 0)
        self.assertEqual(result['removed_cache_rows_remote'], 0)
        rewritten = json.loads(marker.read_text(encoding='utf-8'))
        # An older service comparing its own signature with marker[db] still
        # sees equality, so it never clears the local rows a second time.
        self.assertEqual(rewritten[self.db.name], signature)
        self.assertEqual(rewritten['__other_service__'], {'keep': True})
        self.assertEqual(rewritten['__ssh_canonical__'][self.db.name][host], SSH_CANONICAL_EPOCH)

    def test_invalid_host_prefixes_are_skipped(self):
        before = self.db.read_bytes()
        invalid = ['ssh:host', 'remote-ssh:host', 'remote-sshX:host', 'remote-ssh-discovered:',
                   'remote-ssh-discovered:bad alias', 'remote-ssh-discovered:' + 'a' * 200,
                   'other-host', '', None, 7]
        result = prepare(self.home, self.env, ssh_hosts=invalid)
        self.assertEqual(result['removed_cache_rows_remote'], 0)
        self.assertEqual({item['reason'] for item in result['skipped_hosts']}, {'unmanaged_host'})
        self.assertEqual(len(result['skipped_hosts']), len(invalid))
        self.assertEqual(result['hosts'], {'local': 2})
        self.assertEqual(self.remote_rows('ssh:host'), [('ssh:host', 'remote', 'Remote title')])
        marker = json.loads((self.home / '.manager-sidebar-cache.json').read_text(encoding='utf-8'))
        self.assertNotIn('__ssh_canonical__', marker)
        self.assertNotEqual(self.db.read_bytes(), before)  # only the first local rebuild happened
        self.assertEqual(result['removed_cache_rows_local'], 2)

    def test_malformed_schema_blocks_the_remote_clear_without_writes(self):
        host = 'remote-ssh-discovered:ubuntu-dev'
        self.seed_ssh_host(host)
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute('ALTER TABLE local_thread_catalog_scan_entries RENAME COLUMN host_id TO unknown_host')
        before = self.db.read_bytes()
        with self.assertRaises(ValueError):
            prepare(self.home, self.env, ssh_hosts=[host])
        self.assertEqual(self.db.read_bytes(), before)
        self.assertFalse((self.home / '.manager-sidebar-cache.json').exists())

    def test_malformed_second_database_leaves_the_first_untouched(self):
        host = 'remote-ssh-discovered:ubuntu-dev'
        other = self.home / 'sqlite/codex-other.db'
        with closing(sqlite3.connect(other)) as c, c:
            c.executescript('''
                CREATE TABLE local_thread_catalog(host_id TEXT, thread_id TEXT, display_title TEXT);
                CREATE TABLE local_thread_catalog_sync_state(host_id TEXT, watermark_updated_at INTEGER);
                CREATE TABLE local_thread_catalog_scan_checkpoints(host_id TEXT, checkpoint TEXT);
                CREATE TABLE local_thread_catalog_scan_entries(host_id TEXT, thread_id TEXT);
                CREATE TABLE local_thread_catalog_metadata(id INTEGER, catalog_revision INTEGER);
                INSERT INTO local_thread_catalog VALUES('local','old','Local');
                INSERT INTO local_thread_catalog_metadata VALUES(1,3);
            ''')
        with closing(sqlite3.connect(other)) as c, c:
            c.execute('ALTER TABLE local_thread_catalog_sync_state RENAME COLUMN host_id TO unknown_host')
        before_first = self.db.read_bytes()
        before_second = other.read_bytes()
        with self.assertRaises(ValueError):
            prepare(self.home, self.env, ssh_hosts=[host])
        self.assertEqual(self.db.read_bytes(), before_first)
        self.assertEqual(other.read_bytes(), before_second)

    def test_hardlinked_database_is_not_cleared_for_a_managed_host(self):
        host = 'remote-ssh-discovered:ubuntu-dev'
        os.link(self.db, self.home / 'other-copy.db')
        before = self.db.read_bytes()
        with self.assertRaises(ValueError):
            prepare(self.home, self.env, ssh_hosts=[host])
        self.assertEqual(self.db.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
