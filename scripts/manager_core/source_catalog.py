"""Publish registered homes for native discovery; keep manager link IDs stable."""
import hashlib
import json
from pathlib import Path
from uuid import UUID, uuid5

from .authority import _authority_guard
from .catalog_origin import validated_origin
from .record_catalog import PROJECTION_NAMESPACE
from .store import atomic_json

MAX_BYTES = 4 * 1024 * 1024
FILENAME = 'local-sources.json'


def catalog_path(root):
    return Path(root) / 'work/control-center/catalog' / FILENAME


def _object(path, limit):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError('공통 목록 연결 파일의 경로 또는 크기가 올바르지 않습니다.')
    with path.open('rb') as stream:
        body = stream.read(limit + 1)
    if len(body) > limit:
        raise ValueError('공통 목록 연결 파일이 너무 큽니다.')
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError('공통 목록 연결 파일이 올바르지 않습니다.')
    return value


def inventory(sources):
    managed, legacy, bindings = [], [], {}
    identities, homes = set(), set()
    pending = 0
    for source in sources:
        if source.get('host_id') != 'local' or source.get('history_migrated_to'):
            continue
        sid, home = source['id'], Path(source['home'])
        if not isinstance(sid, str) or not 1 <= len(sid) <= 256 or sid in identities:
            raise ValueError('공통 목록의 저장소 ID가 중복되거나 올바르지 않습니다.')
        identities.add(sid)
        if len(identities) > 256 or not home.is_absolute():
            raise ValueError('공통 목록의 저장소 수 또는 경로가 올바르지 않습니다.')
        if not home.exists():
            pending += 1
            continue
        if not home.is_dir() or home.resolve(strict=True) != home:
            raise ValueError('등록한 저장소의 실제 경로가 변경되었습니다.')
        if home in homes:
            raise ValueError('한 저장소에 여러 ID가 등록되어 있습니다.')
        homes.add(home)
        if sid.startswith('manager:'):
            if str(UUID(sid[8:])) != sid[8:]:
                raise ValueError('관리 저장소 ID가 올바르지 않습니다.')
            marker = home / 'managed-source.json'
            if not marker.exists():
                pending += 1
                continue  # An account not prepared yet is not enrolled by a read.
            if _object(marker, 4096) != {'host_id': 'local', 'store_id': sid}:
                raise ValueError('관리 저장소 식별자가 일치하지 않습니다.')
            native_id, target = sid, managed
        else:
            native_id = 'legacy:' + hashlib.sha256(str(home).encode('utf-8')).hexdigest()
            target = legacy
        entry = dict(hostId='local', sourceStoreId=native_id, codexHome=str(home))
        target.append(entry)
        bindings[native_id] = {**source, 'native_id': native_id}
    for entries in (managed, legacy):
        entries.sort(key=lambda item: item['sourceStoreId'])
    return dict(version=3, hostId='local', sources=managed, legacySources=legacy), bindings, pending


def build(root, sources, *, include_paginated=False):
    """No record enumeration or credential access; the runtime discovers new records."""
    root = Path(root).resolve()
    document, bindings, pending = inventory(sources)
    path = catalog_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.resolve(strict=True) != path.parent:
        raise ValueError('공통 목록 저장 경로가 변경되었습니다.')
    with _authority_guard(path.with_suffix('.guard')):
        if not path.exists() or _object(path, MAX_BYTES) != document:
            atomic_json(path, document)
    return dict(path=str(path), sources=len(bindings), pending_sources=pending,
                entries=None, complete=pending == 0, format='native_sources_v3')


def registered(root, path, sources):
    path = Path(path)
    expected = catalog_path(Path(root).resolve())
    if path != expected or path.resolve(strict=True) != expected:
        raise ValueError('공통 목록 연결 경로가 변경되었습니다.')
    document = _object(path, MAX_BYTES)
    if (set(document) != {'version', 'hostId', 'sources', 'legacySources'}
            or document['version'] != 3 or document['hostId'] != 'local'
            or not isinstance(document['sources'], list) or not isinstance(document['legacySources'], list)
            or len(document['sources']) + len(document['legacySources']) > 256):
        raise ValueError('공통 목록 연결 형식이 올바르지 않습니다.')
    current, bindings, _ = inventory(sources)
    enrolled = {}
    for key in ('sources', 'legacySources'):
        for entry in document[key]:
            if not isinstance(entry, dict) or entry not in current[key]:
                raise ValueError('공통 목록의 등록 출처가 변경되었습니다.')
            sid = entry['sourceStoreId']
            if sid in enrolled:
                raise ValueError('공통 목록의 등록 출처가 중복되었습니다.')
            enrolled[sid] = bindings[sid]
    return enrolled


def projection_id(native_id, thread_id):
    if str(UUID(thread_id)) != thread_id:
        raise ValueError('대화 ID가 올바르지 않습니다.')
    return str(uuid5(PROJECTION_NAMESPACE, 'local\0' + native_id + '\0' + thread_id))


def project(root, path, sources, shortcut):
    if shortcut.get('host_id', 'local') != 'local':
        raise ValueError('로컬 공통 목록의 대화가 아닙니다.')
    matches = [s for s in registered(root, path, sources).values()
               if s['id'] == shortcut.get('source_store_id')]
    if len(matches) != 1:
        raise ValueError('대화의 등록 출처를 확인할 수 없습니다.')
    return projection_id(matches[0]['native_id'], shortcut['thread_id'])


def resolve(root, path, sources, thread):
    origin = validated_origin(thread)
    if origin is None:
        raise ValueError('공통 기록의 원본 연결을 확인할 수 없습니다.')
    source = registered(root, path, sources).get(origin['sourceStoreId'])
    if source is None or projection_id(source['native_id'], origin['canonicalThreadId']) != thread['id']:
        raise ValueError('공통 기록의 원본 ID가 일치하지 않습니다.')
    return dict(thread_id=origin['canonicalThreadId'], source_store_id=source['id'], host_id='local',
                source_alias=source['alias'], projection_thread_id=thread['id'])
