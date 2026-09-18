"""Monotonic evidence that a task has reached canonical project membership.

An unimported legacy assignment and an explicit native removal both read as
NULL. Keep them distinct without treating a desktop mode/cache reset as a move.
Only successful native writes or non-NULL native reads establish this evidence.
"""
import json
from pathlib import Path
from uuid import UUID

from .store import atomic_json
from .updates import UpdateError, _lock_file, _unlock_file

WRITER = '00000000-0000-4000-8000-000000000054'
MAX_IDS = 131072


def read_file(path):
    try:
        if path.stat().st_size > 8 * 1024 * 1024:
            return set()
        data = json.loads(path.read_text(encoding='utf-8'))
        values = data.get('thread_ids')
        if data.get('version') != 1 or not isinstance(values, list) or len(values) > MAX_IDS:
            return set()
        return {str(UUID(value)) for value in values if isinstance(value, str)}
    except (OSError, ValueError, TypeError, AttributeError):
        return set()


def read(signals):
    result = set()
    directory = Path(signals) / 'project-membership-proofs'
    for file in list(directory.glob('*.json'))[:256]:
        try:
            UUID(file.stem)
        except ValueError:
            continue
        result.update(read_file(file))
    return result


def remember(signals, thread_ids):
    values = {str(UUID(value)) for value in thread_ids}
    if not values:
        return
    directory = Path(signals) / 'project-membership-proofs'
    try:
        directory.mkdir(parents=True, exist_ok=True)
        lock = _lock_file(directory / 'bootstrap.lock')
    except (OSError, UpdateError):
        return  # A later native observation will retry; unknown NULL stays safe.
    try:
        path = directory / (WRITER + '.json')
        previous = read_file(path)
        merged = previous | values
        if merged != previous and len(merged) <= MAX_IDS:
            atomic_json(path, dict(version=1, thread_ids=sorted(merged)))
    except OSError:
        return  # Optional evidence must not prevent a profile from opening.
    finally:
        _unlock_file(lock)
