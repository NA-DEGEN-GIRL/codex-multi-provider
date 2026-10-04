"""Pinned SSH preset publication. No credentials, config edits, or lifecycle actions."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

MAX_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = MAX_BYTES + 65536
AUTHORITY = 'manager-execution-authority.json'


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode('utf-8')


def _safe_path(path):
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError('symlink preset path')
    if path.exists() and hasattr(os, 'getuid') and path.stat().st_uid != os.getuid():
        raise ValueError('foreign preset path')


def _read(path, maximum=MAX_BYTES):
    _safe_path(path)
    if path.stat().st_size > maximum:
        raise ValueError('preset size')
    with path.open('rb') as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError('preset size')
    return json.loads(raw)


def _atomic(path, value):
    _safe_path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('wb', dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            stream.write(_bytes(value))
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


@contextmanager
def _lock(profile):
    import fcntl
    path = profile / 'execution-presets.lock'
    _safe_path(path)
    with path.open('a+b') as stream:
        os.chmod(path, 0o600)
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def _authority(profile, revision):
    from native_controller import _descriptor
    descriptor = _descriptor(profile, revision)
    definition = Path(descriptor['definition'])
    _safe_path(definition)
    if definition != profile / 'definitions' / revision:
        raise ValueError('preset definition path')
    authority = _read(definition / AUTHORITY)
    if (authority.get('schema_version') != 1 or authority.get('profile_id') != profile.name
            or not re.fullmatch(r'ssh:[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', authority.get('host_id', ''))
            or not isinstance(authority.get('roles'), dict) or len(authority['roles']) > 256):
        raise ValueError('preset authority')
    return descriptor, authority


def _selector(value):
    if value is None:
        return
    if (not isinstance(value, dict) or set(value) != {'id', 'revision'}
            or str(uuid.UUID(value['id'])) != value['id']
            or type(value['revision']) is not int or value['revision'] < 1):
        raise ValueError('preset selector')


def _manifest(authority, value):
    if (not isinstance(value, dict) or set(value) - {'presets', 'default_preset', 'task_bindings'}
            or not isinstance(value.get('presets'), list) or len(value['presets']) > 256
            or not isinstance(value.get('task_bindings'), dict)):
        raise ValueError('preset selection')
    seen = set()
    for preset in value['presets']:
        if not isinstance(preset, dict) or set(preset) != {'id', 'revision', 'profile_id', 'name', 'main', 'role_ids'}:
            raise ValueError('preset record')
        _selector({key: preset[key] for key in ('id', 'revision')})
        key = (preset['id'], preset['revision'])
        if (key in seen or preset['profile_id'] != authority['profile_id']
                or not isinstance(preset['name'], str) or not 1 <= len(preset['name']) <= 256
                or not isinstance(preset['main'], dict) or set(preset['main']) - {'model', 'effort'}
                or not isinstance(preset['role_ids'], list) or len(preset['role_ids']) > 32
                or len(set(preset['role_ids'])) != len(preset['role_ids'])
                or not set(preset['role_ids']).issubset(authority['roles'])):
            raise ValueError('unprepared preset role')
        seen.add(key)
    _selector(value.get('default_preset'))
    for thread_id, selection in value['task_bindings'].items():
        if not isinstance(thread_id, str) or not 1 <= len(thread_id) <= 256 or any(ord(c) < 32 for c in thread_id):
            raise ValueError('preset task')
        # An unresolved immutable selector must remain unresolved. Never fall
        # back to a different default when a saved role cannot be prepared.
        _selector(selection)
    manifest = dict(authority, **value)
    if len(_bytes(manifest)) > MAX_BYTES:
        raise ValueError('preset size')
    return manifest


def _paths(profile, revision):
    directory = profile / 'execution-presets'
    _safe_path(directory)
    return directory / (revision + '.json'), directory / (revision + '.selection.json')


def publish(profile, revision, request):
    """Publish only the registry for this exact immutable host definition."""
    profile = Path(profile)
    if len(_bytes(request)) > MAX_REQUEST_BYTES:
        raise ValueError('preset request size')
    with _lock(profile):
        descriptor, authority = _authority(profile, revision)
        if (not isinstance(request, dict) or set(request) != {'schema_version', 'profile_id', 'host_id',
                'host_identity', 'revision', 'source_revision', 'selection'}
                or request['schema_version'] != 1 or request['profile_id'] != profile.name
                or request['host_id'] != authority['host_id'] or request['revision'] != revision
                or request['host_identity'] != descriptor['host_identity']
                or type(request['source_revision']) is not int or request['source_revision'] < 0):
            raise ValueError('preset publication binding')
        manifest = _manifest(authority, request['selection'])
        path, selection_path = _paths(profile, revision)
        if selection_path.exists():
            previous = _read(selection_path, MAX_REQUEST_BYTES)
            if (previous['source_revision'] > request['source_revision']
                    or previous['source_revision'] == request['source_revision'] and previous != request):
                raise ValueError('stale preset publication')
        # The selection journal permits cold startup/retry to finish an
        # interrupted atomic registry write without losing the newer choice.
        _atomic(selection_path, request)
        _atomic(path, manifest)
        return dict(status='published', profile_id=profile.name, host_id=authority['host_id'],
                    revision=revision, source_revision=request['source_revision'],
                    registry_sha256=hashlib.sha256(_bytes(manifest)).hexdigest())


def runtime_path(profile, revision):
    """Recover the pinned mutable registry before starting a cold runtime."""
    profile = Path(profile)
    with _lock(profile):
        descriptor, authority = _authority(profile, revision)
        path, selection_path = _paths(profile, revision)
        if selection_path.exists():
            request = _read(selection_path, MAX_REQUEST_BYTES)
            if (request.get('profile_id') != profile.name or request.get('host_id') != authority['host_id']
                    or request.get('host_identity') != descriptor['host_identity'] or request.get('revision') != revision):
                raise ValueError('preset recovery binding')
            manifest = _manifest(authority, request['selection'])
        else:
            manifest = _manifest(authority, dict(presets=[], default_preset=None, task_bindings={}))
        if not path.exists() or _read(path) != manifest:
            _atomic(path, manifest)
        return path
