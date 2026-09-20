"""Background, profile-scoped restarts with durable interruption state."""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
import os
import threading
from uuid import uuid4

from .store import identifier, now
from .updates import UpdateError, _lock_file, _unlock_file
from .instances import process_identity
from .update_hooks import _process_liveness

TERMINAL = frozenset({'complete', 'attention', 'superseded'})


def supersede_previous_notice(data, profile):
    """Retire a failed old-launch notice only after a replacement was started."""
    job = data.get('profile_restarts', {}).get(profile['id'])
    if (job and job.get('phase') == 'attention'
            and job.get('generation') != profile.get('generation')):
        job.update(phase='superseded', message='', updated_at=now(),
                   replacement_generation=profile['generation'])


@contextmanager
def _claim(path):
    lock = _lock_file(path)
    try:
        yield
    finally:
        _unlock_file(lock)


def _owner_alive(job):
    return _process_liveness({'pid': job.get('worker_pid'),
                              'created': job.get('worker_created')}) not in ('exited', 'reused')


class ProfileRestarts:
    def __init__(self, store, instances, hooks, *, spawn=None, interval=2, owner_alive=_owner_alive):
        self.store, self.instances, self.hooks = store, instances, hooks
        self.interval = interval
        self.lock = threading.RLock()
        self.workers = set()
        self.spawn = spawn or self._spawn
        self.owner_alive = owner_alive
        self.stopping = threading.Event()

    def shutdown(self):
        self.stopping.set()

    @staticmethod
    def _spawn(target):
        threading.Thread(target=target, name='profile-restart', daemon=True).start()

    def _write(self, profile_id, job_id, **changes):
        def update(data):
            job = data.setdefault('profile_restarts', {}).get(profile_id)
            if job is None or job['id'] != job_id:
                raise RuntimeError('재시작 요청이 변경되었습니다.')
            job.update(changes, updated_at=now())
            return deepcopy(job)
        return self.store.mutate(update)

    def schedule(self, profile_id, *, automatic_key=None, expected_generation=None, retry_failed=False):
        profile_id = identifier(profile_id)
        with self.lock, _claim(self.store.directory / 'restarts' / (profile_id + '.lock')):
            if self.stopping.is_set():
                raise RuntimeError('관리 앱이 종료 중입니다. 다시 연 뒤 적용하세요.')
            profile = self.store.profile(profile_id)
            self.instances.paths(profile)
            if automatic_key and profile.get('generation') != expected_generation:
                return dict(phase='superseded', profile_id=profile_id)
            if profile.get('removed_at') or profile.get('view_only'):
                raise ValueError('실행할 계정 프로필을 선택하세요.')
            old = self.store.read().get('profile_restarts', {}).get(profile_id)
            interrupted_wait = old and old.get('phase') == 'attention' and old.get('code') == 'service_stopped'
            if (automatic_key and old and old.get('automatic_key') == automatic_key
                    and not interrupted_wait
                    and not (retry_failed and old['phase'] == 'attention')
                    and (old['phase'] in TERMINAL or profile_id in self.workers or self.owner_alive(old))):
                return deepcopy(old)
            if profile_id in self.workers or (old and old['phase'] not in TERMINAL and self.owner_alive(old)):
                return deepcopy(old)
            if old and old['phase'] not in TERMINAL:
                # schedule is an explicit apply request. A proven-dead worker
                # does not require a second click; status() alone never resumes it.
                old = {**old, 'phase': 'attention'}
            job = dict(id=str(uuid4()), profile_id=profile_id, phase='waiting',
                       generation=profile.get('generation'),
                       automatic_key=automatic_key,
                       message='이 프로필의 작업이 끝나면 설정을 적용합니다.',
                       created_at=now(), updated_at=now(), worker_pid=os.getpid(),
                       worker_created=(process_identity(os.getpid()) or {}).get('process_created'),
                       requested_revision=profile['policy']['desired_revision'])
            if old and old['phase'] == 'attention' and old.get('transaction_id'):
                # All remote inspection/recovery runs in the worker, not while
                # the UI request holds the scheduling lock.
                job.update(recovery_transaction_id=old['transaction_id'], transaction_id=old['transaction_id'],
                           recovery_revision=old.get('attempt_revision', old.get('recovery_revision', old.get('requested_revision'))),
                           message='이전 설정 적용 결과를 확인하고 필요한 단계부터 계속합니다.')
            self.store.mutate(lambda data: data.setdefault('profile_restarts', {}).update({profile_id: job}))
            self.workers.add(profile_id)
            try:
                self.spawn(lambda: self._run(profile_id, job['id']))
            except Exception:
                self.workers.discard(profile_id)
                self._write(profile_id, job['id'], phase='attention', message='재시작 처리기를 시작하지 못했습니다.')
                raise
            return deepcopy(job)

    def open_local(self, profile_id):
        """Return the local window first; reconcile its SSH hosts independently."""
        profile_id = identifier(profile_id)
        with _claim(self.store.directory / 'restarts' / (profile_id + '.lock')):
            if self.stopping.is_set():
                raise RuntimeError('관리 앱이 종료 중입니다. 다시 연 뒤 적용하세요.')
            old = self.store.read().get('profile_restarts', {}).get(profile_id)
            with self.lock:
                worker_active = profile_id in self.workers
            active = old and (worker_active or (
                old.get('phase') not in TERMINAL and self.owner_alive(old)))
            if active:
                if old.get('remote_background'):
                    profile = self.store.profile(profile_id)
                    observed = self.instances.observe(profile)
                    return dict(state='existing' if observed.get('status') == 'running' else 'updating',
                                profile_id=profile_id, profile={**profile, **observed},
                                ssh_pending=True, restart=deepcopy(old))
                profile = self.store.profile(profile_id)
                return dict(state='updating', profile_id=profile_id,
                            profile={**profile, **self.instances.observe(profile)}, restart=deepcopy(old))
            shown, transaction_id = self.hooks.open_local_for_remote_reconcile(profile_id)
            profile = self.store.profile(profile_id)
            job = dict(id=str(uuid4()), profile_id=profile_id, phase='waiting', remote_background=True,
                       generation=profile.get('generation'), transaction_id=transaction_id,
                       requested_revision=profile['policy']['desired_revision'],
                       message='로컬 창을 열었습니다. SSH 연결은 별도로 준비하고 있습니다.',
                       created_at=now(), updated_at=now(), worker_pid=os.getpid(),
                       worker_created=(process_identity(os.getpid()) or {}).get('process_created'))
            self.store.mutate(lambda data: data.setdefault('profile_restarts', {}).update({profile_id: job}))
            with self.lock:
                self.workers.add(profile_id)
            try:
                self.spawn(lambda: self._run(profile_id, job['id']))
            except Exception:
                with self.lock:
                    self.workers.discard(profile_id)
                self.hooks.remote_open_failed(profile_id, transaction_id, 'worker_start_failed')
                self._write(profile_id, job['id'], phase='attention', code='worker_start_failed',
                            message='로컬 창은 열었습니다. SSH 준비를 다시 시도해 주세요.')
            return {**shown, 'ssh_pending': True, 'restart': deepcopy(job)}

    def status(self):
        jobs = self.store.read().get('profile_restarts', {})
        with self.lock:
            for profile_id, job in jobs.items():
                if job.get('phase') == 'attention' and job.get('code') == 'runtime_state_unavailable':
                    job['message'] = ('이전 버전의 실행 상태를 확인할 수 없습니다. '
                                      '전체 프로필 업데이트에서 해당 구버전을 한 번에 정리할 수 있습니다.')
                if (job['phase'] not in TERMINAL and profile_id not in self.workers
                        and not self.owner_alive(job)):
                    job.update(phase='attention', message='이전 재시작 상태를 확인한 뒤 다시 적용하세요.')
        return jobs

    def _run(self, profile_id, job_id):
        try:
            while not self.stopping.is_set():
                if self.step(profile_id, job_id):
                    return
                self.stopping.wait(self.interval)
            job = self.store.read().get('profile_restarts', {}).get(profile_id, {})
            if job.get('id') == job_id and job.get('remote_background'):
                self.hooks.remote_open_failed(profile_id, job['transaction_id'], 'service_stopped')
            self._write(profile_id, job_id, phase='attention',
                        code='service_stopped',
                        message='관리 서비스 종료로 적용을 대기합니다. 다음 시작 시 자동 확인합니다.')
        finally:
            with self.lock:
                self.workers.discard(profile_id)

    def step(self, profile_id, job_id):
        """Advance once; False means wait without holding any admission lease."""
        lease = None
        phase = 'observing'
        try:
            profile = self.store.profile(profile_id)
            if profile.get('removed_at'):
                raise UpdateError('profile_removed', '제거된 계정의 재시작을 중지했습니다.')
            job = self.store.read()['profile_restarts'][profile_id]
            if job['id'] != job_id:
                return True
            if job.get('remote_background'):
                try:
                    if not self.hooks.reconcile_opened_remotes(profile_id, job['transaction_id']):
                        self._write(profile_id, job_id, phase='waiting',
                                    message='로컬 창은 사용할 수 있습니다. 진행 중인 SSH 작업이 끝나기를 기다립니다.')
                        return False
                    self._write(profile_id, job_id, phase='complete',
                                message='로컬 창과 SSH 연결 준비를 완료했습니다.')
                except (RuntimeError, ValueError, OSError, KeyError) as error:
                    code = getattr(error, 'code', 'remote_prepare_failed')
                    self.hooks.remote_open_failed(profile_id, job['transaction_id'], code)
                    self._write(profile_id, job_id, phase='attention', code=code,
                                message=('로컬 창은 사용할 수 있습니다. ' + str(error)
                                         if isinstance(error, UpdateError) else
                                         '로컬 창은 사용할 수 있습니다. SSH 연결 준비 상태를 확인해 주세요.'))
                return True
            if (job.get('automatic_key') and not job.get('transaction_id')
                    and profile.get('generation') != job.get('generation')):
                self._write(profile_id, job_id, phase='superseded',
                            message='프로필이 이미 새 실행으로 바뀌어 이전 자동 적용을 정리했습니다.')
                return True
            if job.get('connection_transaction_id'):
                if profile['policy']['desired_revision'] == job.get('attempt_revision'):
                    checked = self.hooks.check_restored_connections(profile_id, job['connection_transaction_id'],
                                                                    job['connection_generation'])
                    if checked.get('verified') is True:
                        self._write(profile_id, job_id, connection_transaction_id=None)
                        return self._complete_or_wait(profile_id, job_id)
                    self._write(profile_id, job_id, phase='connecting', message=checked['message'],
                                remote_connections=checked.get('remote_connections'))
                    return False
                self._write(profile_id, job_id, connection_transaction_id=None)
            if job.get('recovery_transaction_id'):
                phase = 'recovering'
                self._write(profile_id, job_id, phase=phase,
                            message='이미 적용된 설정을 유지하며 이전 실행 결과를 확인하고 있습니다.')
                recovered = self.hooks.recover_profile_restart(profile_id, job['recovery_transaction_id'],
                                                               revision=job.get('recovery_revision'))
                if recovered.get('status') == 'restored':
                    if recovered.get('code') == 'remote_account_pending':
                        self._wait_for_connections(profile_id, job_id, recovered, job['recovery_transaction_id'])
                        return False
                    self._write(profile_id, job_id, recovery_transaction_id=None, transaction_id=None)
                    return self._complete_or_wait(profile_id, job_id)
                self._write(profile_id, job_id, recovery_transaction_id=None, transaction_id=None)
            try:
                self.hooks.guard_launch(profile_id)
            except UpdateError as error:
                if error.code != 'update_maintenance':
                    raise
                self._write(profile_id, job_id, phase='waiting',
                            message='이 프로필의 다른 설정 적용 또는 업데이트가 끝나기를 기다립니다.')
                return False
            current = self.instances.observe(profile)
            from .login_health import inspect as login_health
            health = login_health(profile)
            if health['blocks_launch']:
                raise UpdateError('login_required', health['message'])
            snapshot = self.hooks.snapshot_instances(profile_ids=[profile_id])
            if current['status'] == 'running' or any(item.get('remote_maintenance_required') for item in snapshot):
                if len(snapshot) != 1 or not snapshot[0].get('idle_verified'):
                    blocker = snapshot[0].get('update_blocker') if snapshot else None
                    if blocker in ('runtime_identity_not_ready', 'runtime_admin_not_ready', 'runtime_proof_unavailable'):
                        raise UpdateError('runtime_state_unavailable',
                            '이 관리용 Codex의 실행 상태 기록이 불완전하여 종료하지 않았습니다. '
                            '이 프로필에서 하던 작업을 정리한 뒤 ‘이 프로필 다시 열기’를 누르면 '
                            '선택한 관리용 Codex만 종료하고 새 버전을 엽니다. 원래 Codex 앱은 유지됩니다.')
                    message = ('로그인용 창을 닫으면 관리 런타임으로 자동으로 다시 엽니다.'
                               if profile.get('runtime_channel') == 'packaged' else
                               '이 프로필의 작업이 끝나면 자동으로 다시 엽니다.'
                               if blocker == 'runtime_not_idle' else
                               '이 프로필의 로컬·SSH 실행 상태 확인을 기다리고 있습니다.')
                    self._write(profile_id, job_id, phase='waiting', message=message)
                    return False
                phase = 'acquiring'
                transaction_id = str(uuid4())
                opened = current.get('runtime_state', {}).get('opened_task', {}).get('thread_id')
                self._write(profile_id, job_id, phase=phase, transaction_id=transaction_id,
                            restore_thread_id=opened,
                            message='이 프로필의 작업 저장과 종료를 확인하고 있습니다.')
                lease = self.hooks.acquire_maintenance(snapshot, transaction_id=transaction_id,
                                                       profile_scope=[profile_id])
                phase = 'closing'
                self._write(profile_id, job_id, phase=phase,
                            message='이 프로필의 Codex를 정상 종료하고 있습니다.')
                # Explicit policy changes also need to finish a drained Electron
                # process when WM_CLOSE only hides its window in the tray.
                closed = self.hooks.close_instance(snapshot[0], finish_idle_exit=True)
                if not closed:
                    raise UpdateError('normal_exit_pending', 'Codex의 정상 종료 확인이 필요합니다. 강제 종료하지 않았습니다.')
            phase = 'opening'
            self._write(profile_id, job_id, phase=phase,
                        attempt_revision=self.store.profile(profile_id)['policy']['desired_revision'],
                        message='새 설정으로 이 프로필을 다시 열고 있습니다.')
            restore_entry = {'profile_id': profile_id}
            saved = self.store.read()['profile_restarts'][profile_id]
            if saved.get('restore_thread_id'):
                restore_entry['thread_id'] = saved['restore_thread_id']
            restored = self.hooks.restore_instance(restore_entry)
            if restored.get('code') == 'remote_account_pending' and lease is not None:
                self.hooks.release_maintenance(lease)
                transaction_id = lease['transaction_id']
                lease = None
                self._wait_for_connections(profile_id, job_id, restored, transaction_id)
                return False
            if restored.get('verified') is not True:
                raise UpdateError('restart_verification', '창을 다시 열었지만 런타임 연결 확인이 필요합니다.')
            self._write(profile_id, job_id, generation=self.store.profile(profile_id).get('generation'))
            phase = 'releasing'
            if lease is not None:
                self.hooks.release_maintenance(lease)
                lease = None
            return self._complete_or_wait(profile_id, job_id)
        except (RuntimeError, ValueError, OSError, KeyError) as error:
            code = getattr(error, 'code', 'restart_failed')
            if lease is not None and phase != 'releasing':
                try:
                    self.hooks.release_maintenance(lease)
                except (RuntimeError, ValueError, OSError):
                    code = 'restart_recovery_required'
            # Once a close was requested, a lost result must stay visible and
            # require reconciliation. No automatic WM_CLOSE or model replay.
            self._write(profile_id, job_id, phase='attention', code=code,
                        message='프로필 재시작 상태 확인이 필요합니다. ' + str(error))
            return True

    def _complete_or_wait(self, profile_id, job_id):
        current = self.store.profile(profile_id)
        if current['policy'].get('launched_revision') != current['policy']['desired_revision']:
            self._write(profile_id, job_id, phase='waiting',
                        message='시작 중 변경한 최신 설정을 이어서 적용합니다.')
            return False
        self._write(profile_id, job_id, phase='complete',
                    applied_revision=current['policy'].get('launched_revision'),
                    message='설정을 적용해 이 프로필을 다시 열었습니다.')
        return True

    def _wait_for_connections(self, profile_id, job_id, result, transaction_id):
        self._write(profile_id, job_id, phase='connecting', recovery_transaction_id=None,
                    transaction_id=transaction_id, connection_transaction_id=transaction_id,
                    connection_generation=result['generation'],
                    attempt_revision=self.store.profile(profile_id)['policy'].get('launched_revision'),
                    message=result['message'], remote_connections=result['remote_connections'])
