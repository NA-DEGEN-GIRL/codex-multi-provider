"""Publish native conversation fork ancestry for shared task notes.

The note service is a separate process and cannot read the Codex state
databases, so the manager maintains one small mapping file:
``work/control-center/note-forks.json`` with ``child thread -> parent thread``.
Native conversation forks are recorded in rollout session metadata, not in
``thread_spawn_edges`` (which tracks spawned agents). Only the first metadata
line is read; conversation messages are neither read nor published.

SSH forks come from the manager's SSH catalog cache, whose remote reader
reports each task's ``forked_from_id`` from the same first line. Their keys are
``ssh:<host alias>\\0<child>`` so a remote fork never inherits from another
host or from a local task with the same id.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import urllib.parse
from uuid import UUID

MAX_ENTRIES = 20000
_SEEN = {}
_META = {}
_CATALOGS = {}
_LOCK = threading.RLock()
MAX_META_BYTES = 4 * 1024 * 1024
SSH_PREFIXES = ('ssh:', 'remote-ssh-discovered:')


def _database(home):
    return Path(home) / 'state_5.sqlite'


def _stamp(path):
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


def _subagent(source):
    if isinstance(source, str):
        try:
            source = json.loads(source)
        except ValueError:
            return source in ('subagent', 'sub_agent')
    return isinstance(source, dict) and any(key in source for key in ('subagent', 'sub_agent'))


def _metadata_parent(child, rollout, *, strict=False):
    def unavailable():
        if strict:
            raise RuntimeError('Task fork metadata is not ready; retry loading notes')
        return None

    if not isinstance(rollout, str) or not rollout:
        return unavailable()
    path = Path(rollout)
    key = (os.path.normcase(str(path)), child)
    # A rollout's first session_meta identity never changes when turns append.
    # Cache completed headers, including non-forks, without statting every file.
    if key in _META:
        return _META[key]
    try:
        with path.open('rb') as stream:
            raw = stream.readline(MAX_META_BYTES + 1)
        if len(raw) > MAX_META_BYTES or not raw.endswith(b'\n'):
            return unavailable()  # Retry metadata that is still being written.
        record = json.loads(raw)
        metadata = record.get('payload', {})
        if (record.get('type') != 'session_meta' or not isinstance(metadata, dict)
                or metadata.get('id') != child or _subagent(metadata.get('source'))):
            return unavailable()
        parent = metadata.get('forked_from_id')
        parent = parent if _uuid(parent) and parent != child else None
    except (OSError, ValueError, AttributeError):
        return unavailable()
    if len(_META) >= MAX_ENTRIES * 2:
        _META.clear()
    _META[key] = parent
    return parent


def _parents(database):
    """Read native fork metadata, including threads committed only to the WAL."""
    key = os.path.normcase(str(database))
    stamp = (_stamp(database), _stamp(Path(str(database) + '-wal')))
    cached = _SEEN.get(key)
    if cached is not None and cached[0] == stamp:
        if not cached[3]:
            return cached[2]
        rows = cached[1]
    else:
        # immutable=1 skips live WAL content and can miss newly forked threads.
        uri = 'file:' + urllib.parse.quote(str(database).replace('\\', '/')) + '?mode=ro'
        connection = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            columns = {row[1] for row in connection.execute('PRAGMA table_info(threads)')}
            if not {'id', 'rollout_path'}.issubset(columns):
                rows = []
            else:
                source = 'source' if 'source' in columns else "NULL"
                rows = list(connection.execute(f'SELECT id, rollout_path, {source} FROM threads'))
        finally:
            connection.close()
    found = {}
    pending = False
    for child, rollout, source in rows:
        if _uuid(child) and not _subagent(source):
            parent = _metadata_parent(child, rollout)
            if parent:
                found[child] = parent
            pending |= (os.path.normcase(str(rollout)), child) not in _META
    _SEEN[key] = (stamp, rows, found, pending)
    return found


def _task_parents(database, thread_id):
    """Resolve one ancestry chain without scanning any unrelated rollouts."""
    uri = 'file:' + urllib.parse.quote(str(database).replace('\\', '/')) + '?mode=ro'
    connection = sqlite3.connect(uri, uri=True, timeout=2)
    found = {}
    known = False
    try:
        columns = {row[1] for row in connection.execute('PRAGMA table_info(threads)')}
        if not {'id', 'rollout_path'}.issubset(columns):
            return found, known
        source = 'source' if 'source' in columns else 'NULL'
        visited = set()
        while thread_id not in visited and len(visited) < 64:
            visited.add(thread_id)
            row = connection.execute(f'SELECT rollout_path, {source} FROM threads WHERE id=?',
                                     (thread_id,)).fetchone()
            if row is None:
                break
            known = True
            if _subagent(row[1]):
                break
            parent = _metadata_parent(thread_id, row[0], strict=True)
            if not parent:
                break
            found[thread_id] = parent
            thread_id = parent
        return found, known
    finally:
        connection.close()


def homes(root, state=None):
    root = Path(root)
    if state is None:
        from .store import Store
        state = Store(root).read()
    canonical = Path.home() / '.codex'
    result = []
    # The canonical home participates only when the manager registered it;
    # an unrelated store must never read the user's real Codex databases.
    if any(source.get('host_id') == 'local' and source.get('home')
           and Path(source['home']).resolve() == canonical.resolve()
           for source in state.get('sources', [])):
        result.append(canonical)
    for source in state.get('sources', []):
        if source.get('host_id') == 'local' and source.get('home'):
            result.append(Path(source['home']))
    for profile in state.get('profiles', []):
        if profile.get('home'):
            result.append(Path(profile['home']))
    unique = {}
    for home in result:
        unique[os.path.normcase(str(home))] = home
    return list(unique.values())


def ssh_alias(host_id):
    """Return the SSH host alias of a note task host, or None for local."""
    prefix = next((p for p in SSH_PREFIXES if isinstance(host_id, str) and host_id.startswith(p)), None)
    if prefix is None:
        return None
    if len(host_id) > 256 or len(host_id) == len(prefix) or any(ord(c) < 32 for c in host_id):
        raise ValueError('Invalid note fork host')
    return host_id[len(prefix):]


def ssh_key(alias, thread_id):
    return 'ssh:' + alias + '\0' + thread_id


def _ssh_parents(root, group):
    """Read SSH fork edges from the manager's own catalog cache.

    Returns ``(edges, resolved)`` or None when the cache is missing, belongs to
    another source scope, or cannot be read. ``resolved`` holds every task
    whose rollout header the remote reader has already inspected.
    """
    if group['conflicted']:
        return None
    path = Path(root) / 'work/control-center/catalog/ssh' / (hashlib.sha256(group['alias'].encode()).hexdigest() + '.jsonl')
    key = os.path.normcase(str(path))
    stamp = _stamp(path)
    cached = _CATALOGS.get(key)
    if stamp is None or path.is_symlink():
        return None
    if cached is None or cached[0] != stamp:
        from . import catalog_frames
        try:
            with path.open('rb') as stream:
                value = catalog_frames.read(stream)
            edges, resolved, conflicts = {}, set(), set()
            for row in value.get('conversations', []):
                child = row.get('thread_id')
                if not _uuid(child) or 'forked_from_id' not in row:
                    continue
                resolved.add(child)
                parent = row['forked_from_id']
                if not _uuid(parent) or parent == child:
                    continue
                if edges.setdefault(child, parent) != parent:
                    conflicts.add(child)
            for child in conflicts:
                edges.pop(child)
            cached = (stamp, value.get('scope'), edges, frozenset(resolved))
        except (OSError, ValueError, TypeError, AttributeError):
            cached = (stamp, None, {}, frozenset())
        if len(_CATALOGS) >= 64:
            _CATALOGS.clear()
        _CATALOGS[key] = cached
    if cached[1] is None or cached[1] != group['scope']:
        return None
    return cached[2], cached[3]


def refresh(root, state=None, thread_id=None, host_id='local'):
    """Publish verified native forks; serialize state and note preflight calls."""
    with _LOCK:
        if thread_id is not None and not _uuid(thread_id):
            raise ValueError('Invalid note fork thread id')
        alias = None if host_id == 'local' else ssh_alias(host_id)
        if host_id != 'local' and alias is None:
            raise ValueError('Invalid note fork host')
        try:
            return _refresh(root, state, thread_id, alias)
        except sqlite3.Error as error:
            raise RuntimeError('Could not read task fork metadata; existing notes are unchanged') from error


def _publish(target, mapping, existing):
    if not mapping and not target.exists():
        return None
    if len(mapping) > MAX_ENTRIES:
        mapping = dict(sorted(mapping.items())[:MAX_ENTRIES])
    document = {child: parent for child, parent in sorted(mapping.items())}
    if existing == document:
        return document
    from .store import atomic_json
    atomic_json(target, document)
    return document


def _refresh_ssh_task(root, state, thread_id, alias, target, existing):
    """Publish one SSH task's fork ancestry from the cached catalog."""
    from .remote_catalog import groups
    group = groups(state).get(alias)
    found = _ssh_parents(root, group) if group else None
    if found is None or thread_id not in found[1]:
        # The catalog has not inspected this task yet. Its first note read
        # stays unshared and unsaved; the notes panel reads it again shortly.
        return existing if isinstance(existing, dict) else None
    edges = found[0]
    mapping = dict(existing) if isinstance(existing, dict) else {}
    mapping.pop(ssh_key(alias, thread_id), None)
    visited = set()
    while thread_id in edges and thread_id not in visited and len(visited) < 64:
        visited.add(thread_id)
        mapping[ssh_key(alias, thread_id)] = edges[thread_id]
        thread_id = edges[thread_id]
    return _publish(target, mapping, existing)


