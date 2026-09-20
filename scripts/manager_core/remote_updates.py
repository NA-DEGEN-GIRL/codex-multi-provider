"""Persistent SSH update queues. Status is local; network work is asynchronous.

Managed updates own one profile's recorded SSH cohort and never own its desktop.
Stock updates are a separate, explicitly confirmed action and are never replayed
by the scheduler. Lost managed lifecycle replies retain their original journal.
"""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import threading
import time
from uuid import uuid4

from .store import identifier, now
from .updates import UpdateError, _lock_file, _unlock_file


ACTIVE = frozenset({'queued', 'waiting', 'applying', 'recovering'})
STOCK_ACTIVE = frozenset({'dispatching', 'pending', 'queued', 'starting', 'applying', 'running', 'unknown'})


def _version(bundle):
    if not isinstance(bundle, str):
        return None
    return re.sub(r'-[0-9a-f]{16}$', '', bundle)


def _stock_pending(value):
    job = value.get('update_job') or {}
    return job.get('state') in STOCK_ACTIVE or job.get('verification_pending') is True


class RemoteUpdates:
    def __init__(self, root, store, remote, hooks, *, probe=None, stock_update=None,
                 spawn=None, interval=15, check_interval=3600, clock=time.time):
        self.root, self.store, self.remote, self.hooks = Path(root), store, remote, hooks
        self.probe, self.stock_update = probe, stock_update
        # Keep startup SSH discovery bounded even with many profiles/hosts.
        # Submitted checks remain visible as checking while waiting in the queue.
        self.executor = None if spawn else ThreadPoolExecutor(max_workers=3, thread_name_prefix='ssh-update')
        self.spawn = spawn or self.executor.submit
        self.interval, self.check_interval, self.clock = interval, check_interval, clock
        self.mutex = threading.RLock()
        self.busy = set()
        self.stopping, self.wake = threading.Event(), threading.Event()
        self.started = False

    def _key(self, profile_id, alias):
        profile_id = identifier(profile_id)
        if not isinstance(alias, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', alias):
            raise ValueError('등록된 SSH 호스트를 선택해 주세요.')
        profile = self.store.profile(profile_id)
        if profile.get('removed_at') or profile.get('view_only'):
            raise ValueError('사용 가능한 관리 프로필을 선택해 주세요.')
        return profile_id + ':' + alias

    @staticmethod
    def _default(profile_id, alias):
        return dict(profile_id=profile_id, alias=alias, auto_check=True, auto_apply=False,
                    checking=False, checked_at=None, job=None,
                    managed=dict(active_version=None, prepared_version=None, available_version=None,
                                 state='unknown', message='SSH 버전을 확인하면 실행 중인 관리 런타임을 표시합니다.'),
                    stock=dict(cli_version=None, daemon_version=None, daemon_state='unknown',
                               state='unknown', message='기존 Codex 버전을 아직 확인하지 않았습니다.',
                               update_supported=False, safe_auto_update=False))

    def _read(self, profile_id, alias):
        key = profile_id + ':' + alias
        return deepcopy(self.store.read().get('remote_updates', {}).get(key) or self._default(profile_id, alias))

    def _save(self, profile_id, alias, **changes):
        def save(data):
            value = data.setdefault('remote_updates', {}).setdefault(
                profile_id + ':' + alias, self._default(profile_id, alias))
            value.update(deepcopy(changes))
            return deepcopy(value)
        return self.store.mutate(save)

    def status(self, profile_id, alias):
        self._key(profile_id, alias)
        value = self._read(profile_id, alias)
        data = self.store.read()
        value['managed']['can_schedule'] = alias in data.get('ssh_inventory', {}).get(
            profile_id, {}).get('hosts', [])
        shared = next((v for v in data.get('remote_updates', {}).values()
                       if v.get('profile_id') == profile_id and (v.get('job') or {}).get('state') in ACTIVE), None)
        if shared:
            value['job'] = {**deepcopy(shared['job']), 'alias': shared['alias']}
        # Internal generation/target pins never become UI claims of activity.
        return {key: deepcopy(item) for key, item in value.items() if not key.startswith('_')}

    def status_all(self):
        values = self.store.read().get('remote_updates', {}).values()
        items = [{key: deepcopy(item) for key, item in value.items() if not key.startswith('_')}
                 for value in values]
        return dict(worker_active=any(v.get('checking') or (v.get('job') or {}).get('state') in ACTIVE
                                      or _stock_pending(v.get('stock', {})) for v in items), items=items)

    def settings(self, profile_id, alias, *, auto_check=None, auto_apply=None):
        self._key(profile_id, alias)
        changes = {}
        for key, value in (('auto_check', auto_check), ('auto_apply', auto_apply)):
            if value is not None:
                if type(value) is not bool:
                    raise ValueError(key + ' 설정은 켜기 또는 끄기로 지정해 주세요.')
                changes[key] = value
        self._save(profile_id, alias, **changes)
        self.wake.set()
        return self.status(profile_id, alias)

    def _launch(self, profile_id, alias, fn, *, checking=False):
        key = self._key(profile_id, alias)
        with self.mutex:
            if key in self.busy or self.stopping.is_set():
                return False
            try:
                claim = _lock_file(self.store.directory / 'remote-updates' / profile_id / (alias + '.lock'))
            except UpdateError:
                return False
            self.busy.add(key)
            if checking:
                self._save(profile_id, alias, checking=True)
        def run():
            try:
                if not self.stopping.is_set():
                    fn()
            except Exception as error:
                self._save(profile_id, alias, error=getattr(error, 'code', 'ssh_observation_unavailable'))
            finally:
                if checking:
                    self._save(profile_id, alias, checking=False)
                with self.mutex:
                    self.busy.discard(key)
                _unlock_file(claim)
        try:
            self.spawn(run)
        except Exception:
            with self.mutex:
                self.busy.discard(key)
            if checking:
                self._save(profile_id, alias, checking=False)
            _unlock_file(claim)
            raise
        return True

    def check(self, profile_id, alias):
        self._launch(profile_id, alias, lambda: self._check(profile_id, alias), checking=True)
        return self.status(profile_id, alias)

    def _available(self, platform, architecture):
        if platform != 'linux' or architecture not in ('x86_64', 'aarch64'):
            raise UpdateError('remote_artifact_missing', '지원되는 Linux 관리 런타임 묶음이 필요합니다.')
        path = self.root / 'artifacts/remote' / (platform + '-' + architecture) / 'manifest.json'
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 256000:
            raise UpdateError('remote_artifact_missing', 'Linux 관리 런타임 묶음을 확인할 수 없습니다.')
        manifest = json.loads(path.read_text(encoding='utf-8-sig'))
        version = manifest.get('version')
        if not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}', version):
            raise UpdateError('invalid_artifact', 'Linux 관리 런타임 버전을 확인할 수 없습니다.')
        digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:16]
        return dict(version=version, bundle_id=version + '-' + digest)

    def _check(self, profile_id, alias):
        self.remote._alias(alias)
        profile = self.store.profile(profile_id)
        generation = profile.get('generation')
        source = next((b for b in profile.get('remote_bindings', []) if b.get('alias') == alias), None)
        stock = self._read(profile_id, alias)['stock']
        error = None
        try:
            if self.probe is None:
                from .ssh_versions import probe
            else:
                probe = self.probe
            stock = probe(self.root, self.remote, alias)
            stock['safe_auto_update'] = False
        except Exception as exception:
            stock = {**stock, 'state': 'unavailable', 'message': '기존 Codex 버전을 확인할 수 없습니다.',
                     'safe_auto_update': False}
            error = getattr(exception, 'code', 'stock_observation_unavailable')
        managed = dict(active_version=None, prepared_version=_version((source or {}).get('runtime_bundle')),
                       available_version=None, state='unknown', message='관리 런타임 실행 상태를 확인할 수 없습니다.')
        target = None
        try:
            observed = stock
            artifact = self._available(observed['platform'], observed['architecture'])
            target = dict(platform=observed['platform'], architecture=observed['architecture'],
                          bundle=artifact['bundle_id'], version=artifact['version'],
                          host_identity=observed['managed_host_identity'])
            if source and source.get('host_identity') != target['host_identity']:
                raise UpdateError('ssh_host_changed', '저장된 SSH 호스트 식별 정보가 변경되었습니다.')
            managed.update(available_version=artifact['version'], available_bundle=artifact['bundle_id'],
                           prepared_bundle=(source or {}).get('runtime_bundle'))
            if source and source.get('prepared') is True:
                actual = self.hooks.remote_maintenance.request(source, 'inspect', discover_active=True)
                bundle = actual.get('runtime_bundle')
                if actual.get('host_identity') and actual['host_identity'] != target['host_identity']:
                    raise UpdateError('ssh_host_changed', '실행 중인 SSH 호스트 식별 정보가 변경되었습니다.')
                managed.update(active_version=_version(bundle), active_bundle=bundle,
                               active_revision=(actual.get('process') or {}).get('revision'),
                               idle=actual['idle'], running=actual['process'] is not None)
                if actual['process'] is None:
                    state, message = ('stopped', '관리 SSH 런타임이 실행 중이지 않습니다. 준비된 파일은 별도로 표시합니다.')
                elif bundle is None:
                    state, message = ('unknown', '실행 중인 관리 런타임의 버전을 확인할 수 없습니다.')
                elif bundle == artifact['bundle_id']:
                    state, message = ('current', '관리 SSH 런타임이 현재 제공되는 버전으로 실행 중입니다.')
                else:
                    state, message = ('update_available', '이 프로필에 적용할 관리 SSH 업데이트가 있습니다.')
            else:
                state, message = ('not_prepared', '이 프로필을 SSH에 연결한 뒤 관리 업데이트를 예약해 주세요.')
            managed.update(state=state, message=message)
        except Exception as exception:
            error = getattr(exception, 'code', 'managed_observation_unavailable')
            if error == 'ssh_host_changed':
                target = None
                managed.update(state='attention', message='SSH 호스트 식별 정보가 변경되었습니다. 연결 대상을 확인해 주세요.')
        current = self.store.profile(profile_id)
        if current.get('generation') != generation or next((b for b in current.get('remote_bindings', [])
                if b.get('alias') == alias), None) != source:
            managed.update(state='unknown', message='확인 중 프로필 연결 설정이 변경되었습니다. 다시 확인해 주세요.')
            target = None
        self._save(profile_id, alias, stock=stock, managed=managed, checked_at=now(),
                   _checked_epoch=self.clock(), _target=target, error=error)
        value = self._read(profile_id, alias)
        if (value['auto_apply'] and managed['state'] == 'update_available'
                and (value.get('job') or {}).get('state') in (None, 'complete', 'cancelled')):
            self.schedule(profile_id, alias)

    def schedule(self, profile_id, alias):
        self._key(profile_id, alias)
        with self.mutex, self.store.locked():
            data = self.store.read()
            profile = self.store.profile(profile_id, data)
            if alias not in data.get('ssh_inventory', {}).get(profile_id, {}).get('hosts', []):
                raise UpdateError('ssh_not_enrolled', '이 프로필을 SSH 호스트에 연결한 뒤 업데이트를 예약해 주세요.')
            # A profile's aliases share a daemon cohort and one maintenance gate.
            for value in data.get('remote_updates', {}).values():
                if value.get('profile_id') == profile_id and (value.get('job') or {}).get('state') in ACTIVE:
                    return self.status(profile_id, alias)
            gate = data.get('ssh_maintenance', {}).get(profile_id, {})
            if gate.get('remote_update') and gate.get('state') not in (None, 'released'):
                raise UpdateError('ssh_update_attention', '이전 SSH 업데이트 결과를 확인한 뒤 새 업데이트를 예약해 주세요.')
            job = dict(id=str(uuid4()), state='queued', scope='profile',
                       message='이 프로필에 연결된 SSH 호스트의 작업이 끝나면 관리 런타임을 업데이트합니다.')
            self._save(profile_id, alias, job=job, _job=dict(transaction_id=str(uuid4()),
                       generation=profile.get('generation'), revision=profile['policy']['desired_revision'],
                       targets=None, cancel_requested=False))
        self._launch(profile_id, alias, lambda: self.step(profile_id, alias))
        self.wake.set()
        return self.status(profile_id, alias)

    def _job_state(self, profile_id, alias, state, message, **extras):
        value = self._read(profile_id, alias)
        self._save(profile_id, alias, job={**value['job'], 'state': state, 'message': message, **extras})

    def _update_pin(self, profile_id, alias, transaction_id, **changes):
        def update(data):
            value = data['remote_updates'][profile_id + ':' + alias]
            pin = value['_job']
            if pin.get('transaction_id') != transaction_id:
                raise UpdateError('ssh_generation_changed', 'SSH 업데이트 예약이 변경되었습니다.')
            pin.update(deepcopy(changes))
            return deepcopy(pin)
        return self.store.mutate(update)

    def _lease(self, transaction_id):
        path = self.hooks._lease_path(transaction_id)
        return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else None

    def cancel(self, profile_id, alias):
        self._key(profile_id, alias)
        requested_alias = alias
        with self.mutex:
            shared = next((v for v in self.store.read().get('remote_updates', {}).values()
                           if v.get('profile_id') == profile_id and (v.get('job') or {}).get('state') in ACTIVE), None)
            if shared:
                alias = shared['alias']
            value = self._read(profile_id, alias)
            if (value.get('job') or {}).get('state') not in ACTIVE:
                return self.status(profile_id, requested_alias)
            self._update_pin(profile_id, alias, value['_job']['transaction_id'], cancel_requested=True)
        # Cancellation must own the same cross-process worker claim as apply.
        # The process-local busy set cannot prove another controller is idle.
        self._launch(profile_id, alias, lambda: self.step(profile_id, alias))
        self.wake.set()
        return self.status(profile_id, requested_alias)

    def _cancel(self, profile_id, alias):
        value = self._read(profile_id, alias)
        transaction = value['_job']['transaction_id']
        lease = self._lease(transaction)
        records = (lease or {}).get('profiles', [{}])[0].get('remotes', [])
        if any(r.get('state') not in ('unobserved', 'observed', 'closed') or r.get('exit_proof') for r in records):
            self._update_pin(profile_id, alias, transaction, cancel_requested=False)
            self._job_state(profile_id, alias, 'recovering',
                            'SSH 종료 또는 시작 요청이 이미 진행되었습니다. 결과를 확인한 뒤 연결 제한을 해제합니다.')
            return False
        if lease:
            def release(data):
                gate = data.get('ssh_maintenance', {}).get(profile_id)
                if gate and gate.get('transaction_id') == transaction:
                    gate.update(state='released', updated_at=now())
            self.store.mutate(release)
            lease['state'] = 'released'
            lease['cancelled'] = True
            self.hooks._save_lease(lease)
        self._job_state(profile_id, alias, 'cancelled', '런타임을 변경하기 전에 관리 SSH 업데이트를 취소했습니다.')
        return True

    def step(self, profile_id, alias):
        """One recoverable queue pass; callers serialize it with the worker claim."""
        value = self._read(profile_id, alias)
        if (value.get('job') or {}).get('state') not in ACTIVE:
            return
        pin = value['_job']
        transaction = pin['transaction_id']
        try:
            if pin.get('cancel_requested'):
                self._cancel(profile_id, alias)
                return
            profile = self.store.profile(profile_id)
            if (profile.get('generation') != pin['generation']
                    or profile['policy']['desired_revision'] != pin['revision']):
                raise UpdateError('ssh_generation_changed', '프로필 실행 세대 또는 설정이 변경되었습니다.')
            gate = self.store.read().get('ssh_maintenance', {}).get(profile_id, {})
            if gate.get('transaction_id') != transaction:
                self.hooks.begin_remote_reconcile(profile_id, ensure_local=False,
                    force_runtime_update=True, transaction_id=transaction)
            lease = self._lease(transaction)
            if lease['state'] == 'released':
                self._job_state(profile_id, alias, 'complete', '관리 SSH 업데이트를 완료했습니다. 로컬 작업은 유지했습니다.')
                return
            if not pin.get('targets'):
                targets = {}
                for record in lease['profiles'][0]['remotes']:
                    self._check(profile_id, record['alias'])
                    target = self._read(profile_id, record['alias']).get('_target')
                    if not target:
                        raise UpdateError('ssh_target_unknown', '아직 SSH 업데이트 대상을 확인할 수 없습니다.')
                    targets[record['alias']] = target
                pin = self._update_pin(profile_id, alias, transaction, targets=targets)
            def guard():
                latest = self._read(profile_id, alias)
                if latest['_job'].get('cancel_requested'):
                    raise UpdateError('ssh_update_cancelled', '업데이트 취소가 요청되었습니다.')
                for host, target in pin['targets'].items():
                    artifact = self._available(target['platform'], target['architecture'])
                    if artifact['bundle_id'] != target['bundle']:
                        raise UpdateError('ssh_target_changed', '제공되는 관리 런타임 묶음이 변경되었습니다.')
                    source = next((b for b in self.store.profile(profile_id).get('remote_bindings', [])
                                   if b.get('alias') == host), None)
                    if not source or source.get('host_identity') != target['host_identity']:
                        raise UpdateError('ssh_host_changed', '저장된 SSH 호스트 식별 정보가 변경되었습니다.')
                    journal = self._lease(transaction)
                    record = next(r for r in journal['profiles'][0]['remotes'] if r['alias'] == host)
                    if record.get('next_binding') and source.get('runtime_bundle') != target['bundle']:
                        raise UpdateError('ssh_target_changed', '준비된 관리 런타임이 예약한 버전과 다릅니다.')
            self._job_state(profile_id, alias, 'applying', 'SSH 작업 종료를 확인하고 관리 런타임을 적용하고 있습니다.')
            complete = self.hooks.reconcile_opened_remotes(profile_id, transaction, target_guard=guard)
            if complete:
                self._job_state(profile_id, alias, 'complete', '관리 SSH 업데이트를 완료했습니다. 로컬 작업은 유지했습니다.')
                for host in pin['targets']:
                    self._check(profile_id, host)
            else:
                self._job_state(profile_id, alias, 'waiting', 'SSH 작업 종료가 확인될 때까지 기다립니다. 로컬 작업은 계속할 수 있습니다.')
        except Exception as error:
            code = getattr(error, 'code', 'ssh_observation_unavailable')
            if code == 'ssh_update_cancelled':
                self._cancel(profile_id, alias)
                return
            fatal = code in {'ssh_generation_changed', 'ssh_target_changed', 'ssh_host_changed',
                            'remote_binding_changed', 'remote_configuration_changed',
                            'remote_runtime_exited', 'remote_start_timeout'}
            if fatal:
                self.hooks.remote_open_failed(profile_id, transaction, code)
            self._job_state(profile_id, alias, 'attention' if fatal else 'waiting',
                ('SSH 업데이트 결과 확인이 필요합니다. 이전 버전의 연결 정보와 적용 기록을 보존했습니다.'
                 if fatal else 'SSH 실행 상태를 확인할 때까지 기다립니다. 결과가 불명확한 종료·시작 요청은 반복하지 않습니다.'), code=code)

    def update_stock(self, profile_id, alias, *, confirmed=False, observation_id=None):
        self._key(profile_id, alias)
        if confirmed is not True:
            raise ValueError('기존 Codex 업데이트로 연결이 중단될 수 있음을 먼저 확인해 주세요.')
        value = self._read(profile_id, alias)
        stock = value['stock']
        # The caller must send the observation displayed when the user confirmed.
        # A newer cached probe must never silently substitute another target.
        token = observation_id
        if not isinstance(token, str) or not token or token != stock.get('observation_id') or not stock.get('update_supported'):
            raise ValueError('SSH 버전을 확인한 뒤 지원되는 기존 Codex 업데이트를 실행해 주세요.')
        if _stock_pending(stock):
            return self.status(profile_id, alias)
        def update():
            self._save(profile_id, alias, stock={**stock, 'update_job': dict(state='dispatching',
                       message='확인한 기존 Codex 업데이트를 시작하고 있습니다.')})
            try:
                if self.stock_update is None:
                    from .ssh_versions import update as perform
                else:
                    perform = self.stock_update
                result = perform(self.root, self.remote, alias, token, confirmed=True)
                self._save(profile_id, alias, stock={**result, 'safe_auto_update': False})
            except Exception as error:
                self._save(profile_id, alias, stock={**stock, 'safe_auto_update': False,
                    'update_job': dict(state='unknown', message='업데이트 결과가 불명확합니다. 요청을 반복하지 않고 상태를 확인합니다.')},
                    error=getattr(error, 'code', 'stock_update_unverified'))
            self.wake.set()
        if not self._launch(profile_id, alias, update, checking=True):
            raise UpdateError('ssh_update_busy', '이 SSH 호스트의 확인 또는 업데이트가 진행 중입니다. 완료 후 다시 시도해 주세요.')
        return self.status(profile_id, alias)

    def start(self):
        with self.mutex:
            if self.started:
                return
            self.started = True
        # A process restart only resumes managed journals and read-only probes.
        # It never resubmits the explicit stock update operation.
        for value in self.store.read().get('remote_updates', {}).values():
            self._save(value['profile_id'], value['alias'], checking=False)
        threading.Thread(target=self._loop, name='ssh-update-scheduler', daemon=True).start()

    def tick(self):
        data = self.store.read()
        pairs = {(p['id'], b['alias']) for p in data['profiles'] if not p.get('removed_at') and not p.get('view_only')
                 for b in p.get('remote_bindings', []) if b.get('alias')}
        pairs.update((v['profile_id'], v['alias']) for v in data.get('remote_updates', {}).values())
        for profile_id, alias in sorted(pairs):
            try:
                value = self._read(profile_id, alias)
                profile = next((p for p in data['profiles'] if p['id'] == profile_id), None)
                if not profile or profile.get('removed_at') or profile.get('view_only'):
                    if (value.get('job') or {}).get('state') in ACTIVE:
                        self._job_state(profile_id, alias, 'attention', '프로필이 변경되어 SSH 업데이트를 중단했습니다.',
                                        code='profile_unavailable')
                    continue
                if (value.get('job') or {}).get('state') in ACTIVE:
                    self._launch(profile_id, alias, lambda p=profile_id, a=alias: self._queued_pass(p, a))
                elif (_stock_pending(value['stock']) or (value['auto_check'] and
                      self.clock() - value.get('_checked_epoch', 0) >= self.check_interval)):
                    self.check(profile_id, alias)
            except (ValueError, RuntimeError, OSError, KeyError):
                continue

    def _queued_pass(self, profile_id, alias):
        # A busy managed cohort must not starve a separately confirmed stock
        # install's read-only verification, including after service restart.
        if _stock_pending(self._read(profile_id, alias)['stock']):
            self._check(profile_id, alias)
        self.step(profile_id, alias)

    def _loop(self):
        while not self.stopping.is_set():
            try:
                self.tick()
            except (ValueError, RuntimeError, OSError, KeyError):
                pass
            self.wake.wait(self.interval)
            self.wake.clear()

    def shutdown(self):
        self.stopping.set()
        self.wake.set()
        if self.executor:
            self.executor.shutdown(wait=False)
