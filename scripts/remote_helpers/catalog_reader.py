"""Read registered Linux conversation metadata without starting a Codex runtime."""
import hashlib
import json
import os
from pathlib import Path
from uuid import UUID

from catalog import list_source, sort_conversations
from catalog_legacy import discover, origins, origin_for


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
    legacy = discover(user_home, sources, environ=environ) if request.get('discover_legacy') is True else dict(sources=[], errors=[])
    all_sources = [*sources, *legacy['sources']]
    homes, roots = origins(all_sources)
    rows, errors = [], []
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
    return dict(conversations=selected, errors=errors, discovered_sources=legacy['sources'],
                discovery_errors=legacy['errors'], possibly_truncated=False)