def _refresh(root, state, thread_id, alias=None):
    root = Path(root)
    if state is None:
        from .store import Store
        state = Store(root).read()
    target = root / 'work/control-center/note-forks.json'
    mapping = {}
    known = False
    try:
        existing = json.loads(target.read_text(encoding='utf-8')) if target.exists() else None
    except (OSError, ValueError):
        existing = None
    if alias is not None and thread_id is not None:
        return _refresh_ssh_task(root, state, thread_id, alias, target, existing)
    if thread_id is not None and isinstance(existing, dict):
        mapping.update(existing)
        mapping.pop(thread_id, None)
    for home in homes(root, state):
        database = _database(home)
        if _stamp(database) is None:
            continue
        # A temporary database error must not publish a partial ancestry map.
        if thread_id:
            found, recognized = _task_parents(database, thread_id)
            known |= recognized
            mapping.update(found)
        else:
            mapping.update(_parents(database))
    if thread_id is not None and not known:
        raise RuntimeError('Task metadata is not indexed yet; retry loading notes')
    if thread_id is None:
        from .remote_catalog import groups
        for host, group in groups(state).items():
            found = _ssh_parents(root, group)
            if found is None:
                # An unavailable catalog keeps this host's published forks.
                prefix = ssh_key(host, '')
                if isinstance(existing, dict):
                    mapping.update({child: parent for child, parent in existing.items()
                                    if child.startswith(prefix) and _uuid(parent)})
                continue
            mapping.update({ssh_key(host, child): parent for child, parent in found[0].items()})
    return _publish(target, mapping, existing)
