"""Run a user-requested app update without blocking the manager request pipe."""
from copy import deepcopy
import json
import os
import threading
from uuid import uuid4

from .instances import process_identity
from .profile_restart import _owner_alive
from .store import atomic_json, now
from .updates import UpdateError, _lock_file, _unlock_file, _needs_recovery


ACTIVE = frozenset({'queued', 'running', 'connecting'})


class UpdateJobs:
    def __init__(self, manager, *, spawn=None, interval=2, owner_alive=_owner_alive):
        self.manager = manager
        self.path = manager.directory / 'job.json'
        self.spawn = spawn or self._spawn
        self.interval, self.owner_alive = interval, owner_alive
        self.mutex = threading.RLock()
        self.stopping = threading.Event()
        self.last_check = None

    @staticmethod
    def _spawn(target):
        # Graceful backend input closure must not abandon an in-flight install.
        threading.Thread(target=target, name='codex-update', daemon=False).start()

    def shutdown(self):
        self.stopping.set()

    def _read(self):
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding='utf-8'))

    def _write(self, job, **changes):
        job.update(changes, updated_at=now())
        atomic_json(self.path, job)

    def status(self):
        job, transaction = self._read(), self.manager.status()
        if job.get('phase') in ACTIVE:
            if not self.owner_alive(job):
                return dict(status='recovery_required', worker_active=False,
                            message='이전 업데이트 처리기가 종료됐습니다. 업데이트 버튼으로 상태 확인을 계속하세요.')
            if transaction.get('transaction_id') and transaction.get('transaction_id') != job.get('previous_transaction_id'):
                result = transaction
            else:
                result = job.get('result') or dict(status='queued', message='업데이트 준비 상태를 확인하고 있습니다.')
            return {**deepcopy(result), 'worker_active': True, 'worker_phase': job['phase'], 'job_id': job['id']}
        if self.last_check and not _needs_recovery(transaction):
            result = self.last_check
        else:
            result = job.get('result') or transaction
        # Older versions persisted "nothing to install" as plan_not_ready.
        # Keep real blockers visible, but do not replay that false alarm forever.
        blockers = result.get('blockers', [])
        if (result.get('status') == 'blocked' and len(blockers) == 1
                and blockers[0].get('code') in ('installed_newer', 'up_to_date')):
            result = dict(status=blockers[0]['code'], message=blockers[0]['message'])
        return {**deepcopy(result), 'worker_active': False}

    def check(self):
        status = self.status()
        if status.get('worker_active') or _needs_recovery(self.manager.status()) or status.get('status') == 'recovery_required':
            return status
        self.last_check = self.manager.check()
        return deepcopy(self.last_check)

    def schedule(self):
        with self.mutex:
            if (self.last_check and self.last_check.get('status') in ('installed_newer', 'up_to_date')
                    and not _needs_recovery(self.manager.status())):
                return {**deepcopy(self.last_check), 'worker_active': False}
            try:
                claim = _lock_file(self.manager.directory / 'worker.lock')
            except UpdateError:
                return {**self.status(), 'worker_active': True}
            try:
                previous = self.manager.status()
                identity = process_identity(os.getpid()) or {}
                job = dict(id=str(uuid4()), phase='queued', worker_pid=os.getpid(),
                           worker_created=identity.get('process_created'), previous_transaction_id=previous.get('transaction_id'),
                           action='recover' if _needs_recovery(previous) else 'apply')
                self.last_check = None
                self._write(job)
                try:
                    self.spawn(lambda: self._run(job, claim))
                except Exception:
                    self._write(job, phase='attention', result=dict(status='recovery_required',
                        message='업데이트 처리기를 시작하지 못했습니다. 업데이트 버튼으로 다시 시도할 수 있습니다.'))
                    raise
            except Exception:
                _unlock_file(claim)
                raise
            return dict(status='queued', worker_active=True, worker_phase='queued', job_id=job['id'],
                        message='업데이트 상태 확인을 백그라운드에서 계속합니다.' if job['action'] == 'recover' else
                                '모든 관리 프로필에 적용할 Codex 앱 업데이트를 준비합니다.')

    def _run(self, job, claim):
        try:
            self._write(job, phase='running')
            if job['action'] == 'recover':
                result = self.manager.recover()
            else:
                instances = self.manager.snapshot_instances() if self.manager.snapshot_instances else []
                result = self.manager.apply(self.manager.plan(instances))
            while result.get('status') == 'connecting':
                self._write(job, phase='connecting', result=result)
                if self.stopping.wait(self.interval):
                    self._write(job, phase='attention', result=dict(status='recovery_required',
                        message='관리 서비스 종료로 연결 확인을 멈췄습니다. 업데이트 버튼으로 계속할 수 있습니다.'))
                    return
                result = self.manager.poll_restore()
            self._write(job, phase='finished', result=result)
        except Exception as error:
            self._write(job, phase='attention', result=dict(status='recovery_required',
                code=getattr(error, 'code', 'update_worker_failed'),
                error_type=type(error).__name__,
                message='업데이트 상태 확인이 필요합니다. 업데이트 버튼으로 다시 확인할 수 있습니다.'))
        finally:
            _unlock_file(claim)
