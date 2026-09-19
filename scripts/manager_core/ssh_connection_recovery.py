"""One-shot recovery of explicitly selected saved SSH declarations before launch."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path

from .common import _assert_owned_path
from .ssh_shim import validate_binding
from .store import atomic_json

CONNECTIONS = 'codex-managed-remote-connections'
AUTO = 'remote-connection-auto-connect-by-host-id'
PENDING = '.manager-ssh-connection-recovery.json'


def _snapshot(state):
    # Runtime selection, drafts, and unrelated desktop writes do not invalidate
    # the request. A connection or auto-connect preference edit does.
    fields = ('hostId', 'displayName', 'source', 'alias', 'hostname', 'sshPort', 'identity')
    connections = [{key: item.get(key) for key in fields}
                   for item in state.get(CONNECTIONS, []) if isinstance(item, dict)]
    connections.sort(key=lambda item: str(item.get('hostId')))
    value = {CONNECTIONS: connections, AUTO: state.get(AUTO, {})}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _path(home, name):
    path = home / name
    _assert_owned_path(path, home)
    if path.exists() and (path.is_symlink() or path.stat().st_nlink != 1):
        raise ValueError('SSH recovery file is linked.')
    return path


def queue(home, source, profile_id, bindings, aliases):
    """Queue a reviewed repair without writing the running desktop's state."""
    home, source = Path(home).resolve(), Path(source).resolve()
    selected = set(aliases)
    valid = {validate_binding(b, profile_id)['alias'] for b in bindings if b.get('prepared') is True}
    if not selected or not selected <= valid:
        raise ValueError('SSH recovery requires saved prepared bindings.')
    current = json.loads(_path(home, '.codex-global-state.json').read_text(encoding='utf-8'))
    donor = json.loads((source / '.codex-global-state.json').read_text(encoding='utf-8'))
    present = {item.get('alias') for item in current.get(CONNECTIONS, []) if isinstance(item, dict)}
    if selected & present:
        raise ValueError('SSH recovery only applies to missing aliases.')
    fields = ('hostId', 'displayName', 'source', 'alias', 'hostname', 'sshPort', 'identity')
    additions = [{key: item.get(key) for key in fields} for item in donor.get(CONNECTIONS, [])
                 if isinstance(item, dict) and item.get('alias') in selected
                 and isinstance(item.get('hostId'), str) and item['hostId'].startswith('remote-ssh-')]
    if len(additions) != len(selected) or {item['alias'] for item in additions} != selected:
        raise ValueError('SSH recovery needs unambiguous original declarations.')
    path = _path(home, PENDING)
    if path.exists():
        raise ValueError('SSH connection recovery is already queued.')
    auto = donor.get(AUTO, {})
    atomic_json(path, dict(schema=1, profile_id=profile_id, baseline=_snapshot(current),
        connections=additions, auto={item['hostId']: auto[item['hostId']] for item in additions
            if type(auto.get(item['hostId'])) is bool}))
    return path


def apply_pending(home, current):
    """Called only by inactive preference preparation; completion follows save."""
    path = _path(home, PENDING)
    if not path.exists():
        return None
    pending = json.loads(path.read_text(encoding='utf-8'))
    if pending.get('schema') != 1 or pending.get('profile_id') != home.parent.name:
        raise ValueError('Invalid queued SSH connection recovery.')
    result = {'status': 'skipped_changed_preferences', 'aliases': [x['alias'] for x in pending['connections']]}
    if _snapshot(current) != pending['baseline']:
        return result
    backup = _path(home, '.manager-ssh-connection-recovery-before.json')
    if not backup.exists():
        atomic_json(backup, current)
    current.setdefault(CONNECTIONS, []).extend(deepcopy(pending['connections']))
    for host_id, enabled in pending['auto'].items():
        current.setdefault(AUTO, {}).setdefault(host_id, enabled)
    result['status'] = 'restored'
    return result


def finish(home, result):
    if result is None:
        return
    atomic_json(_path(home, '.manager-ssh-connection-recovery-result.json'), result)
    os.replace(_path(home, PENDING), _path(home, '.manager-ssh-connection-recovery-consumed.json'))
