"""Explicit, same-host transfer of a canonical managed conversation subtree.

No GUI launch, model prompt, automatic resume, raw database copy, or timeout
takeover happens here. Runtime admission and recorder locks provide the proof;
the durable journal prevents an uncertain close/CAS/activation being replayed.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

from . import authority
from .runtime_admin import AdminError
from .store import atomic_json, now

MAX_THREADS = 128
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_TERMINAL = frozenset({'complete', 'blocked_before_close'})
_BINDING_KEYS = {'threadId', 'hostId', 'sourceStoreId', 'ownerProfileId',
                 'ownershipEpoch', 'recordRevision'}


class HandoffError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _id(value):
    if not isinstance(value, str):
        raise HandoffError('invalid_id', '올바른 대화·프로필 ID가 필요합니다.')
    try:
        if str(UUID(value)) != value:
            raise ValueError()
    except ValueError:
        raise HandoffError('invalid_id', '올바른 대화·프로필 ID가 필요합니다.') from None
    return value


def _ids(value):
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_THREADS:
        raise HandoffError('invalid_scope', '영향받는 작업 범위를 확인하지 못했습니다.')
    result = [_id(item) for item in value]
    if len(set(result)) != len(result):
        raise HandoffError('invalid_scope', '작업 범위에 중복된 대화 ID가 있습니다.')
    return result


def _binding(grant):
    return dict(threadId=grant['thread_id'], hostId=grant['host_id'],
                sourceStoreId=grant['store_id'], ownerProfileId=grant['owner_profile_id'],
                ownershipEpoch=grant['epoch'], recordRevision=grant['revision'])


def _safe_read(path, max_bytes):
    path = Path(path)
    info = path.lstat()
    if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400 or info.st_size > max_bytes:
        raise HandoffError('unsafe_path', '관리 기록의 경로 또는 크기를 확인할 수 없습니다.')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise HandoffError('invalid_record', '관리 기록의 형식이 올바르지 않습니다.')
    return value


class HandoffManager:
    def __init__(self, root, store, instances=None, *, admin_factory=None):
        self.root = Path(root).resolve()
        self.store = store
        self.instances = instances  # Compatibility with root's integration contract; never uses GUI identity.
        self.directory = self.store.directory / 'handoffs'
        if admin_factory is None:
            from .runtime_admin import AdminClient
            admin_factory = lambda profile: AdminClient(self.root, profile['id'], profile['generation'])
        self.admin_factory = admin_factory

    def _profile(self, profile_id, *, require_account=True):
        profile = deepcopy(self.store.profile(_id(profile_id)))
        if profile.get('view_only') or (require_account and profile.get('account_missing')):
            raise HandoffError('profile_unavailable', '기록 보기 전용 또는 삭제된 계정에는 실행을 인계할 수 없습니다.')
        return profile

    def _home(self, profile):
        expected = self.store.directory / 'profiles' / _id(profile['id']) / 'codex'
        actual = Path(profile['home'])
        if not expected.is_dir() or actual.resolve(strict=True) != expected or expected.resolve(strict=True) != expected:
            raise HandoffError('source_unmanaged', '관리 앱이 만든 실제 원본 저장소만 인계할 수 있습니다.')
        marker = _safe_read(expected / 'managed-source.json', 4096)
        if marker != {'host_id': 'local', 'store_id': 'manager:' + profile['id']}:
            raise HandoffError('source_identity', '원본 저장소의 식별자가 일치하지 않습니다.')
        return expected

    def _read_authority(self, home, thread_id):
        return authority.read(home, thread_id)

    def _transfer_authority(self, plan, grants, transaction_id):
        return authority.transfer_many(plan['home'], grants, plan['target']['id'], release_verified=True)

    def _runtime_host(self, reference):
        return reference['host_id']

    def _compatible(self, owner, target, source_identity, target_identity):
        return os.path.normcase(source_identity['runtime']['executable_path']) == os.path.normcase(target_identity['runtime']['executable_path'])

    def _reference(self, reference):
        if not isinstance(reference, dict):
            raise HandoffError('invalid_reference', '작업의 원본 출처가 필요합니다.')
        thread_id = _id(reference.get('thread_id'))
        source = reference.get('source_store_id')
        if reference.get('host_id') != 'local' or not isinstance(source, str) or not source.startswith('manager:'):
            raise HandoffError('source_not_writable', '현재는 이 Windows의 관리 프로필 기록만 인계할 수 있습니다. 기존·llm-usage 기록은 보존합니다.')
        # The canonical storage profile can outlive its old login account.
        # Only the current owner and target need usable execution accounts.
        source_profile = self._profile(_id(source[8:]), require_account=False)
        home = self._home(source_profile)
        registered = next((s for s in self.store.read()['sources'] if s['id'] == source and s['host_id'] == 'local'), None)
        if not registered or Path(registered['home']).resolve() != home:
            raise HandoffError('source_unregistered', '등록된 원본 출처를 확인하지 못했습니다.')
        return dict(thread_id=thread_id, host_id='local', source_store_id=source), source_profile, home

    def _admin(self, profile):
        if not profile.get('generation'):
            raise HandoffError('runtime_not_started', '해당 계정의 관리 런타임을 먼저 준비해야 합니다. 실행하지 않았다는 사실은 기록 해제 증거가 아닙니다.')
        _id(profile['generation'])
        admin = self.admin_factory(profile)
        identity = self._identity(admin)
        if not isinstance(identity, dict) or identity.get('generation') != profile['generation']:
            raise HandoffError('runtime_identity', '관리 런타임의 실행 세대가 일치하지 않습니다.')
        self._health(admin, profile['generation'])
        return admin, deepcopy(identity)

    @staticmethod
    def _identity(admin):
        identity = admin.identities()
        if not isinstance(identity, dict) or not isinstance(identity.get('generation'), str):
            raise HandoffError('runtime_identity', '관리 런타임의 실행 세대를 확인하지 못했습니다.')
        normalized = {'generation': _id(identity['generation'])}
        for kind in ('runtime', 'proxy'):
            item = identity.get(kind)
            if not isinstance(item, dict):
                raise HandoffError('runtime_identity', '실제 런타임 프로세스의 생존 정보를 확인하지 못했습니다.')
            pid, created = item.get('pid', item.get('process_id')), item.get('created', item.get('process_created'))
            if (type(pid) is not int or pid <= 0 or type(created) is not int or created <= 0
                    or not isinstance(item.get('executable_path'), str)
                    or not Path(item['executable_path']).is_absolute()):
                raise HandoffError('runtime_identity', '실제 런타임 프로세스의 생존 정보를 확인하지 못했습니다.')
            normalized[kind] = dict(pid=pid, created=created, executable_path=item['executable_path'])
        return normalized

    @staticmethod
    def _health(admin, generation):
        result = admin.request('manager/maintenance/status', {})
        if (result.get('generation') != generation
                or any(result.get(key) is not True for key in ('connected', 'initialized', 'streamComplete', 'accountReady'))
                or result.get('held') is not False):
            raise HandoffError('runtime_not_ready', '원본과 대상 런타임이 인증·초기화를 마쳐야 합니다. 업데이트 점검 중에는 인계를 기다립니다.')
        # Counts for other independent tasks are deliberately not an idle gate.

    @staticmethod
    def _same_identity(admin, expected):
        if HandoffManager._identity(admin) != expected:
            raise HandoffError('runtime_changed', '확인 중 런타임 프로세스가 바뀌었습니다. 이전 증거를 재사용하지 않습니다.')

    def _owning_root(self, admin, requested):
        seen, current = set(), requested
        for _ in range(MAX_THREADS):
            if current in seen:
                raise HandoffError('parent_cycle', '자식 작업의 관계에 순환이 있습니다.')
            seen.add(current)
            thread = admin.request('thread/read', {'threadId': current, 'includeTurns': False}).get('thread', {})
            if thread.get('id') != current or 'parentThreadId' not in thread:
                raise HandoffError('parentage_unknown', '대화와 자식 작업의 원본 관계를 확인하지 못했습니다.')
            parent = thread['parentThreadId']
            if parent is None:
                # Canonical thread/read is deliberately a durable read-only
                # view, so its status does not certify actor residency. Query
                # the runtime inventory; strict idle/close proves release later.
                if current not in self._loaded(admin):
                    raise HandoffError('source_not_loaded', '현재 런타임에 로드되지 않은 원본 작업의 쓰기 해제는 아직 증명할 수 없습니다. 종료 상태만으로 인계하지 않습니다.')
                return current
            current = _id(parent)  # Never walk sessionId or forkedFromId: independent forks remain independent.
        raise HandoffError('parentage_limit', '자식 작업 관계가 확인 가능한 범위를 초과했습니다.')

    @staticmethod
    def _loaded(admin):
        ids, cursor, cursors = set(), None, set()
        for _ in range(128):
            result = admin.request('thread/loaded/list', {'limit': 256, 'cursor': cursor})
            page = result.get('data')
            if not isinstance(page, list) or len(page) > 256:
                raise HandoffError('target_inventory', '대상의 로드된 작업 목록을 확인하지 못했습니다.')
            for item in page:
                item = _id(item)
                if item in ids:
                    raise HandoffError('target_inventory', '대상 작업 목록이 확인 중 바뀌었습니다.')
                ids.add(item)
            cursor = result.get('nextCursor')
            if cursor is None:
                return ids
            if not isinstance(cursor, str) or cursor in cursors:
                raise HandoffError('target_inventory', '대상 작업 목록의 페이지를 확인하지 못했습니다.')
            cursors.add(cursor)
        raise HandoffError('target_inventory', '대상 작업 목록의 확인 범위를 초과했습니다.')

    def recovery_status(self, reference=None):
        output = []
        if not self.directory.exists():
            return output
        files = list(self.directory.glob('*.json'))
        if len(files) > 10000:
            raise HandoffError('journal_limit', '인계 기록 점검이 필요합니다.')
        for file in files:
            value = _safe_read(file, 256 * 1024)
            if value.get('version') != 1 or value.get('status') not in _TERMINAL:
                if reference and (value.get('host_id') != reference['host_id']
                                  or value.get('source_store_id') != reference['source_store_id']
                                  or reference['thread_id'] not in value.get('thread_ids', [value.get('requested_thread_id')])):
                    continue
                output.append(dict(transaction_id=value.get('transaction_id', file.stem),
                                   status='recovery_required', phase=value.get('status', 'unknown'),
                                   message='이 대화의 인계가 진행 중이거나 결과 확인이 필요합니다. 같은 요청을 자동으로 다시 실행하지 않습니다.'))
        return output

    def _plan(self, reference, target_profile_id):
        ref, source_profile, home = self._reference(reference)
        pending = self.recovery_status(ref)
        if pending:
            raise HandoffError('recovery_required', pending[0]['message'])
        target = self._profile(target_profile_id)
        self._home(target)
        grant = self._read_authority(home, ref['thread_id'])
        owner = self._profile(grant['owner_profile_id'])
        target_admin, target_identity = self._admin(target)
        if owner['id'] == target['id']:
            return dict(reference=ref, home=home, source_profile=source_profile, owner=owner, target=target,
                        target_admin=target_admin, target_identity=target_identity, requires_handoff=False,
                        root_thread_id=ref['thread_id'], thread_ids=[ref['thread_id']], grants=[grant])
        source_admin, source_identity = self._admin(owner)
        if not self._compatible(owner, target, source_identity, target_identity):
            raise HandoffError('runtime_compatibility', '서로 다른 런타임 빌드의 인계 호환은 아직 확인하지 못했습니다.')
        root_id = self._owning_root(source_admin, ref['thread_id'])
        observed = source_admin.request('thread/managedIdleStatus', {'threadId': root_id})
        ids = _ids(observed.get('observedThreadIds'))
        if (observed.get('threadId') != root_id or observed.get('hostId') != self._runtime_host(ref)
                or observed.get('sourceStoreId') != ref['source_store_id']
                or root_id not in ids or ref['thread_id'] not in ids
                or observed.get('proofScope') != 'advisory'):
            raise HandoffError('source_scope', '원본 런타임의 대화 범위와 실제 출처가 일치하지 않습니다.')
        blockers = observed.get('blockers')
        cold_claims = []
        if (observed.get('idle') is False and isinstance(blockers, list)
                and 0 < len(blockers) < len(ids)
                and all(isinstance(b, dict) and set(b) == {'threadId', 'kind'}
                        and b['kind'] == 'coldDescendantRequiresWriterClaim'
                        and b['threadId'] in ids and b['threadId'] != root_id for b in blockers)
                and len({b['threadId'] for b in blockers}) == len(blockers)):
            # The advisory endpoint deliberately cannot certify a cold child.
            # Strict close claims its canonical writer lock, checks persisted
            # input/parentage, and retains the claim until release is proved.
            # Nothing here marks it idle or authorizes an ownership change.
            cold_claims = [b['threadId'] for b in blockers]
        elif observed.get('idle') is not True or blockers != []:
            raise HandoffError('source_busy', '이 대화 또는 자식의 실행·도구·승인·대기 작업이 남아 있습니다. 이 작업이 끝난 뒤 인계할 수 있습니다.')
        grants = [self._read_authority(home, tid) for tid in ids]
        if any(g['owner_profile_id'] != owner['id'] or g['store_id'] != ref['source_store_id'] for g in grants):
            raise HandoffError('mixed_authority', '자식 작업의 원본 소유자가 일치하지 않습니다.')
        if set(ids) & self._loaded(target_admin):
            raise HandoffError('target_loaded', '대상 런타임에 같은 대화가 이미 로드되어 있습니다. 다른 작업은 그대로 유지합니다.')
        self._same_identity(source_admin, source_identity)
        self._same_identity(target_admin, target_identity)
        return dict(reference=ref, home=home, source_profile=source_profile, owner=owner, target=target,
                    source_admin=source_admin, source_identity=source_identity, target_admin=target_admin,
                    target_identity=target_identity, root_thread_id=root_id, thread_ids=ids,
                    grants=grants, requires_handoff=True, strict_writer_claim_thread_ids=cold_claims)

    @staticmethod
    def _public(plan):
        return dict(status='ready', requires_handoff=plan['requires_handoff'],
                    source=deepcopy(plan['reference']), owner_profile_id=plan['owner']['id'],
                    target_profile_id=plan['target']['id'], root_thread_id=plan['root_thread_id'],
                    thread_ids=list(plan['thread_ids']), writer_release_verified=False,
                    strict_writer_claim_thread_ids=plan.get('strict_writer_claim_thread_ids', []),
                    message='해당 대화와 자식만 종료·기록 확정한 뒤 선택한 계정에 연결합니다. 메시지는 자동 전송하지 않습니다.' if plan['requires_handoff'] else '선택한 계정이 이미 이 대화의 실행 소유자입니다.')

    @staticmethod
    def _failure(error, *, uncertain=False, transaction_id=None):
        code = getattr(error, 'code', 'handoff_failed')
        message = str(error) if isinstance(error, HandoffError) else '런타임 또는 원본 기록의 상태를 확인하지 못했습니다. 기존 기록을 복사하거나 실행을 다시 보내지 않았습니다.'
        result = dict(status='recovery_required' if uncertain or code == 'recovery_required' else 'blocked',
                      code=code, message=message, writer_release_verified=False)
        if transaction_id:
            result['transaction_id'] = transaction_id
        return result

    def preview(self, reference, target_profile_id):
        try:
            return self._public(self._plan(reference, target_profile_id))
        except (OSError, ValueError, RuntimeError, KeyError) as error:
            return self._failure(error)

    def _write(self, file, value):
        value['updated_at'] = now()
        atomic_json(file, value)
        if os.name != 'nt':
            fd = os.open(file.parent, os.O_RDONLY)
            try: os.fsync(fd)
            finally: os.close(fd)

    def _manifest(self, profile, required):
        """Preserve active owned bindings and valid foreign read-only references."""
        parent = self.store.directory / 'profiles' / profile['id']
        file = parent / 'managed-sources.json'
        if parent.resolve(strict=True) != parent:
            raise HandoffError('manifest_path', '대상 프로필 설정의 실제 경로가 변경되었습니다.')
        with authority._authority_guard(parent / 'managed-sources.guard'):
            existing = _safe_read(file, MAX_MANIFEST_BYTES)
            if (set(existing) != {'version', 'hostId', 'profileId', 'sources', 'bindings'}
                    or existing['version'] != 1 or existing['hostId'] != 'local' or existing['profileId'] != profile['id']):
                raise HandoffError('target_manifest', '대상 런타임의 관리 저장소 설정을 확인하지 못했습니다.')
            sources, homes, bindings = [], {}, {}
            for candidate in self.store.read()['profiles']:
                if candidate.get('view_only'):
                    continue
                marker = Path(candidate['home']) / 'managed-source.json'
                if not marker.is_file():
                    continue
                home = self._home(candidate)
                sid = 'manager:' + candidate['id']
                sources.append(dict(hostId='local', sourceStoreId=sid, codexHome=str(home)))
                homes[sid] = home
                for path in (home / 'managed-authority').glob('*.json'):
                    grant = authority.read(home, path.stem)
                    if grant['owner_profile_id'] == profile['id']:
                        if grant['thread_id'] in bindings and bindings[grant['thread_id']]['sourceStoreId'] != sid:
                            raise HandoffError('thread_collision', '서로 다른 원본에 같은 대화 ID가 있습니다.')
                        bindings[grant['thread_id']] = _binding(grant)
            if not isinstance(existing['bindings'], list) or len(existing['bindings']) > 4096:
                raise HandoffError('target_manifest', '대상 기록 연결 목록의 형식이 올바르지 않습니다.')
            for item in existing['bindings']:
                if not isinstance(item, dict) or set(item) != _BINDING_KEYS:
                    raise HandoffError('target_manifest', '대상 기록 연결 항목의 형식이 올바르지 않습니다.')
                tid = _id(item['threadId'])
                home = homes.get(item['sourceStoreId'])
                if home is None:
                    continue
                current = authority.read(home, tid)
                if _binding(current) != item:
                    continue
                if tid in bindings and bindings[tid]['sourceStoreId'] != item['sourceStoreId']:
                    raise HandoffError('thread_collision', '서로 다른 원본에 같은 대화 ID가 있습니다.')
                bindings[tid] = item
            for grant in required:
                item = _binding(grant)
                home = homes.get(grant['store_id'])
                if home is None or authority.read(home, grant['thread_id']) != grant:
                    raise HandoffError('grant_changed', '대상의 연결을 준비하는 동안 원본 실행 소유자가 변경되었습니다.')
                previous = bindings.get(grant['thread_id'])
                if previous and previous['sourceStoreId'] != grant['store_id']:
                    raise HandoffError('thread_collision', '대상에 같은 ID의 다른 원본 대화가 있습니다.')
                bindings[grant['thread_id']] = item
            value = dict(version=1, hostId='local', profileId=profile['id'], sources=sources,
                         bindings=sorted(bindings.values(), key=lambda b: b['threadId']))
            if len(sources) > 256 or len(bindings) > 4096 or len(json.dumps(value).encode('utf-8')) > MAX_MANIFEST_BYTES:
                raise HandoffError('manifest_limit', '대상 기록 연결 목록의 지원 범위를 초과했습니다.')
            atomic_json(file, value)
        return str(file)

    def continue_conversation(self, reference, target_profile_id):
        journal, file, mutation_possible = None, None, False
        try:
            plan = self._plan(reference, target_profile_id)
            if not plan['requires_handoff']:
                return self._public(plan)
            self.directory.mkdir(parents=True, exist_ok=True)
            if self.directory.resolve() != self.directory:
                raise HandoffError('journal_path', '인계 기록 폴더의 실제 경로가 변경되었습니다.')
            lock = self.directory / (plan['reference']['source_store_id'][8:] + '-' + plan['root_thread_id'] + '.guard')
            with authority._authority_guard(lock):
                # Advisory preview is never reused as a release proof.
                plan = self._plan(reference, target_profile_id)
                if not plan['requires_handoff']:
                    return self._public(plan)
                tx = str(uuid4()); file = self.directory / (tx + '.json')
                journal = dict(version=1, transaction_id=tx, status='preparing', created_at=now(),
                               host_id=plan['reference']['host_id'], source_store_id=plan['reference']['source_store_id'],
                               requested_thread_id=plan['reference']['thread_id'], root_thread_id=plan['root_thread_id'],
                               thread_ids=plan['thread_ids'], source_owner_profile_id=plan['owner']['id'],
                               target_profile_id=plan['target']['id'], source_runtime=plan['source_identity'],
                               target_runtime=plan['target_identity'], expected_grants=plan['grants'],
                               changed_grants=[], close_proof=None, activation=None)
                self._write(file, journal)
                journal['prepared_manifest'] = self._manifest(plan['target'], plan['grants'])
                for tid in plan['thread_ids']:
                    target_view = plan['target_admin'].request('thread/read', {'threadId': tid, 'includeTurns': False})
                    if target_view.get('thread', {}).get('id') != tid:
                        raise HandoffError('target_read_failed', '대상에서 같은 원본 대화를 읽을 수 있는지 확인하지 못했습니다.')
                self._same_identity(plan['source_admin'], plan['source_identity'])
                self._same_identity(plan['target_admin'], plan['target_identity'])
                self._health(plan['target_admin'], plan['target']['generation'])
                if set(plan['thread_ids']) & self._loaded(plan['target_admin']):
                    raise HandoffError('target_loaded', '준비하는 동안 대상에 같은 대화가 로드되었습니다.')
                for grant in plan['grants']:
                    if self._read_authority(plan['home'], grant['thread_id']) != grant:
                        raise HandoffError('grant_changed', '종료 전 원본 실행 소유자가 변경되었습니다.')
                journal['status'] = 'close_requested'; self._write(file, journal)
                mutation_possible = True
                try:
                    proof = plan['source_admin'].request('thread/managedCloseIdle', {'threadId': plan['root_thread_id']}, timeout=75)
                except AdminError as error:
                    # Only the authenticated admin transport can prove that
                    # dispatch never occurred. Runtime response failures and
                    # timeouts retain the durable uncertain-close barrier.
                    if error.uncertain is False:
                        mutation_possible = False
                    raise
                closed = _ids(proof.get('closedThreadIds'))
                if (proof.get('threadId') != plan['root_thread_id'] or proof.get('writerReleaseVerified') is not True
                        or set(closed) != set(plan['thread_ids'])):
                    raise HandoffError('release_unverified', '대화와 전체 자식의 종료·기록 확정 범위가 일치하지 않습니다.')
                self._same_identity(plan['source_admin'], plan['source_identity'])
                journal['close_proof'] = proof; journal['status'] = 'source_released'; self._write(file, journal)
                self._same_identity(plan['target_admin'], plan['target_identity'])
                self._health(plan['target_admin'], plan['target']['generation'])
                ordered = sorted(plan['grants'], key=lambda grant: (grant['thread_id'] == plan['root_thread_id'], grant['thread_id']))
                journal['status'] = 'authority_commit_requested'; self._write(file, journal)
                changed = self._transfer_authority(plan, ordered, tx)
                expected = [{**g, 'owner_profile_id': plan['target']['id'], 'epoch': g['epoch'] + 1, 'revision': g['revision'] + 1} for g in ordered]
                if changed != expected or any(self._read_authority(plan['home'], g['thread_id']) != g for g in expected):
                    raise HandoffError('authority_unverified', '원본 실행 소유 변경의 전체 결과를 확인하지 못했습니다.')
                journal['changed_grants'] = changed; journal['status'] = 'authority_committed'; self._write(file, journal)
                journal['target_manifest'] = self._manifest(plan['target'], changed)
                root_grant = next(g for g in changed if g['thread_id'] == plan['root_thread_id'])
                params = _binding(root_grant)
                self._same_identity(plan['target_admin'], plan['target_identity'])
                journal['status'] = 'activation_requested'; self._write(file, journal)
                activation = plan['target_admin'].request('thread/managedReloadBinding', params, timeout=60)
                activated = _ids(activation.get('activatedThreadIds'))
                if (activation.get('bindingReloaded') is not True or set(activated) != set(plan['thread_ids'])
                        or any(activation.get(key) != value for key, value in params.items())):
                    raise HandoffError('activation_unverified', '대상 런타임의 원본 연결 적용 결과를 확인하지 못했습니다.')
                self._same_identity(plan['target_admin'], plan['target_identity'])
                journal['activation'] = activation; journal['status'] = 'complete'; self._write(file, journal)
                return {**self._public(plan), 'transaction_id': tx, 'requires_handoff': False,
                        'owner_profile_id': plan['target']['id'], 'writer_release_verified': True,
                        'binding_reloaded': True, 'message': '같은 원본 대화와 자식을 선택한 계정에 연결했습니다. 다른 작업은 유지했고 새 메시지는 전송하지 않았습니다.'}
        except (OSError, ValueError, RuntimeError, KeyError) as error:
            if journal is not None:
                journal['status'] = 'recovery_required' if mutation_possible else 'blocked_before_close'
                journal['failure_code'] = getattr(error, 'code', 'handoff_failed')
                if hasattr(error, 'changed'):
                    journal['changed_grants'] = deepcopy(error.changed)
                journal['outcome_uncertain'] = mutation_possible
                try:
                    journal['observed_grants'] = [self._read_authority(plan['home'], tid) for tid in plan['thread_ids']]
                    self._write(file, journal)
                except (OSError, ValueError, RuntimeError):
                    pass  # An earlier durable intent still blocks replay after a storage failure.
            return self._failure(error, uncertain=mutation_possible,
                                 transaction_id=journal.get('transaction_id') if journal else None)
