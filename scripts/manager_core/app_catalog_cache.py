"""Invalidate native sidebar metadata when a managed HOME changes record identity mode.

Called only on the cold-launch path, after confirming the profile process exited.
The native app owns this cache and repopulates it from thread/list. Conversation
stores, accounts, projects, pins, the original app, and every host that is not an
explicitly configured managed SSH identity are not modified.

The local record-identity signature and every managed remote SSH host epoch are
tracked independently in the marker: a local signature change rebuilds only the
``local`` rows, a host epoch change rebuilds only that host's rows, and hosts
that are not explicitly configured as managed SSH identities are never touched.
``ssh_hosts`` defaults to empty, which keeps the previous local-only behaviour.

The caller owns discovery and validation: it passes the full native host IDs of
managed remote SSH bindings (for example ``remote-ssh-discovered:<alias>``) that
are prepared and bound to this profile. No connection probe happens here; the
native catalog is repopulated from a fresh ``thread/list`` afterwards, which also
supports stores that still list older projection IDs.
"""
import hashlib
import json
from contextlib import closing
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from .common import _assert_owned_path
from .store import atomic_json

_MARKER = '.manager-sidebar-cache.json'
_TABLES = ('local_thread_catalog', 'local_thread_catalog_sync_state',
           'local_thread_catalog_scan_checkpoints', 'local_thread_catalog_scan_entries')
# Older services read ``marker[database]`` as the local signature string, so the
# per-database value must stay flat. Managed host epochs live under one reserved
# top-level key that those services pass through untouched.
_LOCAL_KEY = '__local__'
_SSH_KEY = '__ssh_canonical__'
# Only manager-managed SSH bindings, and only their full native host IDs.
# Manually configured ssh hosts and generic remote-ssh IDs are never touched.
_MANAGED_HOST = re.compile(r'remote-ssh-discovered:[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
# One shared epoch for the canonical record-identity migration. A host is
# cleared once and then stays unchanged until this constant is bumped.
SSH_CANONICAL_EPOCH = 'ssh-canonical-v1'


def _host_epochs(ssh_hosts):
    """Validated managed SSH host IDs as ``{host_id: SSH_CANONICAL_EPOCH}``.

    Only full ``remote-ssh-discovered:<alias>`` host IDs are accepted; anything
    else is reported as skipped and is never modified.
    """
    epochs, skipped = {}, []
    if ssh_hosts is None or isinstance(ssh_hosts, (str, bytes)):
        return epochs, skipped
    for host_id in ssh_hosts:
        if not isinstance(host_id, str) or not _MANAGED_HOST.fullmatch(host_id):
            skipped.append(dict(host_id=host_id if isinstance(host_id, str) else None,
                                reason='unmanaged_host'))
            continue
        epochs[host_id] = SSH_CANONICAL_EPOCH
    return epochs, skipped


def _saved_local(saved, name):
    """One database's saved local signature, or None when it is not recorded.

    A dict value is only read for compatibility with an unreleased intermediate
    marker; new markers always write the flat string again.
    """
    value = saved.get(name)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        local = value.get(_LOCAL_KEY)
        return local if isinstance(local, str) else None
    return None


def _saved_hosts(saved, name):
    """Managed host epochs recorded under the reserved top-level key."""
    reserved = saved.get(_SSH_KEY)
    record = reserved.get(name) if isinstance(reserved, dict) else None
    if not isinstance(record, dict):
        return {}
    return {host: epoch for host, epoch in record.items()
            if isinstance(host, str) and isinstance(epoch, str)}


def prepare(home, environment, *, ssh_hosts=()):
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
    if not isinstance(saved, dict):
        saved = {}
    wanted_hosts, skipped_hosts = _host_epochs(ssh_hosts)

    # Validate every candidate database before the first write so an unknown
    # schema or a shared file can never leave a half-migrated cache behind.
    plans = []
    for database in sorted((home / 'sqlite').glob('codex*.db')):
        _assert_owned_path(database, home)
        if database.is_symlink() or database.stat().st_nlink != 1:
            raise ValueError('목록 캐시가 프로필 전용 파일이 아닙니다.')
        previous_local = _saved_local(saved, database.name)
        previous_hosts = _saved_hosts(saved, database.name)
        local_changed = previous_local != signature
        host_changes = {host: epoch for host, epoch in wanted_hosts.items()
                        if previous_hosts.get(host) != epoch}
        if not local_changed and not host_changes:
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
        finally:
            connection.close()
        plans.append(dict(database=database, selected=selected, tables=tables,
                          previous_local=previous_local, previous_hosts=previous_hosts,
                          local_changed=local_changed, host_changes=host_changes))

    outcomes = []
    for plan in plans:
        database = plan['database']
        connection = sqlite3.connect(database.as_uri() + '?mode=rw', uri=True, timeout=5)
        try:
            host_rows = {}
            if plan['local_changed']:
                host_rows['local'] = connection.execute(
                    "SELECT count(*) FROM local_thread_catalog WHERE host_id='local'").fetchone()[0]
            for host in plan['host_changes']:
                host_rows[host] = connection.execute(
                    'SELECT count(*) FROM local_thread_catalog WHERE host_id=?', (host,)).fetchone()[0]
            backup = home / '.manager-cache-backups' / (database.stem + '-' + uuid4().hex + '.db')
            _assert_owned_path(backup, home)
            backup.parent.mkdir(exist_ok=True)
            with closing(sqlite3.connect(backup)) as target:
                connection.backup(target)
            connection.execute('BEGIN IMMEDIATE')
            for table in plan['selected']:
                if plan['local_changed']:
                    connection.execute(f"DELETE FROM {table} WHERE host_id='local'")
                for host in plan['host_changes']:
                    connection.execute(f'DELETE FROM {table} WHERE host_id=?', (host,))
            if 'local_thread_catalog_metadata' in plan['tables']:
                connection.execute('UPDATE local_thread_catalog_metadata SET catalog_revision=catalog_revision+1')
            connection.commit()
            # Older services compare ``marker[database]`` with their own local
            # signature, so that entry must stay the flat string they expect.
            saved[database.name] = signature if plan['local_changed'] else plan['previous_local']
            record = dict(plan['previous_hosts'])
            record.update(wanted_hosts)
            # Local-only launches keep the historic marker shape untouched.
            if record:
                reserved = saved.get(_SSH_KEY)
                reserved = dict(reserved) if isinstance(reserved, dict) else {}
                reserved[database.name] = record
                saved[_SSH_KEY] = reserved
            local_rows = host_rows.get('local', 0)
            remote_rows = sum(rows for host, rows in host_rows.items() if host != 'local')
            outcomes.append(dict(database=database.name, backup=str(backup), hosts=host_rows,
                                 removed_cache_rows=local_rows + remote_rows,
                                 removed_cache_rows_local=local_rows,
                                 removed_cache_rows_remote=remote_rows))
        finally:
            connection.close()
    atomic_json(marker, saved)
    hosts_total = {}
    for item in outcomes:
        for host, rows in item['hosts'].items():
            hosts_total[host] = hosts_total.get(host, 0) + rows
    return dict(state='rebuilt' if outcomes else 'unchanged', databases=outcomes,
                removed_cache_rows=sum(item['removed_cache_rows'] for item in outcomes),
                removed_cache_rows_local=sum(item['removed_cache_rows_local'] for item in outcomes),
                removed_cache_rows_remote=sum(item['removed_cache_rows_remote'] for item in outcomes),
                hosts=hosts_total, skipped_hosts=skipped_hosts)
