"""Content-free per-thread activity for the task shortcut list.

The local runtime proxy publishes activity inside its runtime-state snapshot.
An SSH connection has no such snapshot, so each native SSH proxy process writes
its own small file here; the manager merges fresh files per profile. Only thread
ids, one of three state words and the last opened task's title (as the local
runtime-state already records) leave the pump -- never message content.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
from uuid import UUID

STATES = ('waiting_approval', 'waiting_input', 'working')
# A writer refreshes at least this often; a reader drops older files, so a
# crashed SSH proxy cannot leave a task looking busy.
REFRESH_SECONDS = 10.0
MAX_AGE_SECONDS = 30.0
LIMIT = 64


def activity_path(root, profile_id, alias, pid=None):
    directory = Path(root) / 'work/control-center/instances' / str(UUID(profile_id))
    return directory / f"ssh-activity-{alias}-{pid or os.getpid()}.json"


class ActivityFile:
    def __init__(self, path, *, generation, host_id=None, clock=time.monotonic, wall=time.time):
        self.path, self.generation, self.host_id = Path(path), generation, host_id
        self.clock, self.wall = clock, wall
        self.last = None
        self.written_at = None

    def publish(self, activity, opened=None):
        activity = {thread: state for thread, state in sorted(activity.items())[:LIMIT] if state in STATES}
        # The task this connection last opened (id, title, time) lets "add the
        # open task" work without a keyboard shortcut or the clipboard.
        opened = {key: opened[key] for key in ('thread_id', 'title', 'observed_at')
                  if isinstance(opened, dict) and isinstance(opened.get(key), str)} or None
        now = self.clock()
        if ((activity, opened) == self.last and self.written_at is not None
                and now - self.written_at < REFRESH_SECONDS):
            return
        value = {'schema': 1, 'generation': self.generation, 'observed_at': self.wall(),
                 'thread_activity': activity}
        if opened and self.host_id:
            value['opened_task'] = {**opened, 'host_id': self.host_id}
        temporary = self.path.with_name(self.path.name + '.tmp')
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(value), encoding='utf-8')
            os.replace(temporary, self.path)
        except OSError:
            # A reader holding the file open on Windows; retry on the next poll.
            return
        self.last, self.written_at = (activity, opened), now

    def close(self):
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


def merge(*sources):
    """Strongest state wins: an approval wait outranks input, input outranks work."""
    merged = {}
    for source in sources:
        if not isinstance(source, dict):
            continue
        for thread, state in source.items():
            if state not in STATES or not isinstance(thread, str):
                continue
            current = merged.get(thread)
            if current is None or STATES.index(state) < STATES.index(current):
                merged[thread] = state
    return merged


def read_ssh(directory, generation, *, wall=time.time):
    """Fresh SSH activity files of this launch generation, merged."""
    return read_ssh_state(directory, generation, wall=wall)[0]


def latest_opened(*candidates):
    """The most recently opened task among local and SSH observations."""
    valid = [c for c in candidates if isinstance(c, dict) and isinstance(c.get('thread_id'), str)
             and isinstance(c.get('observed_at'), str)]
    return max(valid, key=lambda c: c['observed_at']) if valid else None


def read_ssh_state(directory, generation, *, wall=time.time):
    """(merged activity, latest opened task) from fresh SSH files of this launch."""
    sources, opened = [], []
    try:
        paths = list(Path(directory).glob('ssh-activity-*.json'))
    except OSError:
        return {}, None
    for path in paths[:64]:
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        age = wall() - value.get('observed_at', 0) if isinstance(value.get('observed_at'), (int, float)) else None
        if age is None or age > MAX_AGE_SECONDS:
            if age is None or age > 86400:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            continue
        if value.get('generation') == generation:
            sources.append(value.get('thread_activity'))
            opened.append(value.get('opened_task'))
    return merge(*sources), latest_opened(*opened)
