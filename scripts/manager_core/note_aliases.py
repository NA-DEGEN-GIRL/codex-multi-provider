"""Keep remote notes attached when an old catalog link becomes an editable ID.

Only metadata from the manager's existing SSH cache is used. No SSH connection,
record copy, or note-body rewrite is necessary.
"""
import hashlib
import json
from pathlib import Path
import threading
import uuid

from . import catalog_frames
from .store import atomic_json

_NAMESPACE = uuid.UUID('14a0f21b-b529-45fc-bd9e-b07637424fa3')
_LOCK = threading.RLock()


def document_path(root, task):
    encoded = json.dumps(task, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return Path(root) / 'work/control-center/notes' / (hashlib.sha256(encoded).hexdigest() + '.json')


def refresh(root, task):
    host = task.get('host_id', '')
    thread = str(uuid.UUID(task['thread_id']))
    prefix = next((p for p in ('ssh:', 'remote-ssh-discovered:') if host.startswith(p)), None)
    if prefix is None or len(host) > 256 or any(ord(c) < 32 for c in host):
        raise ValueError('Invalid remote note host')
    current = dict(host_id=host, thread_id=thread)
    with _LOCK:
        if document_path(root, current).exists():
            return
        directory = Path(root) / 'work/control-center'
        cache = directory / 'catalog/ssh' / (hashlib.sha256(host[len(prefix):].encode()).hexdigest() + '.jsonl')
        if not cache.exists():
            return
        with cache.open('rb') as stream:
            data = catalog_frames.read(stream)
        candidates = set()
        for entry in data.get('conversations', []):
            if entry.get('thread_id') != thread:
                continue
            source = entry.get('source_store_id')
            if not isinstance(source, str) or not source or len(source) > 256:
                continue
            projection = str(uuid.uuid5(_NAMESPACE, f'local\0{source}\0{thread}'))
            previous = dict(host_id=host, thread_id=projection)
            if document_path(root, previous).is_file():
                candidates.add(projection)
        if len(candidates) > 1:
            raise ValueError('Several old task copies have notes; existing notes were preserved')
        if not candidates:
            return
        path = directory / 'note-aliases.json'
        if path.exists() and (path.is_symlink() or path.stat().st_size > 4 * 1024 * 1024):
            raise ValueError('Invalid note alias file')
        aliases = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        if not isinstance(aliases, dict) or len(aliases) >= 16384:
            raise ValueError('Invalid note alias map')
        key = host + '\0' + thread
        target = candidates.pop()
        if key in aliases and aliases[key] != target:
            raise ValueError('Remote note identity changed; existing notes were preserved')
        aliases[key] = target
        atomic_json(path, aliases)
