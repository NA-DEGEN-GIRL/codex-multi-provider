"""Migration checksum compatibility between a runtime build and the live SQLite stores; temporary stores only."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from manager_core import runtime_migrations as MIGRATIONS

REPO = Path(__file__).resolve().parents[1]

# Shaped like codex-rs/state: one directory per database, <VERSION>_<description>.sql.
SOURCES = {
    'migrations': {
        '0001_threads.sql': 'CREATE TABLE threads (\n    id TEXT PRIMARY KEY\n);\n',
        '0002_thread_titles.sql': 'ALTER TABLE threads ADD COLUMN title TEXT;\nCREATE INDEX threads_title ON threads(title);\n',
    },
    'logs_migrations': {
        '0001_logs.sql': 'CREATE TABLE logs (\n    id INTEGER PRIMARY KEY\n);\n',
    },
}
# Which store each directory migrates, as the runtime names them.
STORES = {'migrations': 'state_5.sqlite', 'logs_migrations': 'logs_2.sqlite'}


def write_sources(codex_rs, newline, sources=SOURCES):
    for directory, files in sources.items():
        folder = Path(codex_rs) / 'state' / directory
        folder.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (folder / name).write_bytes(text.replace('\n', newline).encode())
    return Path(codex_rs)


def make_store(path, rows=(), *, table=True, wal=False):
    """A store with sqlx's bookkeeping table; rows are (version, description, checksum[, success])."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        if wal:
            connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('CREATE TABLE threads (id TEXT PRIMARY KEY)')
        if table:
            connection.execute('CREATE TABLE _sqlx_migrations (version INTEGER PRIMARY KEY, description TEXT NOT NULL, '
                               'installed_on TEXT NOT NULL, success BOOLEAN NOT NULL, checksum BLOB NOT NULL, '
                               'execution_time INTEGER NOT NULL)')
            for row in rows:
                version, description, checksum, success = (tuple(row) + (True,))[:4]
                connection.execute('INSERT INTO _sqlx_migrations VALUES (?, ?, ?, ?, ?, ?)',
                                   (version, description, '2026-10-01 00:00:00', success, checksum, 1000))
        connection.commit()
    return path


def applied_by(migrations, directory):
    """The rows a runtime embedding `migrations` writes when it migrates that directory's store."""
    return [(item['version'], item['description'], bytes.fromhex(item['sha384']))
            for item in migrations if item['directory'] == directory]


def migrated_home(home, migrations):
    return [make_store(Path(home) / STORES[directory], applied_by(migrations, directory)) for directory in SOURCES]


class TemporaryTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.crlf = MIGRATIONS.source_migrations(write_sources(self.base / 'crlf/codex-rs', '\r\n'))
        self.lf = MIGRATIONS.source_migrations(write_sources(self.base / 'lf/codex-rs', '\n'))


class SourceMigrationTests(TemporaryTestCase):
    def test_every_migration_directory_is_read_with_sqlx_versions_and_descriptions(self):
        codex_rs = self.base / 'crlf/codex-rs'
        # Not migrations sqlx would embed.
        (codex_rs / 'state/migrations/README.md').write_text('notes', encoding='utf-8')
        (codex_rs / 'state/migrations/notes.sql').write_text('-- no version', encoding='utf-8')
        (codex_rs / 'state/migrations/draft_threads.sql').write_text('-- no number', encoding='utf-8')
        (codex_rs / 'state/unlisted_migrations').mkdir()
        (codex_rs / 'state/unlisted_migrations/0001_other.sql').write_text('SELECT 1;', encoding='utf-8')
        found = MIGRATIONS.source_migrations(codex_rs)
        expected = [
            ('migrations', 1, 'threads', 'migrations/0001_threads.sql'),
            ('migrations', 2, 'thread titles', 'migrations/0002_thread_titles.sql'),
            ('logs_migrations', 1, 'logs', 'logs_migrations/0001_logs.sql'),
        ]
        self.assertEqual(found, [dict(directory=directory, version=version, description=description,
                                      sha384=hashlib.sha384((codex_rs / 'state' / name).read_bytes()).hexdigest())
                                 for directory, version, description, name in expected])
        self.assertEqual(found, self.crlf)

    def test_line_endings_change_only_the_checksum(self):
        def identity(migrations):
            return [(item['directory'], item['version'], item['description']) for item in migrations]

        self.assertEqual(identity(self.crlf), identity(self.lf))
        for crlf, lf in zip(self.crlf, self.lf):
            self.assertNotEqual(crlf['sha384'], lf['sha384'])
        sample = self.base / 'crlf/codex-rs/state/migrations/0001_threads.sql'
        self.assertIn(b'\r\n', sample.read_bytes())
        self.assertEqual(self.crlf[0]['sha384'], hashlib.sha384(sample.read_bytes()).hexdigest())

    def test_missing_sources_embed_nothing(self):
        self.assertEqual(MIGRATIONS.source_migrations(self.base / 'absent/codex-rs'), [])


