"""Bounded, background quota refresh. Never starts a task or changes a login."""
from copy import deepcopy
from pathlib import Path
import threading
import time

from .native_usage import newer, observed_timestamp


def _binding(profile):
    return tuple(profile.get(key) for key in
                 ('auth_mode', 'home', 'source_home', 'account_fingerprint', 'usage_account_id'))


def read_quota(root, profile):
    from .proxy_auth import read_existing_tokens, account_fingerprint
    from .login_probe import verify, verification_runtime
    from desktop_launch import find_app
    home = Path(profile['home'] if profile.get('auth_mode') == 'native' else
                profile.get('source_home') or profile['home'])
    expected = profile.get('account_fingerprint')
    tokens = read_existing_tokens(home)
    if not expected or account_fingerprint(tokens.account_id) != expected:
        raise RuntimeError('계정 연결 확인이 필요합니다.')
    result = verify(root, verification_runtime(root, find_app()), tokens, timeout=18)
    if account_fingerprint(read_existing_tokens(home, minimum_validity=0).account_id) != expected:
        raise RuntimeError('조회 중 계정이 변경되었습니다.')
    if not result.get('quota_read') or not result.get('usage', {}).get('windows'):
        raise RuntimeError('사용량 정보를 받지 못했습니다.')
    return result['usage']


class UsageRefresh:
    def __init__(self, root, store, *, probe=None, interval=60):
        self.root, self.store = Path(root), store
        self.probe = probe or (lambda profile: read_quota(self.root, profile))
        self.interval = interval
        self.lock = threading.Lock()
        self.running, self.attempted = {}, {}
        self.latest = {}

    def value(self, profile):
        with self.lock:
            binding, value = self.latest.get(profile['id'], (None, None))
            return deepcopy(newer(profile.get('usage'), value if binding == _binding(profile) else None))

    def schedule(self):
        profiles = self.store.read()['profiles']
        with self.lock:
            for profile in profiles:
                if len(self.running) >= 2:
                    break
                pid = profile['id']
                binding, cached = self.latest.get(pid, (None, None))
                usage = newer(profile.get('usage'), cached if binding == _binding(profile) else None)
                if (profile.get('removed_at') or profile.get('view_only') or not profile.get('account_fingerprint')
                        or pid in self.running or time.monotonic() - self.attempted.get(pid, -self.interval) < self.interval
                        or time.time() - observed_timestamp(usage) < self.interval):
                    continue
                snapshot = deepcopy(profile)
                worker = threading.Thread(target=self._run, args=(snapshot,), daemon=True,
                                          name='codex-quota-' + pid[:8])
                self.attempted[pid] = time.monotonic()
                self.running[pid] = worker
                worker.start()

    def active(self, profile_id):
        with self.lock:
            return profile_id in self.running

    def _run(self, profile):
        try:
            result = self.probe(profile)
            if not isinstance(result, dict) or not result.get('windows'):
                raise ValueError('Missing quota windows')
            def save(data):
                current = self.store.profile(profile['id'], data)
                if current.get('removed_at') or _binding(current) != _binding(profile):
                    return False
                current['usage'] = deepcopy(newer(current.get('usage'), result))
                return True
            if self.store.mutate(save):
                with self.lock:
                    self.latest[profile['id']] = (_binding(profile), deepcopy(result))
        except (ValueError, RuntimeError, OSError):
            def fail(data):
                current = self.store.profile(profile['id'], data)
                if current.get('removed_at') or _binding(current) != _binding(profile):
                    return
                usage = current.setdefault('usage', dict(windows=[]))
                # A failed older request must not mark a concurrently refreshed value stale.
                if observed_timestamp(usage) <= observed_timestamp(profile.get('usage', {})):
                    usage.update(freshness='stale', error=dict(code='refresh_failed',
                        message='사용량 조회에 실패해 이전 값을 표시합니다.'))
            try:
                self.store.mutate(fail)
            except (ValueError, RuntimeError, OSError):
                pass
        finally:
            with self.lock:
                self.running.pop(profile['id'], None)
