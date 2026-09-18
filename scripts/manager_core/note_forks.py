"""Publish the parent of every forked task so note groups can be inherited.

The note service is a separate process and cannot read the Codex state
databases, so the manager maintains one small mapping file:
``work/control-center/note-forks.json`` with ``child thread -> parent thread``.
A task without its own note document adopts its parent's note group, which is
how a forked conversation keeps editing the same memo until the user presses
"메모 fork".
"""
import json
import os
from pathlib import Path
import sqlite3
import urllib.parse

MAX_ENTRIES = 20000
_SEEN = {}


def _database(home):
    return Path(home) / 'state_5.sqlite'


def _stamp(path):
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _parents(database):
    """Read child -> parent spawn edges from one read-only state database."""
    uri = 'file:' + urllib.parse.quote(str(database).replace('\\', '/')) + '?mode=ro&immutable=1'
    found = {}
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=2)
    except sqlite3.Error:
        return found
    try:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if 'thread_spawn_edges' not in tables:
            return found
        for parent, child in connection.execute(
                'SELECT parent_thread_id, child_thread_id FROM thread_spawn_edges'):
            if not isinstance(parent, str) or not isinstance(child, str):
                continue
            if parent != child and len(parent) == 36 and len(child) == 36:
                found[child] = parent
    except sqlite3.Error:
        return found
    finally:
        connection.close()
    return found


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


def refresh(root, state=None):
    """Rewrite the mapping file only when a state database actually changed."""
    root = Path(root)
    target = root / 'work/control-center/note-forks.json'
    mapping = {}
    changed = False
    for home in homes(root, state):
        database = _database(home)
        stamp = _stamp(database)
        if stamp is None:
            continue
        key = os.path.normcase(str(database))
        cached = _SEEN.get(key)
        if cached is not None and cached[0] == stamp:
            mapping.update(cached[1])
            continue
        found = _parents(database)
        _SEEN[key] = (stamp, found)
        mapping.update(found)
        changed = True
    if not mapping:
        return None
    if len(mapping) > MAX_ENTRIES:
        mapping = dict(sorted(mapping.items())[:MAX_ENTRIES])
    document = {child: parent for child, parent in sorted(mapping.items())}
    try:
        existing = json.loads(target.read_text(encoding='utf-8')) if target.exists() else None
    except (OSError, ValueError):
        existing = None
    if existing == document:
        return document
    from .store import atomic_json
    atomic_json(target, document)
    return document
