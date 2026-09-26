"""Bounded, background quota refresh. Never starts a task or changes a login."""
from copy import deepcopy
from pathlib import Path
import subprocess
import threading
import time

from .native_usage import REFRESH_INTERVAL, newer, observed_timestamp
from .store import Unchanged

# An idle profile's saved observation is still rewritten this often, so a
# restarted service does not start from an hours-old value.
_REWRITE_AFTER = 1800


def _binding(profile):
    return tuple(profile.get(key) for key in
                 ('auth_mode', 'home', 'source_home', 'account_fingerprint', 'usage_account_id'))


def _marked(usage):
    return isinstance(usage, dict) and bool(usage.get('error') or usage.get('freshness') == 'stale')


def _unchanged(stored, result):
    """True when saving result would only move a recent observed_at of a clean value."""
    return (isinstance(stored, dict) and not _marked(stored)
            and stored.get('windows') == result.get('windows')
            and (not result.get('reset_credits') or stored.get('reset_credits') == result.get('reset_credits'))
            and time.time() - observed_timestamp(stored) < _REWRITE_AFTER)


_purges = {}
_purges_lock = threading.Lock()
_PURGE_DELAY = 120
_PURGE_EVERY = 6 * 3600


def _purge_due(root):
    """Start the stale probe-home purge for root when its last run is 6 h old."""
    key, now = str(root), time.monotonic()
    with _purges_lock:
        started, worker = _purges.get(key, (None, None))
        if worker is not None and (worker.is_alive() or now - started < _PURGE_EVERY):
            return
        def run():
            from .login_probe import purge_in_child
            time.sleep(_PURGE_DELAY)  # Let startup profile launches finish their disk work first.
            try:
                purge_in_child(root)  # a child process, off this backend's GIL
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                pass  # The next periodic run retries; never disturb quota refresh.
        # A long-running service still removes homes a locked probe left behind.
        worker = threading.Thread(target=run, daemon=True, name='codex-probe-purge')
        _purges[key] = (now, worker)
    worker.start()


def read_quota(root, profile):
    from .proxy_auth import read_existing_tokens, account_fingerprint
    from .login_probe import verify, verification_runtime
    from desktop_launch import cached_app
    home = Path(profile['home'] if profile.get('auth_mode') == 'native' else
                profile.get('source_home') or profile['home'])
    expected = profile.get('account_fingerprint')
    tokens = read_existing_tokens(home)
    if not expected or account_fingerprint(tokens.account_id) != expected:
        raise RuntimeError('계정 연결 확인이 필요합니다.')
    result = verify(root, verification_runtime(root, cached_app()), tokens, timeout=18)
    if account_fingerprint(read_existing_tokens(home, minimum_validity=0).account_id) != expected:
        raise RuntimeError('조회 중 계정이 변경되었습니다.')
    if not result.get('quota_read') or not result.get('usage', {}).get('windows'):
        raise RuntimeError('사용량 정보를 받지 못했습니다.')
    return result['usage']


class UsageRefresh:
    # Each background probe starts a codex app-server in a new CODEX_HOME. The
    # backend does not know which profile is on screen, so every account uses
    # one slower cadence. refresh_all and login verification stay immediate.
    INTERVAL = REFRESH_INTERVAL

    def __init__(self, root, store, *, probe=None, interval=INTERVAL):
        self.root, self.store = Path(root), store
        self.probe = probe or (lambda profile: read_quota(self.root, profile))
        self.interval = interval
        self.lock = threading.Lock()
        self.running, self.attempted = {}, {}
        self.latest = {}

    def value(self, profile):
        stored = profile.get('usage')
        with self.lock:
            binding, value = self.latest.get(profile['id'], (None, None))
            shown = deepcopy(newer(stored, value if binding == _binding(profile) else None))
        if _marked(stored) and isinstance(shown, dict) and not _marked(shown):
            # A clean reply is kept in memory only while the saved value was
            # clean, so a saved stale or error mark is the later event.
            shown.update(freshness='stale', error=deepcopy(stored.get('error')))
        return shown

    def schedule(self, profiles=None):
        """Start due probes; profiles are raw store profiles when the caller has them."""
        _purge_due(self.root)
        if profiles is None:
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
                if _unchanged(current.get('usage'), result):
                    # Same windows as the value saved now (not at poll start, so
                    # a mark written during the probe is replaced): skip the
                    # whole-state rewrite. state() shows the newer observation
                    # from memory; a restarted service re-probes.
                    return Unchanged(True)
                current['usage'] = deepcopy(newer(current.get('usage'), result))
                return True
            if self.store.mutate(save):
                with self.lock:
                    self.latest[profile['id']] = (_binding(profile), deepcopy(result))
        except (ValueError, RuntimeError, OSError):
            with self.lock:
                # A reply kept only in memory must turn stale like the saved one.
                binding, cached = self.latest.get(profile['id'], (None, None))
                if cached is not None and binding == _binding(profile):
                    cached.update(freshness='stale', error=dict(code='refresh_failed',
                        message='사용량 조회에 실패해 이전 값을 표시합니다.'))
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
