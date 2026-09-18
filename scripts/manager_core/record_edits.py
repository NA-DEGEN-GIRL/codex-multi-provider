"""Explicit title edits to registered local records; never resume an actor.

The runtime catalog remains a history reader. This manager adapter handles only
the user's thread/name/set request after account and maintenance admission. It
updates the canonical name column and native name index, without acquiring a
conversation writer, altering history, or loading another account's credentials.
"""
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3

from .source_catalog import registered, projection_id
from .store import Store, identifier


def normal_path(value):
    path = Path(value)
    value = str(path)
    if os.name == 'nt' and value.startswith('\\\\?\\'):
        if value.startswith('\\\\?\\UNC\\'):
            return Path('\\\\' + value[8:])
        if len(value) > 6 and value[5:7] == ':\\':
            return Path(value[4:])
    return path


def plain_file(path):
    info = path.lstat()
    if (not path.is_file() or path.is_symlink() or info.st_nlink != 1
            or getattr(info, 'st_file_attributes', 0) & 0x400 or normal_path(path.resolve(strict=True)) != normal_path(path)):
        raise ValueError('작업 기록의 경로가 변경되었습니다.')


def verify_rollout(home, rollout, thread_id):
    path = normal_path(rollout)
    if not path.is_absolute() or not any(path.is_relative_to(home / part) for part in ('sessions', 'archived_sessions')):
        raise ValueError('작업의 원본 저장소가 일치하지 않습니다.')
    plain_file(path)
    with path.open('rb') as stream:
        first = stream.readline(1024 * 1024 + 1)
    if len(first) > 1024 * 1024:
        raise ValueError('작업 식별 정보를 확인할 수 없습니다.')
    item = json.loads(first)
    if item.get('type') != 'session_meta' or item.get('payload', {}).get('id') != thread_id:
        raise ValueError('작업 식별 정보가 일치하지 않습니다.')
    return path


def rename_record(root, catalog_path, origin, projection, name):
    thread_id = identifier(origin['canonicalThreadId'])
    if not isinstance(name, str) or not (name := name.strip()) or len(name) > 512 or any(ord(c) < 32 for c in name):
        raise ValueError('작업 이름은 1~512자의 한 줄로 입력하세요.')
    store = Store(root)
    sources = registered(store.root, catalog_path, store.read()['sources'])
    source = sources.get(origin['sourceStoreId'])
    if (origin.get('hostId') != 'local' or source is None
            or projection_id(source['native_id'], thread_id) != projection):
        raise ValueError('작업의 원본 연결이 변경되었습니다. 목록을 다시 여세요.')
    home = Path(source['home'])
    candidates = sorted({*home.glob('state_*.sqlite'), *(home / 'sqlite').glob('state_*.sqlite')})
    matches = []
    for path in candidates:
        plain_file(path)
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
            columns = {r[1] for r in db.execute('PRAGMA table_info(threads)')}
            if not {'id', 'name', 'rollout_path'} <= columns:
                continue
            row = db.execute('SELECT rollout_path FROM threads WHERE id=?', (thread_id,)).fetchone()
            if row:
                verify_rollout(home, row[0], thread_id)
                matches.append((path, row[0]))
    if len(matches) != 1:
        raise ValueError('이 작업의 원본 이름 저장소를 하나로 확인하지 못했습니다.')
    path, rollout = matches[0]
    index = home / 'session_index.jsonl'
    if index.exists():
        plain_file(index)
    entry = dict(id=thread_id, thread_name=name, updated_at=datetime.now(timezone.utc).isoformat())
    line = (json.dumps(entry, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
    with closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=2)) as db:
        db.execute('BEGIN IMMEDIATE')
        # Recheck the enrolled source and selected record after taking the write lock.
        current = registered(store.root, catalog_path, store.read()['sources']).get(origin['sourceStoreId'])
        if current != source:
            raise ValueError('이름 변경 중 원본 연결이 바뀌었습니다.')
        plain_file(path)
        verify_rollout(home, rollout, thread_id)
        row = db.execute('SELECT rollout_path FROM threads WHERE id=?', (thread_id,)).fetchone()
        if not row or row[0] != rollout:
            raise ValueError('이름 변경 중 작업의 위치가 바뀌었습니다.')
        db.execute('UPDATE threads SET name=? WHERE id=?', (name, thread_id))
        # One append matches the native session index format. SQLite serializes
        # simultaneous manager renames; JSONL history is never opened for writing.
        if index.exists():
            plain_file(index)
        fd = os.open(index, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, 'O_BINARY', 0), 0o600)
        try:
            if os.write(fd, line) != len(line):
                raise OSError('Incomplete name index append')
            os.fsync(fd)
        finally:
            os.close(fd)
        db.commit()
    return name


class RecordEdits:
    def __init__(self, environment, origins):
        self.root = environment.get('CODEX_MANAGER_ROOT')
        self.catalog = environment.get('CODEX_MANAGER_SHARED_CATALOG')
        if environment.get('CODEX_MANAGER_RECORD_CATALOG') or environment.get('CODEX_MANAGER_SHARED_EXECUTION') == '1':
            self.catalog = None
        self.origins = origins

    def handle(self, message):
        if (not self.root or not self.catalog or message.get('method') != 'thread/name/set'
                or type(message.get('id')) not in (int, str)):
            return None
        params = message.get('params')
        if not isinstance(params, dict):
            return None
        try:
            projection = identifier(params.get('threadId'))
            origin = self.origins.lookup(projection)
            if origin is None:
                return None  # Native tasks retain the runtime's normal name handler.
            name = rename_record(self.root, self.catalog, origin, projection, params.get('name'))
            return [{'id': message['id'], 'result': {}},
                    {'method': 'thread/name/updated', 'params': {'threadId': projection, 'threadName': name}}]
        except (ValueError, OSError, sqlite3.Error, TypeError, KeyError):
            return [{'id': message['id'], 'error': {'code': -32600,
                'message': '작업 이름을 원본 기록에 저장하지 못했습니다. 원본 연결 또는 기록 파일 상태를 확인하세요.'}}]