class CompatibilityTests(TemporaryTestCase):
    def setUp(self):
        super().setUp()
        self.state, self.logs = migrated_home(self.base / 'home', self.crlf)

    def test_crlf_runtime_accepts_stores_a_crlf_runtime_migrated(self):
        self.assertEqual(MIGRATIONS.incompatible_migrations(self.crlf, [self.state, self.logs]), [])
        self.assertEqual(MIGRATIONS.applied_migrations(self.state),
                         [(1, 'threads', self.crlf[0]['sha384']), (2, 'thread titles', self.crlf[1]['sha384'])])

    def test_lf_runtime_reports_every_crlf_applied_row(self):
        self.assertEqual(MIGRATIONS.incompatible_migrations(self.lf, [self.state, self.logs]), [
            '%s: 1 threads' % self.state, '%s: 2 thread titles' % self.state, '%s: 1 logs' % self.logs])
        # Either set of bytes is acceptable when the runtime carries both.
        self.assertEqual(MIGRATIONS.incompatible_migrations(self.lf + self.crlf, [self.state, self.logs]), [])

    def test_migrations_the_runtime_does_not_embed_are_ignored(self):
        # A newer runtime applied version 3; sqlx's ignore_missing lets this runtime open it anyway.
        store = make_store(self.base / 'newer/state_5.sqlite', applied_by(self.crlf, 'migrations') + [
            (3, 'thread archive', b'\x03' * 48)])
        self.assertEqual(MIGRATIONS.incompatible_migrations(self.crlf, [store]), [])
        self.assertEqual(MIGRATIONS.incompatible_migrations(self.lf, [store]),
                         ['%s: 1 threads' % store, '%s: 2 thread titles' % store])

    def test_failed_migrations_are_ignored(self):
        rows = applied_by(self.crlf, 'migrations')
        failed = make_store(self.base / 'failed/state_5.sqlite', [rows[0], rows[1][:2] + (b'\x00' * 48, False)])
        self.assertEqual(MIGRATIONS.applied_migrations(failed), [(1, 'threads', self.crlf[0]['sha384'])])
        self.assertEqual(MIGRATIONS.incompatible_migrations(self.crlf, [failed]), [])
        # The same row recorded as successful is reported.
        succeeded = make_store(self.base / 'succeeded/state_5.sqlite', [rows[0], rows[1][:2] + (b'\x00' * 48, True)])
        self.assertEqual(MIGRATIONS.incompatible_migrations(self.crlf, [succeeded]),
                         ['%s: 2 thread titles' % succeeded])

    def test_store_without_the_migration_table_has_nothing_applied(self):
        store = make_store(self.base / 'fresh/state_5.sqlite', table=False)
        self.assertEqual(MIGRATIONS.applied_migrations(store), [])
        self.assertEqual(MIGRATIONS.incompatible_migrations(self.lf, [store]), [])

    def test_unreadable_stores_are_skipped(self):
        corrupt = self.base / 'broken/state_5.sqlite'
        corrupt.parent.mkdir()
        corrupt.write_bytes(b'this is not an SQLite database\n' * 64)
        empty_table = make_store(self.base / 'other-schema/state_5.sqlite', table=False)
        with closing(sqlite3.connect(empty_table)) as connection:
            connection.execute('CREATE TABLE _sqlx_migrations (version INTEGER)')
            connection.commit()
        missing = self.base / 'missing/state_5.sqlite'
        folder = self.base / 'folder.sqlite'
        folder.mkdir()
        for path in (corrupt, empty_table, missing):
            with self.subTest(path=path.parent.name), self.assertRaises(sqlite3.Error):
                MIGRATIONS.applied_migrations(path)
        self.assertEqual(MIGRATIONS.incompatible_migrations(self.lf, [corrupt, empty_table, missing, folder, self.state]),
                         ['%s: 1 threads' % self.state, '%s: 2 thread titles' % self.state])
        # Read-only: nothing is created or repaired.
        self.assertFalse(missing.exists())
        self.assertFalse(missing.parent.exists())
        self.assertEqual(corrupt.read_bytes(), b'this is not an SQLite database\n' * 64)

    def test_reading_never_changes_the_store(self):
        before = self.state.read_bytes()
        MIGRATIONS.applied_migrations(self.state)
        MIGRATIONS.incompatible_migrations(self.lf, [self.state])
        self.assertEqual(self.state.read_bytes(), before)
        self.assertEqual(sorted(path.name for path in self.state.parent.iterdir()), ['logs_2.sqlite', 'state_5.sqlite'])

    def test_store_held_open_by_a_running_writer_is_still_read(self):
        # The live runtime keeps its WAL stores open, possibly inside a write transaction.
        store = make_store(self.base / 'live/state_5.sqlite', applied_by(self.crlf, 'migrations'), wal=True)
        writer = sqlite3.connect(store, isolation_level=None)
        self.addCleanup(writer.close)
        writer.execute('BEGIN IMMEDIATE')
        writer.execute("INSERT INTO _sqlx_migrations VALUES (3, 'uncommitted', 'now', 1, ?, 1)", (b'\x03' * 48,))
        self.assertEqual([row[:2] for row in MIGRATIONS.applied_migrations(store)], [(1, 'threads'), (2, 'thread titles')])
        self.assertEqual(MIGRATIONS.incompatible_migrations(self.lf, [store]),
                         ['%s: 1 threads' % store, '%s: 2 thread titles' % store])
        writer.execute('ROLLBACK')


