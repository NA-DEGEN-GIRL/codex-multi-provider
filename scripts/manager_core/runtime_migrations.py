"""Will a runtime accept the SQLite stores it is about to open?

sqlx refuses to start when an applied migration's checksum (SHA-384 of the SQL
file bytes, line endings included) differs from the one compiled into the
runtime. A runtime built from LF sources therefore cannot open stores that a
CRLF-built Windows runtime migrated, and the reverse: the Linux cross build of
revision 118 exited at startup with "failed to initialize sqlite state runtime".
The headless validations use fresh stores and cannot see this, so staging and
activation compare the candidate's migrations with the live stores.
"""
import hashlib
import json
from pathlib import Path
import sqlite3

# codex-rs/state/src/migrations.rs: one sqlx::migrate! directory per database.
MIGRATION_DIRECTORIES = ('migrations', 'logs_migrations', 'goals_migrations', 'memory_migrations',
                         'queue_migrations', 'thread_history_migrations')
# Store file name prefix (state_5.sqlite, logs_2.sqlite, ...) -> its migrator.
STORE_DIRECTORIES = (('thread_history_', 'thread_history_migrations'), ('state_', 'migrations'),
                     ('logs_', 'logs_migrations'), ('goals_', 'goals_migrations'),
                     ('memories_', 'memory_migrations'), ('queue_', 'queue_migrations'))


def source_migrations(codex_rs):
    """The (version, description, sha384) sqlx embeds from these sources."""
    found = []
    for name in MIGRATION_DIRECTORIES:
        for path in sorted((Path(codex_rs) / 'state' / name).glob('*.sql')):
            version, separator, description = path.stem.partition('_')
            if not separator or not version.isdigit():
                continue
            found.append(dict(directory=name, version=int(version), description=description.replace('_', ' '),
                              sha384=hashlib.sha384(path.read_bytes()).hexdigest()))
    return found


def store_paths(root):
    """SQLite stores a managed runtime opens: the shared record home and profile homes."""
    root = Path(root)
    homes = []
    canonical = root / 'work/control-center/canonical-storage.json'
    if canonical.is_file():
        home = json.loads(canonical.read_text(encoding='utf-8-sig')).get('home')
        if home:
            homes.append(Path(home))
    homes += sorted((root / 'work/control-center/profiles').glob('*/codex'))
    return [path for home in homes if home.is_dir() for path in sorted(home.glob('*.sqlite'))]


def applied_migrations(path):
    """Applied (version, description, checksum hex) rows; read-only, never migrates."""
    connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    try:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' "
                                  "AND name = '_sqlx_migrations'").fetchone():
            return []
        return [(version, description, bytes(checksum).hex()) for version, description, checksum in
                connection.execute('SELECT version, description, checksum FROM _sqlx_migrations WHERE success')]
    finally:
        connection.close()


def incompatible_migrations(migrations, stores):
    """Applied migrations the runtime also embeds, but with different bytes.

    Like sqlx, a known store compares by version within its own migrator, so a
    renamed migration still counts. Unknown store names fall back to (version,
    description). Migrations unknown to the runtime are ignored, as the runtime
    ignores them (ignore_missing). An unreadable store is not this check's concern.
    """
    by_directory, by_description = {}, {}
    for item in migrations:
        by_directory.setdefault((item['directory'], item['version']), set()).add(item['sha384'])
        by_description.setdefault((item['version'], item['description']), set()).add(item['sha384'])
    problems = []
    for path in stores:
        directory = next((name for prefix, name in STORE_DIRECTORIES
                          if Path(path).name.startswith(prefix)), None)
        try:
            rows = applied_migrations(path)
        except (OSError, sqlite3.Error):
            continue
        for version, description, checksum in rows:
            expected = (by_directory.get((directory, version)) if directory is not None
                        else by_description.get((version, description)))
            if expected is not None and checksum not in expected:
                problems.append('%s: %d %s' % (path, version, description))
    return problems


def require_compatible(migrations, root):
    problems = incompatible_migrations(migrations, store_paths(root))
    if problems:
        raise RuntimeError('This runtime would refuse existing records: applied migrations differ from the '
                           'ones it embeds (line endings?). ' + '; '.join(problems[:5]))
