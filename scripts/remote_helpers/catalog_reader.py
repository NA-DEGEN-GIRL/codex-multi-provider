"""Read registered Linux conversation metadata without starting a Codex runtime."""
import hashlib
import json
import os
from pathlib import Path
import stat
from uuid import UUID

from catalog import list_source, sort_conversations
from catalog_legacy import discover, origins, origin_for

MAX_META_BYTES = 4 * 1024 * 1024
# Each refresh reads at most this many rollout headers, newest tasks first.
# Headers never change, so the manager reports the threads it already knows.
MAX_FORK_READS = 256
MAX_FORK_KNOWN = 8192


def _subagent(source):
    if isinstance(source, str):
        try:
            source = json.loads(source)
        except ValueError:
            return source in ('subagent', 'sub_agent')
    return isinstance(source, dict) and any(key in source for key in ('subagent', 'sub_agent'))


def fork_parent(thread_id, rollout):
    """Return (resolved, parent) from the first session_meta line only.

    Conversation lines are never read. A header that is still being written is
    unresolved and read again later; any other complete header is final.
    """
    try:
        descriptor = os.open(rollout, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(descriptor, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return True, None
            raw = stream.readline(MAX_META_BYTES + 1)
    except OSError:
        return False, None
    if len(raw) > MAX_META_BYTES or not raw.endswith(b'\n'):
        return False, None
    try:
        record = json.loads(raw)
        metadata = record.get('payload')
        if (record.get('type') != 'session_meta' or not isinstance(metadata, dict)
                or metadata.get('id') != thread_id or _subagent(metadata.get('source'))):
            return True, None
        parent = metadata.get('forked_from_id')
        return True, parent if isinstance(parent, str) and parent != thread_id and str(UUID(parent)) == parent else None
    except (ValueError, AttributeError, RecursionError):
        return True, None


def host_identity():
    machine = Path('/etc/machine-id').read_text().strip()
    return hashlib.sha256((machine + '\\0' + str(os.getuid()) + '\\0' + str(Path.home())).encode()).hexdigest()


def read(request, *, user_home=None, identity=None, environ=None):
    user_home = Path(user_home) if user_home is not None else Path.home()
    if request['host_identity'] != (identity if identity is not None else host_identity()):
        raise ValueError('SSH host identity changed')
    sources = request['sources']
    if not isinstance(sources, list) or not 1 <= len(sources) <= 256:
        raise ValueError('Invalid source inventory')
    expected = {}
    for source in sources:
        sid = source['id']
        profile_id = str(UUID(sid[8:])) if isinstance(sid, str) and sid.startswith('manager:') else None
        home = user_home / '.local/share/codex-control-center/profiles' / str(profile_id) / 'codex'
        if sid != 'manager:' + str(profile_id) or source['home'] != str(home) or sid in expected:
            raise ValueError('Source is outside the registered managed profile')
        expected[sid] = home
    known = request.get('fork_known', [])
    if (not isinstance(known, list) or len(known) > MAX_FORK_KNOWN
            or not all(isinstance(item, str) for item in known)):
        raise ValueError('Invalid fork inventory')
    known = set(known)
    legacy = discover(user_home, sources, environ=environ) if request.get('discover_legacy') is True else dict(sources=[], errors=[])
    all_sources = [*sources, *legacy['sources']]
    homes, roots = origins(all_sources)
    rows, errors, rollouts = [], [], {}
    for source in all_sources:
        sid, home = source['id'], homes[source['id']]
        try:
            if not home.exists():
                raise ValueError('Source is unavailable')
            if home.resolve(strict=True) != home:
                raise ValueError('Source path changed')
            if sid in expected:
                marker = home / 'managed-source.json'
                if marker.is_symlink() or marker.stat().st_size > 4096 or marker.stat().st_nlink != 1:
                    raise ValueError('Source identity changed')
                if json.loads(marker.read_text(encoding='utf-8')) != {'host_id': 'local', 'store_id': sid}:
                    raise ValueError('Source identity changed')
            found = list_source({**source, 'host_id': 'local', 'alias': sid}, None, strict=True)
            for row in found:
                # Canonical resumes can add a foreign store's row to this DB.
                # Recover its registered origin from its actual rollout path.
                origin = origin_for(row, sid, homes, roots)
                if origin is None:
                    continue
                rows.append(dict(thread_id=row['thread_id'], source_store_id=origin,
                    title=str(row.get('title') or row['thread_id'])[:512],
                    cwd=str(row['cwd'])[:4096] if row.get('cwd') is not None else None,
                    updated_at=row.get('updated_at'), archived=bool(row.get('archived'))))
                if row.get('rollout_path'):
                    rollouts.setdefault((origin, row['thread_id']), row['rollout_path'])
        except (OSError, ValueError, TypeError, AttributeError):
            errors.append(sid)
        except Exception as error:
            # SQLite read/locking errors are unavailable metadata, not deletion.
            import sqlite3
            if not isinstance(error, sqlite3.Error):
                raise
            errors.append(sid)
    unique = {}
    for row in sort_conversations(rows):
        unique.setdefault((row['source_store_id'], row['thread_id']), row)
    selected = sort_conversations(unique.values())
    # Shared task notes follow native conversation forks. Report only the
    # parent id; a row without the field has not been resolved yet.
    budget = MAX_FORK_READS
    for row in selected:
        rollout = rollouts.get((row['source_store_id'], row['thread_id']))
        if rollout is None or row['thread_id'] in known:
            continue
        if budget <= 0:
            break
        budget -= 1
        resolved, parent = fork_parent(row['thread_id'], rollout)
        if resolved:
            row['forked_from_id'] = parent
    return dict(conversations=selected, errors=errors, discovered_sources=legacy['sources'],
                discovery_errors=legacy['errors'], possibly_truncated=False)
