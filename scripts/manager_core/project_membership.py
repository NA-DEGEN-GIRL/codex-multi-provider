"""Project common native membership into an inactive desktop's legacy cache."""
from contextlib import closing
from pathlib import Path
import sqlite3


def project_memberships(state, home, canonical=None):
    database = Path(canonical or Path.home() / '.codex') / 'state_5.sqlite'
    if not database.is_file():
        return
    host = 'local:' + str(Path(home).resolve())
    projects = state.get('local-projects', {})
    mapping = state.get('app-server-project-id-by-legacy-project-id-by-host', {}).get(host, {})
    aliases = {native: legacy for legacy, native in mapping.items() if legacy in projects}
    assignments = dict(state.get('thread-project-assignments', {}))
    projectless = set(state.get('projectless-thread-ids', []))
    migrations = state.get('app-server-projects-migration-by-host', {})
    migration = migrations.get(host, {})
    pending = set(migration.get('pendingThreadAssignmentIds', []))
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
        if 'project_id' not in {r[1] for r in db.execute('PRAGMA table_info(threads)')}:
            return
        for thread, native in db.execute('SELECT id,project_id FROM threads'):
            previous = assignments.get(thread)
            if previous is not None and previous.get('projectKind') != 'local':
                continue  # Remote/ChatGPT membership belongs to its own store.
            legacy = aliases.get(native)
            if thread in pending:
                desired = mapping.get(previous.get('projectId')) if previous else None
                if desired != native:
                    continue  # Preserve a legacy move that has not committed yet.
                pending.discard(thread)
            if legacy is not None:
                assignments[thread] = dict(projectKind='local', projectId=legacy)
                projectless.discard(thread)
            elif native is None and previous is not None:
                assignments.pop(thread)
                projectless.add(thread)
    state['thread-project-assignments'] = assignments
    state['projectless-thread-ids'] = sorted(projectless)
    if migration:
        state['app-server-projects-migration-by-host'] = {**migrations, host: {
            **migration, 'pendingThreadAssignmentIds': sorted(pending)}}
