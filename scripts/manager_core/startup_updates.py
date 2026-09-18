"""Check installed versions when a manager window opens, without blocking UI."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import threading

from .instances import process_identity
from .runtime_build import resolve
from .runtime_selection import describe
from .store import identifier
from .profile_recovery import stop_profile


def selected_manager_proxy(root):
    root = Path(root).resolve()
    pointer = root / 'artifacts/manager/current.json'
    if not pointer.is_file():
        return None
    release = json.loads(pointer.read_text(encoding='utf-8-sig'))
    proxy = Path(release['runtime_proxy']).resolve()
    if not proxy.is_relative_to(root / 'artifacts/manager') or not proxy.is_file():
        raise RuntimeError('관리 앱의 업데이트 경로를 확인하지 못했습니다.')
    return str(proxy)


class StartupUpdates:
    def __init__(self, root, store, instances, restarts, *, spawn=None,
                 runtime_resolver=resolve, describe_runtime=describe, identity=process_identity,
                 manager_resolver=selected_manager_proxy, stopper=stop_profile):
        self.root, self.store, self.instances, self.restarts = root, store, instances, restarts
        self.resolve, self.describe, self.identity = runtime_resolver, describe_runtime, identity
        self.manager_resolver = manager_resolver
        self.stopper = stopper
        self.spawn = spawn or (lambda fn: threading.Thread(target=fn, name='startup-updates', daemon=True).start())
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.result = dict(state='not_started', worker_active=False, profiles=[])

    def shutdown(self):
        self.stopping.set()

    def status(self):
        with self.lock:
            value = deepcopy(self.result)
        jobs = self.restarts.status() if hasattr(self.restarts, 'status') else {}
        profiles = {p['id']: p for p in self.store.read()['profiles']}
        counts = dict(current=0, pending=0, attention=0)
        for entry in value['profiles']:
            profile = profiles.get(entry['profile_id'], {})
            entry['alias'] = profile.get('alias', entry['profile_id'])
            job = jobs.get(entry['profile_id'], {})
            if entry.get('job_id') and entry.get('job_id') == job.get('id'):
                entry.update(state=job['phase'], message=job.get('message', ''), code=job.get('code'))
            state = entry['state']
            counts['current' if state in ('current', 'complete', 'latest_on_open') else
                   'attention' if state in ('attention', 'unknown', 'login_pending', 'superseded') else 'pending'] += 1
        value.update(counts=counts, total=len(value['profiles']))
        if not value['worker_active'] and value['profiles'] and value['state'] != 'stopped':
            value['state'] = 'attention' if counts['attention'] else 'applying' if counts['pending'] else 'complete'
            value['message'] = (f"전체 프로필 {value['total']}개 · 최신/다음 실행 준비 {counts['current']}개"
                                f" · 적용 대기 {counts['pending']}개 · 확인 필요 {counts['attention']}개")
        return value

    def recover_legacy(self, candidates, *, interrupt_running_work=False):
        if interrupt_running_work is not True:
            raise ValueError('구버전 프로필 종료 확인이 필요합니다.')
        if not isinstance(candidates, list) or not 1 <= len(candidates) <= 64:
            raise ValueError('정리할 구버전 프로필 목록이 올바르지 않습니다.')
        pinned = []
        data = self.store.read()
        for item in candidates:
            if not isinstance(item, dict):
                raise ValueError('정리할 프로필 정보가 올바르지 않습니다.')
            pid, generation, job_id = (identifier(item[key]) for key in ('profile_id', 'generation', 'job_id'))
            profile = self.store.profile(pid, data)
            job = data.get('profile_restarts', {}).get(pid, {})
            if (profile.get('generation') != generation or job.get('id') != job_id or
                job.get('phase') != 'attention' or job.get('code') != 'runtime_state_unavailable' or
                profile.get('removed_at') or profile.get('view_only') or any(p['profile_id'] == pid for p in pinned)):
                raise ValueError('구버전 상태가 변경되었습니다. 전체 프로필 상태를 다시 확인하세요.')
            pinned.append(dict(profile_id=pid, generation=generation, job_id=job_id))
        with self.lock:
            if self.stopping.is_set() or self.result['worker_active']:
                raise RuntimeError('전체 프로필 업데이트가 진행 중입니다.')
            self.result = dict(state='recovering', worker_active=True,
                profiles=[dict(profile_id=p['profile_id'], state='recovering') for p in pinned],
                message='확인한 구버전 프로필을 정리하고 최신 버전으로 엽니다.')
            try:
                self.spawn(lambda: self._recover_legacy(pinned))
            except Exception:
                self.result.update(state='attention', worker_active=False)
                raise
            return deepcopy(self.result)

    def _recover_legacy(self, candidates):
        failures = []
        for item in candidates:
            if self.stopping.is_set(): break
            try:
                from .login_health import require as require_login
                require_login(self.store.profile(item['profile_id']))
                # stop_profile rechecks the pinned generation, executable, birth
                # identity and owned user-data folder immediately before exit.
                self.stopper(self.store, self.instances, item['profile_id'],
                    expected_generation=item['generation'], interrupt_running_work=True)
                self.instances.show(item['profile_id'], reopen_existing=False)
            except (OSError, RuntimeError, ValueError, KeyError):
                failures.append(item['profile_id'])
        self._run(retry_failed=False)
        if failures:
            with self.lock:
                for result in self.result['profiles']:
                    if result['profile_id'] in failures and result['state'] != 'login_pending':
                        result.update(state='attention', message='구버전 종료/재실행 상태를 확인하지 못했습니다.')

    def start(self, *, retry_failed=False):
        with self.lock:
            if self.stopping.is_set():
                raise RuntimeError('관리 서비스가 종료 중입니다.')
            if self.result['worker_active']:
                return deepcopy(self.result)
            self.result = dict(state='checking', worker_active=True, profiles=[],
                               message='실행 중인 프로필의 업데이트를 자동 확인합니다.')
            try:
                self.spawn(lambda: self._run(retry_failed=retry_failed))
            except Exception:
                self.result.update(state='attention', worker_active=False)
                raise
            return deepcopy(self.result)

    def _run(self, *, retry_failed=False):
        results = []
        state = 'complete'
        try:
            selected = self.resolve(self.root)
            manager_proxy = self.manager_resolver(self.root)
            from .release_code import runtime_revision
            selected_revision = runtime_revision(manager_proxy)
            data = self.store.read()
            for profile in data['profiles']:
                if self.stopping.is_set():
                    state = 'stopped'
                    break
                if profile.get('removed_at') or profile.get('view_only'):
                    continue
                if profile.get('runtime_channel') == 'packaged':
                    results.append(dict(profile_id=profile['id'], state='login_pending', message='로그인을 마치면 관리 런타임을 적용합니다.'))
                    continue
                try:
                    previous = data.get('profile_restarts', {}).get(profile['id'], {})
                    unfinished = previous.get('automatic_key') and (
                        previous.get('phase') not in ('complete', 'attention', 'superseded') or
                        previous.get('code') == 'service_stopped')
                    observed = {**profile, **self.instances.observe(profile)}
                    from .login_health import inspect as login_health
                    health = login_health(profile)
                    if health['blocks_launch']:
                        results.append(dict(profile_id=profile['id'], state='login_pending',
                                            message=health['message']))
                        continue
                    if observed.get('status') != 'running' and not unfinished:
                        # Do not open unused accounts. Their normal launch already
                        # resolves the installed runtime pointer afresh.
                        results.append(dict(profile_id=profile['id'], state='latest_on_open'))
                        continue
                    version = self.describe(self.root, observed, identity=self.identity, selected=selected)
                    policy = profile['policy']
                    pending_policy = policy.get('launched_revision') != policy.get('desired_revision')
                    pending_manager = bool(manager_proxy and (
                        not profile.get('manager_release') or
                        Path(profile['manager_release']) != Path(manager_proxy)))
                    if selected_revision:
                        pending_manager = profile.get('manager_runtime_revision') != selected_revision
                    if not version.get('restart_required') and not pending_policy and not pending_manager and not unfinished:
                        results.append(dict(profile_id=profile['id'], state=version.get('state', 'unknown')))
                        continue
                    key = hashlib.sha256(json.dumps(dict(generation=profile.get('generation'),
                        runtime=selected['runtime'], digest=selected.get('sha256'),
                        manager_proxy=selected_revision or manager_proxy,
                        capabilities=selected.get('capabilities'), revision=policy['desired_revision']),
                        sort_keys=True).encode()).hexdigest()
                    job = self.restarts.schedule(profile['id'], automatic_key=key,
                                                 expected_generation=profile.get('generation'), retry_failed=retry_failed)
                    results.append(dict(profile_id=profile['id'], state=job['phase'], job_id=job.get('id')))
                except (OSError, ValueError, RuntimeError, KeyError):
                    results.append(dict(profile_id=profile['id'], state='attention'))
        except (OSError, ValueError, RuntimeError, KeyError):
            state = 'attention'
        finally:
            with self.lock:
                self.result = dict(state=state, worker_active=False, profiles=results,
                    message='시작 시 업데이트 확인을 마쳤습니다. 필요한 프로필은 작업 종료 후 자동 적용합니다.'
                            if state == 'complete' else '시작 시 업데이트 상태 확인이 필요합니다.')
