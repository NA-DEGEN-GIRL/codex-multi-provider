"""Lossless ordinal repair for a stopped, explicitly selected local task.

Preserve every payload and a byte-for-byte backup. Never modify an open rollout:
Windows share denial is checked before any live file/index change.
"""
import ctypes
from ctypes import wintypes
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
from uuid import UUID, uuid4

TABLES = ('thread_items', 'thread_turns', 'thread_realtime_items', 'thread_history_projection_state')
ORDINAL = re.compile(rb'^(\s*\{\s*"timestamp"\s*:\s*"(?:[^"\\]|\\.)*"\s*,\s*"ordinal"\s*:\s*)(\d+)')


def canonical_path(value):
    """Compare resolved Windows paths equally with or without the long-path prefix."""
    path = Path(value).resolve()
    text = str(path)
    if os.name == 'nt' and text.startswith('\\\\?\\UNC\\'):
        return Path('\\\\' + text[8:])
    if os.name == 'nt' and text.startswith('\\\\?\\'):
        return Path(text[4:])
    return path


def prepare(source, destination, thread_id):
    """Write a candidate only; preserve all bytes except top-level ordinals."""
    thread_id = str(UUID(thread_id))
    changed = 0; first_change = None; first_ordinal = None; count = 0; previous = -1
    original_hash = hashlib.sha256(); repaired_hash = hashlib.sha256()
    with Path(source).open('rb') as reader, Path(destination).open('xb') as writer:
        for raw in reader:
            count += 1
            original_hash.update(raw)
            if not raw.endswith(b'\n'):
                raise ValueError('기록 마지막 줄이 아직 저장 중입니다.')
            row = json.loads(raw)
            if count == 1:
                meta = row.get('payload', {})
                if (row.get('type') != 'session_meta' or meta.get('id') != thread_id
                        or meta.get('history_mode') != 'paginated' or meta.get('history_base')):
                    raise ValueError('이 복구기는 독립된 원본 작업의 페이지 기록만 처리합니다.')
            ordinal = row.get('ordinal')
            if type(ordinal) is not int or ordinal < 0:
                raise ValueError('기록 번호를 확인하지 못했습니다.')
            desired = max(ordinal, previous + 1)
            if desired != ordinal:
                match = ORDINAL.match(raw)
                if not match or int(match[2]) != ordinal:
                    raise ValueError('원본 바이트를 유지하면서 기록 번호를 복구할 수 없습니다.')
                raw = raw[:match.start(2)] + str(desired).encode() + raw[match.end(2):]
                changed += 1
                if first_change is None:
                    first_change = count
                    first_ordinal = ordinal
            writer.write(raw); repaired_hash.update(raw); previous = desired
        writer.flush(); os.fsync(writer.fileno())
    return dict(thread_id=thread_id, lines=count, changed_ordinals=changed,
        first_changed_line=first_change, first_changed_ordinal=first_ordinal, last_ordinal=previous,
        original_sha256=original_hash.hexdigest(), repaired_sha256=repaired_hash.hexdigest())


@contextmanager
def exclusive_rollout(path):
    if os.name != 'nt':
        raise ValueError('이 복구 단계는 Windows의 파일 사용 여부 확인이 필요합니다.')
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    handle = kernel.CreateFileW(str(path), 0xC0000000, 0, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ValueError('이 작업을 사용하는 Codex가 열려 있습니다. 본앱과 관리 앱의 Codex 창을 정상 종료한 뒤 다시 여세요. 기록은 변경하지 않았습니다.')
    with os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY), 'r+b') as stream:
        yield stream


def _index_paths(root, home, thread_id, rollout, boundary):
    homes = {home}
    state_file = Path(root)/'work/control-center/state.json'
    if state_file.exists():
        for source in json.loads(state_file.read_text(encoding='utf-8-sig')).get('sources', []):
            if source.get('host_id') == 'local': homes.add(canonical_path(source['home']))
    paths = set()
    for folder in homes:
        state = folder/'state_5.sqlite'
        if not state.exists(): continue
        with closing(sqlite3.connect(state.as_uri()+'?mode=ro', uri=True)) as db:
            # Positional descendants need a separate coordinated migration. Do
            # not silently reinterpret their frozen history boundaries.
            for (record,) in db.execute('SELECT rollout_path FROM threads'):
                try:
                    with Path(record).open(encoding='utf8') as f: meta=json.loads(f.readline()).get('payload', {})
                except (OSError, ValueError): continue
                if thread_id in str(meta.get('history_base')):
                    raise ValueError('이 기록을 참조하는 분기 작업이 있어 개별 복구가 필요합니다.')
                if (meta.get('forked_from_id') == thread_id
                        and isinstance(meta.get('forked_from_ordinal_exclusive'), int)
                        and meta['forked_from_ordinal_exclusive'] > boundary):
                    raise ValueError('변경할 기록 번호를 참조하는 분기 작업이 있어 개별 복구가 필요합니다.')
            row = db.execute('SELECT rollout_path FROM threads WHERE id=?', (thread_id,)).fetchone()
            if row and canonical_path(row[0]) == rollout and (folder/'thread_history_1.sqlite').exists():
                paths.add((folder/'thread_history_1.sqlite').resolve())
    return sorted(paths)


