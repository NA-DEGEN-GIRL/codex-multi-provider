"""One local history store, with a cold, backed-up import of older profile stores.

Authentication/configuration stay in each profile. Original metadata wins for
existing tasks. No running desktop is closed, and no source file is removed.
"""
from contextlib import ExitStack, closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
from uuid import UUID, uuid4

from .history_recovery import canonical_path, exclusive_rollout
from .store import Store, atomic_json


def marker_path(root):
    return Path(root) / 'work/control-center/canonical-storage.json'


def ready(root, home=None):
    home = canonical_path(home or Path.home() / '.codex')
    path = marker_path(root)
    if not path.exists():
        return False
    value = json.loads(path.read_text(encoding='utf8'))
    return value.get('version') == 1 and value.get('state') == 'complete' and canonical_path(value['home']) == home


def _connect(home):
    db = sqlite3.connect((home / 'state_5.sqlite').as_uri() + '?mode=ro', uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute('BEGIN')
    return db


def _tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _rows(db, table):
    return [dict(r) for r in db.execute('SELECT * FROM ' + table)] if table in _tables(db) else []


def _insert(db, table, row):
    columns = {r[1] for r in db.execute('PRAGMA table_info(' + table + ')')}
    row = {k: v for k, v in row.items() if k in columns}
    db.execute('INSERT INTO ' + table + ' (' + ','.join(row) + ') VALUES (' +
               ','.join('?' for _ in row) + ')', tuple(row.values()))


def plan(root, home=None):
    """Read metadata only; hash comparisons are deferred to the closed import."""
    home = canonical_path(home or Path.home() / '.codex')
    state = Store(root).read()
    with closing(_connect(home)) as db:
        original = {r['id']: r for r in _rows(db, 'threads')}
    entries, missing, same = [], 0, 0
    for source in state['sources']:
        if source.get('host_id') != 'local' or source.get('history_migrated_to'):
            continue
        folder = canonical_path(source['home'])
        if folder == home or not (folder / 'state_5.sqlite').exists():
            continue
        with closing(_connect(folder)) as db:
            records = _rows(db, 'threads')
        for record in records:
            tid = str(UUID(record['id']))
            path = canonical_path(record['rollout_path'])
            if not path.is_file():
                missing += 1
                continue
            roots = [canonical_path(folder / part) for part in ('sessions', 'archived_sessions')]
            if not any(path.is_relative_to(part) for part in roots):
                raise ValueError('등록한 저장소 밖의 기록 경로입니다: ' + tid)
            current = original.get(tid)
            target = canonical_path(current['rollout_path']) if current else None
            if target and target.exists() and os.path.samefile(path, target):
                same += 1
                continue
            part = 'archived_sessions' if (current or record).get('archived') else 'sessions'
            date = re.match(r'rollout-(\d{4})-(\d{2})-(\d{2})T', path.name)
            relative = Path(*date.groups(), path.name) if date and part == 'sessions' else Path(path.name)
            destination = target if target and target.exists() else home / part / relative
            if not any(destination.is_relative_to(home / sub) for sub in ('sessions', 'archived_sessions')):
                raise ValueError('본앱 저장소 밖의 기록 대상입니다: ' + tid)
            entries.append(dict(thread_id=tid, source_home=str(folder), source=str(path),
                                destination=str(destination), existing=current is not None,
                                record=record))
    return dict(version=1, home=str(home), entries=entries, same_files=same,
                missing_source_files=missing,
                imported_tasks=len({e['thread_id'] for e in entries}),
                registered_sources=[s['id'] for s in state['sources'] if s.get('host_id') == 'local'])


def require_closed(root):
    from .instances import process_identity
    from start_synced_original import original_processes, find_app
    installed = canonical_path(find_app()['executable'])
    sync_root = canonical_path(Path(root) / 'artifacts/original-sync-desktop')
    for process in original_processes():
        path = process.get('ExecutablePath')
        if path and (canonical_path(path) == installed or canonical_path(path).is_relative_to(sync_root)):
            raise ValueError('기존 기록 통합을 처음 적용할 때는 본앱과 관리 앱의 Codex 창을 한 번 정상 종료해 주세요. 이후에는 모든 프로필이 같은 저장소를 사용합니다.')
    for profile in Store(root).read()['profiles']:
        identity = process_identity(profile.get('process_id'))
        if identity and identity.get('process_created') == profile.get('process_created'):
            raise ValueError('기존 기록 통합 대기: ' + profile['alias'] + '의 Codex 창을 정상 종료한 뒤 다시 여세요. 작업을 강제 중단하지 않았습니다.')


def _same_bytes(a, b):
    if a.stat().st_size != b.stat().st_size:
        return False
    with a.open('rb') as left, b.open('rb') as right:
        return hashlib.file_digest(left, 'sha256').digest() == hashlib.file_digest(right, 'sha256').digest()


def _validate(stream, tid):
    stream.seek(0)
    line = stream.readline(4 * 1024 * 1024)
    value = json.loads(line)
    if value.get('type') != 'session_meta' or value.get('payload', {}).get('id') != tid:
        raise ValueError('원본 기록의 작업 ID가 일치하지 않습니다: ' + tid)
    stream.seek(-1, os.SEEK_END)
    if stream.read(1) != b'\n':
        raise ValueError('아직 저장 중인 기록입니다: ' + tid)
    stream.seek(0)


def _project_map(source, target, needed):
    result = {}
    known = {r['id']: r for r in _rows(target, 'projects')}
    roots = lambda db, pid: tuple(canonical_path(r[0]) for r in db.execute(
        'SELECT path FROM project_roots WHERE project_id=? ORDER BY position', (pid,)))
    for project in _rows(source, 'projects'):
        pid = project['id']
        if pid not in needed:
            continue
        match = next((key for key in known if roots(source, pid) and roots(source, pid) == roots(target, key)), None)
        if match:
            result[pid] = match
        elif pid in known:
            raise ValueError('서로 다른 프로젝트에 같은 ID가 있어 기록을 보존했습니다.')
        else:
            _insert(target, 'projects', project)
            for row in source.execute('SELECT * FROM project_roots WHERE project_id=?', (pid,)):
                _insert(target, 'project_roots', dict(row))
            known[pid] = project
            result[pid] = pid
    return result


def migrate(root, home=None):
    """Run once on normal startup, before any profile desktop is spawned."""
    root = Path(root).resolve()
    home = canonical_path(home or Path.home() / '.codex')
    store = Store(root)
    # Serialize startup from the original launcher and the manager. SQLite and
    # the manager state have independent locks; never hold a GUI thread here.
    from .authority import _authority_guard
    marker_path(root).parent.mkdir(parents=True, exist_ok=True)
    with _authority_guard(marker_path(root).with_suffix('.guard')):
        if ready(root, home):
            return json.loads(marker_path(root).read_text(encoding='utf8'))
        require_closed(root)
        inventory = plan(root, home)
        output = root / 'work/history-consolidation' / (datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:6])
        output.mkdir(parents=True)
        atomic_json(output / 'plan.json', inventory)
        entries = inventory['entries']
        # Keep source databases and every source JSONL intact. Target changes
        # are additive and transactionally indexed; a crash can be rerun safely.
        with ExitStack() as stack:
            snapshots = {}
            for folder in {Path(e['source_home']) for e in entries} | {home}:
                db = stack.enter_context(closing(_connect(folder)))
                snapshot = output / ('state-' + hashlib.sha256(str(folder).encode()).hexdigest()[:16] + '.sqlite')
                with closing(sqlite3.connect(snapshot)) as backup:
                    db.backup(backup)
                snapshots[folder] = db
            staged = {}
            for entry in entries:
                src = Path(entry['source']); dst = Path(entry['destination']); tid = entry['thread_id']
                with exclusive_rollout(src) as stream:
                    _validate(stream, tid)
                    candidate = output / (tid + '.jsonl')
                    if candidate.exists():
                        with candidate.open('rb') as previous:
                            equal = hashlib.file_digest(stream, 'sha256').digest() == hashlib.file_digest(previous, 'sha256').digest()
                        if not equal:
                            raise ValueError('같은 작업 ID에 서로 다른 기록이 있어 덮어쓰지 않았습니다: ' + tid)
                    else:
                        with candidate.open('xb') as writer:
                            shutil.copyfileobj(stream, writer)
                            writer.flush(); os.fsync(writer.fileno())
                if dst.exists() and not _same_bytes(candidate, dst):
                    raise ValueError('본앱과 프로필의 기록이 달라 원본을 보존했습니다: ' + tid)
                staged[tid] = (candidate, dst)
            # Release read transactions before taking the target write lock.
            for db in snapshots.values():
                db.rollback()
            target = stack.enter_context(closing(sqlite3.connect(home / 'state_5.sqlite', timeout=10)))
            target.row_factory = sqlite3.Row
            target.execute('PRAGMA foreign_keys=ON')
            target.execute('BEGIN IMMEDIATE')
            copied = 0
            try:
                known = {r['id'] for r in _rows(target, 'threads')}
                sections = {r['id'] for r in _rows(target, 'thread_sections')}
                imported = set()
                for folder, source in snapshots.items():
                    owned = [e for e in entries if Path(e['source_home']) == folder]
                    new = [e for e in owned if e['thread_id'] not in known]
                    projects = _project_map(source, target, {e['record'].get('project_id') for e in new})
                    for entry in owned:
                        tid = entry['thread_id']; candidate, dst = staged[tid]
                        if not dst.exists():
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            temporary = dst.with_name(dst.name + '.' + uuid4().hex + '.tmp')
                            with candidate.open('rb') as reader, temporary.open('xb') as writer:
                                shutil.copyfileobj(reader, writer)
                                writer.flush(); os.fsync(writer.fileno())
                            # The complete file appears atomically, without
                            # replacing anything created by another writer.
                            os.link(temporary, dst)
                            temporary.unlink()
                            copied += 1
                        if tid in known:
                            target.execute('UPDATE threads SET rollout_path=? WHERE id=?', (str(dst), tid))
                            continue
                        row = dict(entry['record'], rollout_path=str(dst))
                        row['project_id'] = projects.get(row.get('project_id'))
                        sid = row.get('thread_section_id')
                        if sid and sid not in sections:
                            section = next((s for s in _rows(source, 'thread_sections') if s['id'] == sid), None)
                            if section is None:
                                raise ValueError('작업의 섹션 정보를 찾지 못했습니다: ' + tid)
                            _insert(target, 'thread_sections', section); sections.add(sid)
                        _insert(target, 'threads', row)
                        known.add(tid); imported.add(tid)
                        for table in ('thread_dynamic_tools', 'thread_artifacts'):
                            if table in _tables(source) and table in _tables(target):
                                for item in source.execute('SELECT * FROM ' + table + ' WHERE thread_id=?', (tid,)):
                                    _insert(target, table, dict(item))
                    for edge in _rows(source, 'thread_spawn_edges'):
                        if edge['child_thread_id'] in imported and not target.execute(
                                'SELECT 1 FROM thread_spawn_edges WHERE child_thread_id=?', (edge['child_thread_id'],)).fetchone():
                            _insert(target, 'thread_spawn_edges', edge)
                target.commit()
            except BaseException:
                target.rollback()
                raise
        from .history_recovery import apply_pending
        recovery = apply_pending(root)
        def mark(data):
            canonical = next((s for s in data['sources'] if s.get('host_id') == 'local' and canonical_path(s['home']) == home), None)
            if canonical is None:
                Store._source(data, home, 'original:local', '공통 기록')
                canonical = next(s for s in data['sources'] if s['id'] == 'original:local')
            for source in data['sources']:
                if source.get('host_id') == 'local' and source is not canonical:
                    source['history_migrated_to'] = str(home)
            for link in data['shortcuts']:
                if link.get('host_id', 'local') == 'local':
                    link['source_store_id'] = canonical['id']
        store.mutate(mark)
        result = dict(version=1, state='complete', home=str(home), backup=str(output),
                      copied_files=copied, imported_tasks=inventory['imported_tasks'],
                      same_files=inventory['same_files'], missing_source_files=inventory['missing_source_files'],
                      history_recovery=recovery)
        atomic_json(marker_path(root), result)
        atomic_json(output / 'result.json', result)
        return result
