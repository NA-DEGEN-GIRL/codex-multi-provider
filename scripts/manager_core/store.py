"""Versioned manager state with atomic replacement and interprocess exclusion."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from uuid import UUID, uuid4


def now():
    return datetime.now(timezone.utc).isoformat()


def identifier(value):
    if not isinstance(value, str):
        raise ValueError('ID가 올바르지 않습니다.')
    return str(UUID(value))


def label(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 160:
        raise ValueError('별칭은 1~160자로 입력하세요.')
    if any(ord(c) < 32 for c in value):
        raise ValueError('별칭에 제어 문자를 사용할 수 없습니다.')
    return value.strip()


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name+'.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        deadline = time.monotonic() + 1
        while True:
            try:
                os.replace(temporary, path)
                break
            except PermissionError as error:
                # A Windows reader may briefly deny replacement of its open
                # file. Keep the old complete document until that handle closes.
                if (os.name != 'nt' or getattr(error, 'winerror', None) not in (5, 32, 33)
                        or time.monotonic() >= deadline):
                    raise
                time.sleep(.01)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.directory = self.root / 'work/control-center'
        self.path = self.directory / 'state.json'
        self._lock = threading.RLock()
        self._local = threading.local()

    @contextmanager
    def locked(self):
        with self._lock:
            if getattr(self._local, 'held', False):
                yield
                return
            with self._file_locked():
                self._local.held = True
                try:
                    yield
                finally:
                    self._local.held = False

    @contextmanager
    def _file_locked(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / 'state.lock').open('a+b') as file:
            file.seek(0, os.SEEK_END)
            if file.tell() == 0:
                file.write(b'0'); file.flush()
            deadline = time.monotonic() + 10
            while True:
                try:
                    file.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError('다른 관리 작업이 상태를 저장 중입니다. 다시 시도하세요.')
                    time.sleep(.05)
            try:
                yield
            finally:
                file.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(file, fcntl.LOCK_UN)

    def read(self):
        # Readers and writers use the same interprocess lock. On Windows even
        # a complete atomic replace may temporarily deny another open handle.
        with self.locked():
            return self._read_unlocked()

    def _read_unlocked(self):
        if not self.path.exists():
            return dict(version=1, revision=0, profiles=[], shortcuts=[], deleted_shortcuts=[],
                        sources=[], representative_profile_id=None, notices=[])
        data = json.loads(self.path.read_text(encoding='utf-8-sig'))
        if data.get('version') != 1:
            raise RuntimeError('지원하지 않는 관리 데이터 버전입니다. 기존 파일을 보존했습니다.')
        return data

    def mutate(self, operation):
        with self.locked():
            data = self.read()
            result = operation(data)
            data['revision'] += 1
            data['updated_at'] = now()
            atomic_json(self.path, data)
            return deepcopy(result)

    def add_profile(self, alias, usage_account_id=None, source_home=None, *, external_model_id=None, external_settings=None):
        alias = label(alias)
        if usage_account_id:
            usage_account_id = identifier(usage_account_id)
        def add(data):
            if usage_account_id and any(p.get('usage_account_id') == usage_account_id for p in data['profiles']):
                return next(p for p in data['profiles'] if p.get('usage_account_id') == usage_account_id)
            pid = str(uuid4())
            directory = self.directory / 'profiles' / pid
            profile = dict(id=pid, alias=alias, usage_account_id=usage_account_id,
                           home=str(directory/'codex'), ui_home=str(directory/'ui'),
                           source_home=source_home, status='not_started', process_id=None,
                           policy=dict(enabled=False, model_ids=[], desired_revision=0, effective_revision=None),
                           created_at=now())
            if external_model_id:
                profile.update(auth_mode='external', profile_kind='external', runtime_channel='managed',
                               external_model_id=identifier(external_model_id), external_settings=deepcopy(external_settings or {}), alias_authority='manager')
            data['profiles'].append(profile)
            if not data['representative_profile_id']:
                data['representative_profile_id'] = pid
            self._source(data, directory/'codex', 'manager:'+pid, alias)
            if source_home:
                self._source(data, source_home, 'usage:'+usage_account_id, alias)
            return profile
        return self.mutate(add)

    @staticmethod
    def _source(data, home, sid, alias):
        canonical = str(Path(home).resolve())
        if not any(s['home'].casefold() == canonical.casefold() for s in data['sources']):
            data['sources'].append(dict(id=sid, home=canonical, host_id='local', alias=alias))

    def profile(self, profile_id, data=None):
        pid = identifier(profile_id)
        result = next((p for p in (data or self.read())['profiles'] if p['id'] == pid), None)
        if not result:
            raise ValueError('등록된 프로필을 찾을 수 없습니다.')
        return result

    def move_profile(self, profile_id, target_profile_id, position):
        """Move one live profile relative to another, without replacing stale lists."""
        pid, target_id = identifier(profile_id), identifier(target_profile_id)
        if position not in ('before', 'after'):
            raise ValueError('프로필 이동 위치가 올바르지 않습니다.')

        def move(data):
            profile, target = self.profile(pid, data), self.profile(target_id, data)
            if profile.get('removed_at') or target.get('removed_at'):
                raise ValueError('제거된 프로필은 이동할 수 없습니다. 목록을 새로고침하세요.')
            if pid != target_id:
                data['profiles'].remove(profile)
                index = data['profiles'].index(target) + (position == 'after')
                data['profiles'].insert(index, profile)
            return dict(profile_ids=[p['id'] for p in data['profiles'] if not p.get('removed_at')],
                        message='프로필 순서를 저장했습니다.')

        return self.mutate(move)

    @staticmethod
    def remote_source(data, binding, alias):
        from pathlib import PurePosixPath
        from .ssh_shim import validate_binding
        validated=validate_binding(binding,identifier(binding['profile_id']))
        expected=str(PurePosixPath(validated['remote_launcher']).parent/'codex')
        if binding.get('prepared') is not True or binding.get('remote_profile_home')!=expected:
            raise ValueError('검증된 SSH 프로필 원본 경로가 필요합니다.')
        sid='manager:'+validated['profile_id'];host='ssh:'+validated['alias']
        source=next((s for s in data['sources'] if s['id']==sid and s['host_id']==host),None)
        value=dict(id=sid,host_id=host,home=expected,alias=label(alias))
        if source is not None:
            if source['home']!=expected:raise ValueError('기존 SSH 대화의 원본 경로가 바뀌었습니다.')
            source.update(alias=value['alias'])
        else:data['sources'].append(value)
        return value

    def shortcut_add(self, alias, profile_id, thread_id, host_id, source_store_id, *, catalog_source=None):
        alias, profile_id, thread_id = label(alias), identifier(profile_id), identifier(thread_id)
        def add(data):
            if self.profile(profile_id, data).get('removed_at'):
                raise ValueError('목록에 있는 계정을 선택하세요.')
            if catalog_source is not None:
                from .remote_catalog import groups
                if catalog_source['id'] != source_store_id or catalog_source['host_id'] != host_id:
                    raise ValueError('대화 출처가 일치하지 않습니다.')
                group = groups(data).get(host_id[4:]) if host_id.startswith('ssh:') else None
                if not group or group['conflicted'] or group['host_identity'] != catalog_source.get('host_identity'):
                    raise ValueError('SSH 등록 정보가 바뀌었습니다. 목록에서 다시 확인하세요.')
                existing = next((s for s in data['sources'] if s['id'] == source_store_id and s['host_id'] == host_id), None)
                if existing is None:
                    data['sources'].append(deepcopy(catalog_source))
                elif any(existing.get(key) != catalog_source.get(key) for key in ('home', 'host_identity')):
                    raise ValueError('기존 SSH 대화의 원본이 바뀌었습니다. 목록에서 다시 확인하세요.')
            if not any(s['id'] == source_store_id and s['host_id'] == host_id for s in data['sources']):
                raise ValueError('등록된 대화 출처와 호스트를 선택하세요.')
            link = dict(id=str(uuid4()), alias=alias, profile_id=profile_id, thread_id=thread_id,
                        host_id=host_id, source_store_id=source_store_id, revision=0)
            data['shortcuts'].append(link)
            return link
        return self.mutate(add)

    def shortcut_move(self, shortcut_id, profile_id):
        sid = identifier(shortcut_id)
        def move(data):
            if self.profile(profile_id, data).get('removed_at'):
                raise ValueError('목록에 있는 계정을 선택하세요.')
            link = next((x for x in data['shortcuts'] if x['id'] == sid), None)
            if not link:
                raise ValueError('바로가기를 찾을 수 없습니다.')
            link['profile_id'] = profile_id
            link['revision'] += 1
            return link
        return self.mutate(move)

    def shortcut_rename(self, shortcut_id, alias):
        sid, alias = identifier(shortcut_id), label(alias)
        def rename(data):
            link = next((x for x in data['shortcuts'] if x['id'] == sid), None)
            if link is None:
                raise ValueError('바로가기를 찾을 수 없습니다.')
            link['alias'] = alias
            link['revision'] += 1
            return link
        return self.mutate(rename)

    def shortcut_delete(self, shortcut_id):
        sid = identifier(shortcut_id)
        def delete(data):
            link = next((x for x in data['shortcuts'] if x['id'] == sid), None)
            if not link:
                raise ValueError('바로가기를 찾을 수 없습니다.')
            data['shortcuts'].remove(link)
            data['deleted_shortcuts'].append(link)
            data['deleted_shortcuts'] = data['deleted_shortcuts'][-30:]
            return dict(deleted=sid, record_deleted=False)
        return self.mutate(delete)

    def shortcut_undo(self):
        def undo(data):
            if not data['deleted_shortcuts']:
                raise ValueError('복구할 바로가기가 없습니다.')
            link = data['deleted_shortcuts'].pop()
            if any(p['id']==link['profile_id'] and p.get('removed_at') for p in data['profiles']):
                raise ValueError('바로가기를 복구하려면 연결된 계정을 먼저 복원하세요.')
            if not any(p['id'] == link['profile_id'] for p in data['profiles']):
                link['profile_id'] = None
            if not any(x['id'] == link['id'] for x in data['shortcuts']):
                data['shortcuts'].append(link)
            return link
        return self.mutate(undo)
