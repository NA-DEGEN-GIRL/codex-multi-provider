"""Share saved workspace declarations before launch; never share live UI state."""
from copy import deepcopy
from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3

from .source_catalog import projection_id


def _confirmed_threads(signals):
    """Per-task native membership proofs; no path means no evidence."""
    if signals is None:
        return set()
    from . import membership_proofs
    try:
        return membership_proofs.read(signals)
    except (OSError, TypeError, ValueError):
        return set()


def workspace_path(value):
    path = os.path.normcase(str(Path(value).resolve()))
    if path.startswith('\\\\?\\unc\\'):
        return '\\\\' + path[8:]
    return path[4:] if path.startswith('\\\\?\\') else path


def inferred_assignments(source, original, projects):
    """Reproduce legacy folder grouping from metadata, without reading bodies."""
    database = source / 'state_5.sqlite'
    if not database.is_file():
        return {}
    roots = [(workspace_path(root), legacy)
             for legacy, project in projects.items() for root in project['rootPaths']]
    projectless = set(original.get('projectless-thread-ids', []))
    hints = original.get('thread-workspace-root-hints', {})
    result = {}
    connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=5)
    try:
        connection.execute('PRAGMA query_only=ON')
        for index, (thread, cwd) in enumerate(connection.execute('SELECT id,cwd FROM threads LIMIT 100001')):
            if index >= 100000:
                raise ValueError('공유 프로젝트의 대화 분류 범위를 초과했습니다.')
            if thread in projectless:
                continue
            candidate = hints.get(thread) or cwd
            if not isinstance(candidate, str):
                continue
            path = workspace_path(candidate)
            matches = [(len(root), legacy) for root, legacy in roots
                       if path == root or path.startswith(root.rstrip('\\/') + os.sep)]
            if matches:
                deepest = max(length for length, _ in matches)
                ids = {legacy for length, legacy in matches if length == deepest}
                if len(ids) == 1:
                    result[thread] = ids.pop()
    finally:
        connection.close()
    return result


def merge_entries(current, donor, previous):
    """Refresh imported entries while retaining profile additions and edits."""
    result = deepcopy(current)
    for key, value in donor.items():
        if (key not in current and key not in previous) or current.get(key) == previous.get(key) or current.get(key) == value:
            result[key] = deepcopy(value)
    for key, value in previous.items():
        if key not in donor and current.get(key) == value:
            result.pop(key, None)
    return result


def current_workspace(source, original, *, signals=None):
    """After native migration, SQLite is newer than retained legacy JSON.

    Read a single source snapshot. A native NULL and an unimported legacy
    assignment are indistinguishable, so a retained legacy assignment is only
    cleared by a native NULL when the desktop confirmed its native read
    migration or a per-task proof recorded a successful native membership.
    Assignments the desktop still lists as pending survive either signal.
    """
    host = 'local:' + str(source)
    migration = original.get('app-server-projects-migration-by-host', {}).get(host, {})
    pending_ids = migration.get('pendingThreadAssignmentIds')
    pending_ids = set(pending_ids) if isinstance(pending_ids, list) else set()
    read_migrated = migration.get('threadAssignmentsReadMigrated') is True
    confirmed = _confirmed_threads(signals)
    database = source / 'state_5.sqlite'
    if not migration.get('projectsMigrated') or not database.is_file():
        return original
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=5)) as connection:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'projects', 'project_roots', 'threads'} <= tables:
            return original
        mapping = original.get('app-server-project-id-by-legacy-project-id-by-host', {}).get(host, {})
        inverse = {server: legacy for legacy, server in mapping.items()}
        projects, aliases, order = {}, {}, []
        for server, name, created, updated in connection.execute(
                'SELECT id,name,created_at_ms,updated_at_ms FROM projects ORDER BY position,id'):
            legacy = inverse.get(server, server)
            roots = [row[0] for row in connection.execute(
                'SELECT path FROM project_roots WHERE project_id=? ORDER BY position', (server,))]
            projects[legacy] = dict(id=legacy, name=name, rootPaths=roots, createdAt=created, updatedAt=updated)
            aliases[legacy] = server
            order.append(legacy)
        result = deepcopy(original)
        result['local-projects'] = projects
        result['project-order'] = order
        result.setdefault('app-server-project-id-by-legacy-project-id-by-host', {})[host] = aliases
        assignments = result.setdefault('thread-project-assignments', {})
        projectless = set(result.get('projectless-thread-ids', []))
        inverse = {server: legacy for legacy, server in aliases.items()}
        columns = {row[1] for row in connection.execute('PRAGMA table_info(threads)')}
        if 'project_id' in columns:
            for thread, project in connection.execute('SELECT id,project_id FROM threads'):
                if thread in pending_ids and (thread in assignments or thread in projectless):
                    # The desktop intentionally retains this explicit move
                    # until the native metadata write succeeds. Folder/SQLite
                    # fallback must not undo the user's pending assignment, and
                    # a recorded migration flag (true regardless of completion)
                    # must not either.
                    continue
                if project in inverse:
                    assignments[thread] = dict(projectKind='local', projectId=inverse[project])
                    projectless.discard(thread)
                elif project is None and (read_migrated or thread in confirmed):
                    # Only a confirmed native removal clears retained legacy
                    # membership. A project id this home cannot map is another
                    # store's membership, not evidence to drop the assignment.
                    assignments.pop(thread, None)
                    projectless.add(thread)
        result['projectless-thread-ids'] = list(projectless)
        return result


