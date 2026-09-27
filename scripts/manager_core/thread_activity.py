"""Content-free per-thread activity for the task shortcut list.

The local runtime proxy publishes activity inside its runtime-state snapshot.
An SSH connection has no such snapshot, so each native SSH proxy process writes
its own small file here; the manager merges fresh files per profile. Only thread
ids and one of three state words leave the pump -- never titles or content.
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
    def __init__(self, path, *, generation, clock=time.monotonic, wall=time.time):
        self.path, self.generation = Path(path), generation
        self.clock, self.wall = clock, wall
        self.last = None
        self.written_at = None

    def publish(self, activity):
        activity = {thread: state for thread, state in sorted(activity.items())[:LIMIT] if state in STATES}
        now = self.clock()
        if activity == self.last and self.written_at is not None and now - self.written_at < REFRESH_SECONDS:
            return
        value = {'schema': 1, 'generation': self.generation, 'observed_at': self.wall(),
                 'thread_activity': activity}
        temporary = self.path.with_name(self.path.name + '.tmp')
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(value), encoding='utf-8')
            os.replace(temporary, self.path)
        except OSError:
            # A reader holding the file open on Windows; retry on the next poll.
            return
        self.last, self.written_at = activity, now

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
    sources = []
    try:
        paths = list(Path(directory).glob('ssh-activity-*.json'))
    except OSError:
        return {}
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
    return merge(*sources)