def repair(root, home, thread_id):
    home = canonical_path(home); thread_id = str(UUID(thread_id))
    with closing(sqlite3.connect((home/'state_5.sqlite').as_uri()+'?mode=ro', uri=True)) as db:
        row = db.execute('SELECT rollout_path FROM threads WHERE id=?', (thread_id,)).fetchone()
    if not row: raise ValueError('복구할 작업을 찾지 못했습니다.')
    rollout = canonical_path(row[0])
    if not any(rollout.is_relative_to(home/sub) for sub in ('sessions','archived_sessions')):
        raise ValueError('원본 작업 저장소 밖의 기록은 수정하지 않습니다.')
    output = Path(root)/'work/history-recovery'/('repair-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')+'-'+uuid4().hex[:6])
    output.mkdir(parents=True)
    # Acquire before taking the backup/candidate so the final completed turn is
    # included, even when the user closes the app hours after requesting repair.
    with exclusive_rollout(rollout) as stream:
        backup = output/'original.jsonl'; candidate = output/'repaired.jsonl'
        with backup.open('xb') as file:
            shutil.copyfileobj(stream, file)
            file.flush(); os.fsync(file.fileno())
        result = prepare(backup, candidate, thread_id)
        if not result['changed_ordinals']: return {**result, 'state':'unchanged'}
        indexes = _index_paths(root, home, thread_id, rollout, result['first_changed_ordinal'])
        journal = output/'repair.json'
        journal.write_text(json.dumps({**result,'rollout':str(rollout),'state':'prepared'},indent=2))
        with ExitStack() as stack:
            databases=[]
            for number, path in enumerate(indexes):
                db=stack.enter_context(closing(sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,timeout=5)))
                db.execute('BEGIN IMMEDIATE')
                # Back up only this task's derived rows, never credentials or
                # unrelated tasks. These rows can also be rebuilt from JSONL.
                saved=stack.enter_context(closing(sqlite3.connect(output/f'index-{number}.sqlite')))
                for table in TABLES:
                    definition=db.execute('SELECT sql FROM sqlite_master WHERE type=? AND name=?',('table',table)).fetchone()
                    if not definition: continue
                    saved.execute(definition[0])
                    rows=db.execute(f'SELECT * FROM {table} WHERE thread_id=?',(thread_id,))
                    placeholders=','.join('?' for _ in rows.description)
                    saved.executemany(f'INSERT INTO {table} VALUES ({placeholders})',rows)
                    db.execute(f'DELETE FROM {table} WHERE thread_id=?',(thread_id,))
                saved.commit();databases.append(db)
            try:
                stream.seek(0)
                with candidate.open('rb') as file: shutil.copyfileobj(file,stream)
                stream.truncate();stream.flush();os.fsync(stream.fileno())
                for db in databases: db.commit()
            except BaseException:
                for db in databases: db.rollback()
                stream.seek(0)
                with backup.open('rb') as file: shutil.copyfileobj(file,stream)
                stream.truncate();stream.flush();os.fsync(stream.fileno())
                raise
        result.update(state='repaired',backup=str(backup),indexes_rebuilt_on_open=len(indexes))
        journal.write_text(json.dumps({**result,'rollout':str(rollout)},indent=2))
    return result


def apply_pending(root):
    request = Path(root)/'work/history-recovery/pending.json'
    if not request.exists(): return []
    value=json.loads(request.read_text(encoding='utf8')); home=(Path.home()/'.codex').resolve()
    results=[]
    for task in value['thread_ids']:
        results.append(repair(root,home,str(UUID(task))))
    request.rename(request.with_name('completed-'+uuid4().hex+'.json'))
    return results