def merge_workspace(current, original, owned, source, *, ssh_ready_aliases=None, signals=None):
    source = Path(source).resolve()
    original = current_workspace(source, original, signals=signals)
    projects = {}
    for key, value in original.get('local-projects', {}).items():
        if (isinstance(value, dict) and value.get('id') == key
                and isinstance(value.get('name'), str)
                and isinstance(value.get('rootPaths'), list)
                and all(isinstance(root, str) for root in value['rootPaths'])):
            projects[key] = {field: value[field] for field in
                ('id', 'name', 'rootPaths', 'createdAt', 'updatedAt') if field in value}
    previous = owned.setdefault('workspace', {})
    updates = {'local-projects': projects}
    for key in ('project-appearances', 'electron-workspace-root-labels'):
        if isinstance(original.get(key), dict):
            updates[key] = original[key]
    native_id = 'legacy:' + hashlib.sha256(str(source).encode('utf-8')).hexdigest()
    assignments = {}
    canonical_assignments = inferred_assignments(source, original, projects)
    for thread, assignment in original.get('thread-project-assignments', {}).items():
        if (isinstance(assignment, dict) and assignment.get('projectKind') == 'local'
                and assignment.get('projectId') in projects):
            value = dict(projectKind='local', projectId=assignment['projectId'])
            try:
                assignments[projection_id(native_id, thread)] = value
            except ValueError:
                continue
            canonical_assignments[thread] = assignment['projectId']
    for thread, legacy in canonical_assignments.items():
        try:
            assignments[projection_id(native_id, thread)] = dict(projectKind='local', projectId=legacy)
        except ValueError:
            continue
    updates['thread-project-assignments'] = assignments
    connections = {}
    for value in original.get('codex-managed-remote-connections', []):
        if (isinstance(value, dict) and isinstance(value.get('hostId'), str)
                and value['hostId'].startswith('remote-ssh-')
                and (value.get('alias') or value.get('hostname'))):
            connections[value['hostId']] = {field: value.get(field) for field in
                ('hostId', 'displayName', 'source', 'alias', 'hostname', 'sshPort', 'identity')}
    key = 'codex-managed-remote-connections'
    old_connections = {v['hostId']: v for v in current.get(key, []) if isinstance(v, dict) and 'hostId' in v}
    # Analytics identity is app-owned and differs across installations.
    analytics = {k: v.get('connectionAnalyticsId') for k, v in old_connections.items()}
    normalized = {k: {f: v.get(f) for f in ('hostId', 'displayName', 'source', 'alias', 'hostname', 'sshPort', 'identity')}
                  for k, v in old_connections.items()}
    merged = merge_entries(normalized, connections, previous.get(key, {}))
    # Startup healing of known managed alias loss. A desktop save can drop a
    # previously imported alias-backed discovered connection while the donor
    # still declares it; merge_entries reads "missing but previously imported"
    # as a profile edit, so the entry would stay lost forever. The same save
    # clears the host's auto-connect value, so a surviving alias can also be
    # left without its donor preference. Saved declarations and preferences do
    # not depend on runtime readiness: first connect prepares missing bindings,
    # and maintenance may temporarily leave no prepared aliases.
    # Manual hostname declarations, profile edits, explicit False values and
    # donor removals keep their existing merge semantics; no tombstone schema
    # is introduced.
    healed, managed = {}, set()
    for host_id, value in connections.items():
        if host_id not in previous.get(key, {}):
            continue
        if value.get('source') != 'discovered' or not value.get('alias') or value.get('hostname') is not None:
            continue
        managed.add(host_id)
        if host_id not in normalized:
            healed[host_id] = value
    for host_id, value in healed.items():
        merged[host_id] = deepcopy(value)
    current[key] = [{**v, **({'connectionAnalyticsId': analytics[k]} if analytics.get(k) else {})}
                    for k, v in merged.items()]
    previous[key] = deepcopy(connections)
    auto = original.get('remote-connection-auto-connect-by-host-id', {})
    updates['remote-connection-auto-connect-by-host-id'] = {
        # Connection preference and remote runtime readiness are different state.
        # The SSH launcher prepares a missing profile binding on first connect.
        key: value
        for key, value in auto.items() if key in connections and isinstance(value, bool)}
    removed = set(previous.get('local-projects', {})) - set(projects)
    for key, donor in updates.items():
        current[key] = merge_entries(current.get(key, {}), donor, previous.get(key, {}))
        previous[key] = deepcopy(donor)
    for host_id in managed:
        auto_values = current.setdefault('remote-connection-auto-connect-by-host-id', {})
        if host_id not in auto_values and isinstance(auto.get(host_id), bool):
            auto_values[host_id] = auto[host_id]
    # Project declarations are common. A native importer changing updatedAt
    # must not turn a removed common project into a private profile addition.
    for key in removed:
        current['local-projects'].pop(key, None)
        current.get('project-appearances', {}).pop(key, None)
    current['thread-project-assignments'] = {thread: assignment for thread, assignment
        in current['thread-project-assignments'].items() if assignment.get('projectId') not in removed}
    previous['removed_project_ids'] = sorted((set(previous.get('removed_project_ids', [])) | removed) - set(projects))
    order = [key for key in original.get('project-order', []) if key in current['local-projects']]
    remote_ids = {project['id'] for project in current.get('remote-projects', []) if isinstance(project, dict) and 'id' in project}
    current['project-order'] = [key for key in dict.fromkeys(order + current.get('project-order', []) + list(current['local-projects']))
                                if key in current['local-projects'] or key in remote_ids]
    # Server project IDs belong to each HOME. Import only declarations; let the
    # native app import its own projects and record its own migration progress.
    source_maps = original.get('app-server-project-id-by-legacy-project-id-by-host', {})
    source_map = source_maps.get('local:' + str(source), {})
    aliases = dict(version=1, sourceHome=str(source),
        projects={server: legacy for legacy, server in source_map.items()
                  if legacy in projects and isinstance(server, str)},
        threadAssignments=canonical_assignments)
    return aliases, dict(projects=len(projects), ssh_connections=len(connections))


