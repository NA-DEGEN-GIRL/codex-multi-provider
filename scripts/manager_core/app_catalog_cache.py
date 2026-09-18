"""Invalidate native sidebar metadata when a managed HOME changes record identity mode.

Called only on the cold-launch path, after confirming the profile process exited.
The native app owns this cache and repopulates it from thread/list. Conversation
stores, accounts, projects, remote caches, and the original app are not modified.
"""
import hashlib
import json
from contextlib import closing
from pathlib import Path
import sqlite3
from uuid import uuid4

from .common import _assert_owned_path
from .store import atomic_json

_MARKER = '.manager-sidebar-cache.json'
_TABLES = ('local_thread_catalog', 'local_thread_catalog_sync_state',
           'local_thread_catalog_scan_checkpoints', 'local_thread_catalog_scan_entries')


def prepare(home, environment):
    home = Path(home).resolve(strict=True)
    if home == (Path.home() / '.codex').resolve():
        raise ValueError('원본 앱의 목록 캐시는 수정하지 않습니다.')
    mode = 'native'
    catalog_path = environment.get('CODEX_MANAGER_SHARED_CATALOG') or environment.get('CODEX_MANAGER_RECORD_CATALOG')
    identities = []
    if environment.get('CODEX_RECORD_HOME'):
        mode = 'canonical-storage-v1'
        identities = [('local', str(Path(environment['CODEX_RECORD_HOME']).resolve()))]
    if catalog_path:
        catalog = json.loads(Path(catalog_path).read_text(encoding='utf-8'))
        version = catalog.get('version')
        if version not in (1, 3):
            raise ValueError('목록 캐시의 기록 형식을 확인할 수 없습니다.')
        mode = ('shared' if environment.get('CODEX_MANAGER_SHARED_CATALOG') else 'viewer') + ':' + str(version)
        if environment.get('CODEX_MANAGER_SHARED_EXECUTION') == '1':
            mode += ':editable-canonical-v1'
        entries = catalog.get('entries', []) if version == 1 else catalog.get('sources', []) + catalog.get('legacySources', [])
        identities = sorted({(e['hostId'], e['sourceStoreId'], str(Path(e['codexHome']).resolve())) for e in entries})
    signature = hashlib.sha256(json.dumps([mode, identities], ensure_ascii=False).encode()).hexdigest()
    marker = home / _MARKER
    _assert_owned_path(marker, home)
    if marker.is_symlink() or (marker.exists() and marker.stat().st_nlink != 1):
        raise ValueError('목록 캐시 설정 파일이 다른 파일과 연결되어 있습니다.')
    saved = json.loads(marker.read_text(encoding='utf-8')) if marker.exists() else {}
    outcomes = []
    for database in sorted((home / 'sqlite').glob('codex*.db')):
        _assert_owned_path(database, home)
        if database.is_symlink() or database.stat().st_nlink != 1:
            raise ValueError('목록 캐시가 프로필 전용 파일이 아닙니다.')
        if saved.get(database.name) == signature:
            continue
        connection = sqlite3.connect(database.as_uri() + '?mode=rw', uri=True, timeout=5)
        try:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'local_thread_catalog' not in tables:
                continue
            selected = [table for table in _TABLES if table in tables]
            if any('host_id' not in {row[1] for row in connection.execute(f'PRAGMA table_info({table})')} for table in selected):
                raise ValueError('앱 목록 캐시의 구조가 변경되어 자동 정리를 중단했습니다.')
            if 'local_thread_catalog_metadata' in tables and 'catalog_revision' not in {
                row[1] for row in connection.execute('PRAGMA table_info(local_thread_catalog_metadata)')
            }:
                raise ValueError('앱 목록 캐시의 버전 구조를 확인할 수 없습니다.')
            count = connection.execute("SELECT count(*) FROM local_thread_catalog WHERE host_id='local'").fetchone()[0]
            backup = home / '.manager-cache-backups' / (database.stem + '-' + uuid4().hex + '.db')
            _assert_owned_path(backup, home)
            backup.parent.mkdir(exist_ok=True)
            with closing(sqlite3.connect(backup)) as target:
                connection.backup(target)
            connection.execute('BEGIN IMMEDIATE')
            for table in selected:
                connection.execute(f"DELETE FROM {table} WHERE host_id='local'")
            if 'local_thread_catalog_metadata' in tables:
                connection.execute('UPDATE local_thread_catalog_metadata SET catalog_revision=catalog_revision+1')
            connection.commit()
            saved[database.name] = signature
            outcomes.append(dict(database=database.name, removed_cache_rows=count, backup=str(backup)))
        finally:
            connection.close()
    atomic_json(marker, saved)
    return dict(state='rebuilt' if outcomes else 'unchanged', databases=outcomes,
                removed_cache_rows=sum(item['removed_cache_rows'] for item in outcomes))
