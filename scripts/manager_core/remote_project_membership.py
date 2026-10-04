"""Apply an explicitly requested remote task move at the next safe profile launch.

Never edit the state file while Electron owns it. Working directories, history,
Git state, and native project databases are not part of this repair.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
from uuid import UUID

from .common import _assert_owned_path
from .store import atomic_json

PENDING = '.manager-remote-project-membership.json'
ASSIGNMENTS = 'thread-project-assignments'
PROJECTLESS = 'projectless-thread-ids'
HOSTS = 'thread-project-membership-host-ids'
MAX_THREADS = 4096


def _path(home, name):
    path = Path(home) / name
    _assert_owned_path(path, Path(home))
    if path.exists() and (path.is_symlink() or path.stat().st_nlink != 1):
        raise ValueError('Remote membership repair path is linked.')
    return path


def _membership(state, thread_id):
    return dict(assignment=state.get(ASSIGNMENTS, {}).get(thread_id),
                projectless=thread_id in state.get(PROJECTLESS, []),
                host_id=state.get(HOSTS, {}).get(thread_id))


def _destination(state, project_id, host_id):
    return any(p.get('id') == project_id and p.get('hostId') == host_id
               for p in state.get('remote-projects', []) if isinstance(p, dict))


def queue(home, project_id, host_id, thread_ids):
    home = Path(home).resolve(strict=True)
    project_id = str(UUID(project_id))
    if not isinstance(host_id, str) or not host_id.startswith('remote-ssh-') or len(host_id) > 256:
        raise ValueError('A saved SSH host is required.')
    ids = sorted({str(UUID(value)) for value in thread_ids})
    if not 0 < len(ids) <= MAX_THREADS:
        raise ValueError('Invalid remote membership repair size.')
    current = json.loads(_path(home, '.codex-global-state.json').read_text(encoding='utf-8'))
    if not _destination(current, project_id, host_id):
        raise ValueError('The destination remote project is not registered in this profile.')
    path = _path(home, PENDING)
    if path.exists():
        raise ValueError('Remote project membership repair is already queued.')
    value = dict(schema=1, profile_id=home.parent.name, project_id=project_id, host_id=host_id,
                 memberships={tid: _membership(current, tid) for tid in ids})
    atomic_json(_path(home, '.manager-remote-project-membership-before.json'), value['memberships'])
    atomic_json(path, value)
    return path


def apply_pending(home, current, baseline, *, signals=None):
    path = _path(home, PENDING)
    if not path.exists():
        return None
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('Remote membership repair exceeds bounds.')
    request = json.loads(path.read_text(encoding='utf-8'))
    if request.get('schema') != 1 or request.get('profile_id') != Path(home).parent.name:
        raise ValueError('Invalid remote membership repair profile.')
    project_id = str(UUID(request['project_id']))
    host_id, members = request['host_id'], request['memberships']
    if (not isinstance(host_id, str) or not host_id.startswith('remote-ssh-') or len(host_id) > 256
            or not isinstance(members, dict) or not 0 < len(members) <= MAX_THREADS):
        raise ValueError('Invalid remote membership repair.')
    for tid, before in members.items():
        if str(UUID(tid)) != tid or not isinstance(before, dict) or set(before) != {'assignment', 'projectless', 'host_id'}:
            raise ValueError('Invalid remote membership baseline.')
    backup = _path(home, '.manager-remote-project-membership-before.json')
    if not backup.exists():
        atomic_json(backup, members)
    result = dict(status='applied', assigned=0, skipped_changed=0, skipped_missing_project=False)
    registered = _destination(current, project_id, host_id)
    if signals is not None:
        from .shared_workspaces import records
        latest = records(Path(signals) / 'workspaces', local=False).get(project_id)
        if latest is not None:
            registered = latest[2] is not None and latest[2].get('hostId') == host_id
    if not registered:
        result.update(status='skipped_missing_project', skipped_missing_project=True)
    assignment = dict(projectKind='remote', projectId=project_id, hostId=host_id)
    desired = dict(assignment=assignment, projectless=False, host_id=host_id)
    selected = ([tid for tid, before in members.items() if _membership(baseline, tid) in (before, desired)]
                if registered else [])
    result['skipped_changed'] = len(members) - len(selected) if registered else 0
    # Canonical preference merging may overwrite projectless membership before
    # this repair runs. A later user edit still wins for every skipped task.
    for tid in members.keys() - set(selected):
        actual = _membership(baseline, tid)
        for key, value in ((ASSIGNMENTS, actual['assignment']), (HOSTS, actual['host_id'])):
            if value is None:
                current.get(key, {}).pop(tid, None)
            else:
                current.setdefault(key, {})[tid] = deepcopy(value)
        projectless = list(current.get(PROJECTLESS, []))
        if actual['projectless'] and tid not in projectless:
            projectless.append(tid)
        elif not actual['projectless']:
            projectless = [value for value in projectless if value != tid]
        current[PROJECTLESS] = projectless
    if selected:
        for tid in selected:
            current.setdefault(ASSIGNMENTS, {})[tid] = deepcopy(assignment)
            current.setdefault(HOSTS, {})[tid] = host_id
        selected_set = set(selected)
        current[PROJECTLESS] = [tid for tid in current.get(PROJECTLESS, []) if tid not in selected_set]
        result['assigned'] = len(selected)
    return result


def finish(home, result):
    if result is not None:
        atomic_json(_path(home, '.manager-remote-project-membership-result.json'), result)
        os.replace(_path(home, PENDING), _path(home, '.manager-remote-project-membership-consumed.json'))