def remove_imported_projects(home, current, owned, sqlite_home=None):
    """Remove obsolete imported project links before this profile starts.

    Matches native project/delete: clear membership, retain every thread and
    rollout. Never mutate the source store or a live profile's database.
    """
    from .common import _assert_owned_path
    from .store import atomic_json
    home = Path(home).resolve()
    removed = set(owned.get('workspace', {}).get('removed_project_ids', []))
    host = 'local:' + str(home)
    mapping = current.get('app-server-project-id-by-legacy-project-id-by-host', {}).get(host, {})
    selected = {legacy: server for legacy, server in mapping.items() if legacy in removed}
    if not selected:
        return 0
    database = (Path(sqlite_home).resolve() if sqlite_home else home) / 'state_5.sqlite'
    _assert_owned_path(database, home)
    if not database.is_file():
        return 0
    with closing(sqlite3.connect(database, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('BEGIN IMMEDIATE')
        backup = {}
        for legacy, server in selected.items():
            row = connection.execute('SELECT * FROM projects WHERE id=?', (server,)).fetchone()
            if row:
                backup[legacy] = dict(project=dict(row), roots=[dict(r) for r in connection.execute(
                    'SELECT * FROM project_roots WHERE project_id=?', (server,))],
                    threads=[r[0] for r in connection.execute('SELECT id FROM threads WHERE project_id=?', (server,))])
        if backup:
            # A small reversible metadata record, without message bodies.
            archive = home / '.manager-removed-projects.json'
            _assert_owned_path(archive, home)
            import json
            saved = json.loads(archive.read_text(encoding='utf-8')) if archive.is_file() else {}
            saved.update(backup)
            atomic_json(archive, saved)
            for value in backup.values():
                server = value['project']['id']
                connection.execute('UPDATE threads SET project_id=NULL WHERE project_id=?', (server,))
                connection.execute('DELETE FROM projects WHERE id=?', (server,))
        connection.commit()
    for legacy in selected:
        mapping.pop(legacy, None)
    return len(backup)