class StorePathTests(TemporaryTestCase):
    def control_center(self, root):
        return root / 'work/control-center'

    def write_canonical(self, root, value):
        path = self.control_center(root) / 'canonical-storage.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        # Written by PowerShell/.NET as well: a BOM must not matter.
        path.write_text(json.dumps(value), encoding='utf-8-sig')

    def test_record_home_and_profile_homes_are_scanned(self):
        root = self.base / 'project'
        home = self.base / 'records'
        for name in ('state_5.sqlite', 'logs_2.sqlite', 'notes.txt', 'state_5.sqlite-wal', 'nested/goals_1.sqlite'):
            (home / name).parent.mkdir(parents=True, exist_ok=True)
            (home / name).write_bytes(b'')
        self.write_canonical(root, dict(version=1, home=str(home), state='complete'))
        profiles = self.control_center(root) / 'profiles'
        for name in ('beta/codex/queue_1.sqlite', 'alpha/codex/state_5.sqlite', 'alpha/codex/memories_1.sqlite',
                     'gamma/state_5.sqlite', 'delta/claude/state_5.sqlite'):
            (profiles / name).parent.mkdir(parents=True, exist_ok=True)
            (profiles / name).write_bytes(b'')
        (profiles / 'epsilon/codex').mkdir(parents=True)
        self.assertEqual(MIGRATIONS.store_paths(root), [
            home / 'logs_2.sqlite', home / 'state_5.sqlite',
            profiles / 'alpha/codex/memories_1.sqlite', profiles / 'alpha/codex/state_5.sqlite',
            profiles / 'beta/codex/queue_1.sqlite'])

    def test_missing_or_absent_record_home_contributes_nothing(self):
        root = self.base / 'project'
        profile = self.control_center(root) / 'profiles/alpha/codex'
        profile.mkdir(parents=True)
        (profile / 'state_5.sqlite').write_bytes(b'')
        self.assertEqual(MIGRATIONS.store_paths(root), [profile / 'state_5.sqlite'])
        for value in (dict(version=1), dict(home=None), dict(home=''), dict(home=str(self.base / 'gone'))):
            with self.subTest(value=value):
                self.write_canonical(root, value)
                self.assertEqual(MIGRATIONS.store_paths(root), [profile / 'state_5.sqlite'])
        self.assertEqual(MIGRATIONS.store_paths(self.base / 'empty'), [])

    def test_paths_with_url_characters_open_as_stores(self):
        # The real workspace is D:\#Programming\...; an unescaped '#' would cut the sqlite URI short
        # and the store would silently count as unreadable.
        root = self.base / '#Programming 100%' / 'codex-multi-provider'
        home = self.base / '#records home'
        self.write_canonical(root, dict(home=str(home)))
        state, logs = migrated_home(home, self.crlf)
        profile = migrated_home(self.control_center(root) / 'profiles/alpha/codex', self.crlf)
        self.assertEqual(MIGRATIONS.store_paths(root), [logs, state] + sorted(profile))
        self.assertEqual([row[:2] for row in MIGRATIONS.applied_migrations(state)], [(1, 'threads'), (2, 'thread titles')])
        MIGRATIONS.require_compatible(self.crlf, root)
        with self.assertRaises(RuntimeError) as raised:
            MIGRATIONS.require_compatible(self.lf, root)
        self.assertIn(str(state), str(raised.exception))
        self.assertIn(str(profile[0]), str(raised.exception))


