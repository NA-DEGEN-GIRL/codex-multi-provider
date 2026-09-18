"""Bootstrap shared project declarations and hydrate an inactive desktop HOME.

SQLite owns local project execution IDs. The small event registries own desktop
declarations/removals, including native deletion's undo interval. No thread,
credential, connection grant or live editor state is copied.
"""
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
from uuid import UUID

from .store import Store, atomic_json
from .updates import UpdateError, _lock_file, _unlock_file

WRITER = '00000000-0000-4000-8000-000000000049'
MAP = 'app-server-project-id-by-legacy-project-id-by-host'


def read_state(home):
    file = Path(home) / '.codex-global-state.json'
    if not file.is_file() or file.stat().st_size > 8 * 1024 * 1024:
        return {}
    return json.loads(file.read_text(encoding='utf-8-sig'))


def identity(value):
    return isinstance(value, str) and bool(re.fullmatch(
        r'(?:[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}|local-[a-f0-9]{32})', value))


def seed_local(root, canonical=None):
    store = Store(root)
    canonical = Path(canonical or Path.home() / '.codex').resolve()
    directory = store.directory / 'record-signals/local-workspaces'
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.glob('*.json')):
        return
    try:
        lock = _lock_file(directory / 'migration.lock')
    except UpdateError:
        return
    try:
        if any(directory.glob('*.json')):
            return
        database = canonical / 'state_5.sqlite'
        if not database.is_file():
            return
        aliases, known = {}, {}
        for home in [canonical] + [Path(p['home']) for p in store.read()['profiles']]:
            try:
                state = read_state(home)
            except (OSError, ValueError):
                continue
            mapping = state.get(MAP, {}).get('local:' + str(home), {})
            for legacy, native in mapping.items():
                if identity(legacy) and legacy in state.get('local-projects', {}):
                    aliases.setdefault(native, legacy)
                    known[legacy] = native
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
            db.execute('BEGIN')
            if not {'projects', 'project_roots'} <= {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
                return
            values, native_ids = {}, set()
            for native, name, created, updated in db.execute('SELECT id,name,created_at_ms,updated_at_ms FROM projects ORDER BY position,id'):
                UUID(native)
                native_ids.add(native)
                legacy = aliases.get(native, native)
                roots = [row[0] for row in db.execute('SELECT path FROM project_roots WHERE project_id=? ORDER BY position', (native,))]
                values[legacy] = dict(id=legacy, name=name, rootPaths=roots, createdAt=created,
                                      updatedAt=updated, serverId=native)
            for legacy, native in known.items():
                if native not in native_ids:
                    values.setdefault(legacy, None)
        atomic_json(directory / (WRITER + '.json'), dict(version=1,
            projects=[[key, 1, WRITER, value] for key, value in values.items()]))
    finally:
        _unlock_file(lock)


def records(directory, *, local):
    result = {}
    for file in list(directory.glob('*.json'))[:256]:
        try:
            UUID(file.stem)
            if file.stat().st_size > 4 * 1024 * 1024:
                continue
            data = json.loads(file.read_text(encoding='utf-8'))
            if data.get('version') != 1 or not isinstance(data.get('projects'), list) or len(data['projects']) > 4096:
                continue
            for key, seq, author, value in data['projects']:
                UUID(author)
                if not identity(key) or type(seq) is not int or not 1 <= seq < 2**53:
                    continue
                if value is not None:
                    if not isinstance(value, dict) or value.get('id') != key:
                        continue
                    if local:
                        if not isinstance(value.get('name'), str) or not isinstance(value.get('rootPaths'), list):
                            continue
                        if not all(isinstance(r, str) and len(r) <= 4096 for r in value['rootPaths']):
                            continue
                        if value.get('serverId'):
                            UUID(value['serverId'])
                    elif not (str(value.get('hostId', '')).startswith('remote-ssh-') and
                              str(value.get('remotePath', '')).startswith('/') and isinstance(value.get('label'), str)):
                        continue
                previous = result.get(key)
                if previous is None or (seq, author) > previous[:2]:
                    result[key] = (seq, author, value)
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return result


def prepare_home(root, home):
    """Call only before launch; running stores update through their own API."""
    seed_local(root)
    directory = Store(root).directory / 'record-signals'
    home = Path(home).resolve()
    state = read_state(home)
    before = json.dumps(state)
    local = records(directory / 'local-workspaces', local=True)
    remote = records(directory / 'workspaces', local=False)
    host = 'local:' + str(home)
    mapping = state.setdefault(MAP, {}).setdefault(host, {})
    projects = state.setdefault('local-projects', {})
    canonical = {r[2]['serverId']: key for key, r in local.items() if r[2] and r[2].get('serverId')}
    removed = set()
    for key in list(projects):
        if mapping.get(key) in canonical and canonical[mapping[key]] != key:
            del projects[key]
            removed.add(key)
    for key, (_, _, value) in local.items():
        if value is None:
            projects.pop(key, None)
            removed.add(key)
        else:
            projects[key] = {k: value[k] for k in ('id', 'name', 'rootPaths', 'createdAt', 'updatedAt') if k in value}
            if value.get('serverId'):
                mapping[key] = value['serverId']
    remotes = {p['id']: p for p in state.get('remote-projects', [])}
    for key, (_, _, value) in remote.items():
        if value is None:
            remotes.pop(key, None)
            removed.add(key)
        else:
            remotes[key] = {k: value[k] for k in ('id', 'hostId', 'remotePath', 'label')}
    state['remote-projects'] = list(remotes.values())
    order = [key for key in state.get('project-order', []) if key not in removed]
    state['project-order'] = list(dict.fromkeys(order + list(projects) + list(remotes)))
    from .project_membership import project_memberships
    project_memberships(state, home)
    if json.dumps(state) != before:
        atomic_json(home / '.codex-global-state.json', state)