class RequireCompatibleTests(TemporaryTestCase):
    def test_incompatible_store_is_named_and_at_most_five_rows_are_listed(self):
        root = self.base / 'project'
        home = self.base / 'records'
        (root / 'work/control-center').mkdir(parents=True)
        (root / 'work/control-center/canonical-storage.json').write_text(json.dumps(dict(home=str(home))),
                                                                        encoding='utf-8')
        runtime = [dict(directory='migrations', version=version, description='step %d' % version,
                        sha384=hashlib.sha384(b'runtime %d' % version).hexdigest()) for version in range(1, 8)]
        store = make_store(home / 'state_5.sqlite', [
            (item['version'], item['description'], hashlib.sha384(b'store %d' % item['version']).digest())
            for item in runtime])
        with self.assertRaisesRegex(RuntimeError, '^This runtime would refuse existing records') as raised:
            MIGRATIONS.require_compatible(runtime, root)
        message = str(raised.exception)
        for version in range(1, 6):
            self.assertIn('%s: %d step %d' % (store, version, version), message)
        for version in (6, 7):
            self.assertNotIn('step %d' % version, message)

    def test_workspace_without_stores_is_compatible(self):
        self.assertIsNone(MIGRATIONS.require_compatible(self.lf, self.base / 'project'))


class RepositoryRuntimeTests(unittest.TestCase):
    def test_runtime_sources_provide_all_six_migration_directories(self):
        codex_rs = REPO / 'runtime/codex-rs'
        if not (codex_rs / 'state').is_dir():
            self.skipTest('runtime/codex-rs/state is not checked out')
        found = MIGRATIONS.source_migrations(codex_rs)
        self.assertEqual({item['directory'] for item in found}, set(MIGRATIONS.MIGRATION_DIRECTORIES))
        self.assertEqual(len(MIGRATIONS.MIGRATION_DIRECTORIES), 6)
        for directory in MIGRATIONS.MIGRATION_DIRECTORIES:
            with self.subTest(directory=directory):
                files = sorted((codex_rs / 'state' / directory).glob('*.sql'))
                entries = [item for item in found if item['directory'] == directory]
                self.assertEqual(len(entries), len(files))
                self.assertEqual(len({item['version'] for item in entries}), len(entries))
                for item, path in zip(entries, files):
                    self.assertEqual(item['sha384'], hashlib.sha384(path.read_bytes()).hexdigest())
                    self.assertNotIn('_', item['description'])
        # The list mirrors the sqlx::migrate! directories the runtime compiles in.
        declared = codex_rs / 'state/src/migrations.rs'
        if declared.is_file():
            text = declared.read_text(encoding='utf-8')
            for directory in MIGRATIONS.MIGRATION_DIRECTORIES:
                self.assertIn('sqlx::migrate!("./%s")' % directory, text)
            self.assertEqual(text.count('sqlx::migrate!('), len(MIGRATIONS.MIGRATION_DIRECTORIES))


if __name__ == '__main__':
    unittest.main()
