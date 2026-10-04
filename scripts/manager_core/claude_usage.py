"""Claude-owned subscription observations; never call inference.

The native CLI exposes quota JSON on actual rate_limit_event messages, and its
fixed subscription endpoint provides a bounded read-only refresh. Its print-mode
command only reports billing mode. Missing windows stay unknown; neither token
counts nor estimated cost are quota values.
"""
from copy import deepcopy
from datetime import datetime, timezone
import math
from pathlib import Path
import re
import threading
import time

from .claude_auth import ClaudeError, auth_status, mask_email
from .native_usage import REFRESH_INTERVAL, STALE_AFTER, observed_timestamp
from .store import Unchanged


_WINDOWS = {'five_hour': '5시간', 'seven_day': '주간'}
_IDENTITY = re.compile(r'[0-9a-f]{64}')
_ERRORS = {
    'not_logged_in': 'Claude 로그인 후 사용량을 확인할 수 있습니다.',
    'account_unverified': 'Claude 계정 확인이 필요합니다. 로그인 상태를 다시 확인해 주세요.',
    'interactive_usage_required': '공식 Claude CLI의 /usage에서 현재 한도를 확인하세요. 자동 표시에는 CLI가 보낸 실제 한도 정보가 필요합니다.',
    'refresh_failed': 'Claude 사용량을 확인하지 못했습니다. 이전 값이 있으면 함께 표시합니다.',
    'refresh_timeout': 'Claude 사용량 조회 시간이 초과되었습니다. 잠시 후 다시 확인해 주세요.',
    'window_unavailable': 'Claude CLI가 아직 이 사용량 정보를 제공하지 않았습니다.',
    'onboarding_required': '공식 Claude Code의 첫 실행 설정을 완료한 뒤 사용량을 다시 확인해 주세요.',
    'usage_access_unavailable': 'Claude 사용량 조회 인증을 확인하지 못했습니다. 이 프로필의 로그인을 다시 확인해 주세요.',
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def unavailable(code='window_unavailable'):
    code = code if code in _ERRORS else 'refresh_failed'
    return dict(provider='claude_code', windows=[], observed_at=None,
                freshness='unknown', source='claude_cli', error=dict(code=code, message=_ERRORS[code]))


def _identity(value):
    return isinstance(value, str) and _IDENTITY.fullmatch(value) is not None


def _number(value, maximum):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= maximum


def _window(key, used, reset, stamp, reset_text=None):
    if key not in _WINDOWS or not _number(used, 100):
        return None
    # Reject invalid percentages; do not silently turn corrupted data into 0/100.
    reset = reset if type(reset) is int and 0 < reset < 253402300800 else None
    result = dict(key=key, label=_WINDOWS[key], used_percent=used,
                  remaining_percent=100-used, resets_at=reset, observed_at=stamp)
    if isinstance(reset_text, str) and re.fullmatch(r'Resets [A-Za-z0-9 ,:()./+_\-]{1,96}', reset_text):
        result['reset_text'] = reset_text
    return result


def normalize_event(event, *, observed_at=None):
    """Allowlist the official SDK rate-limit event, without copying raw payloads."""
    if not isinstance(event, dict) or event.get('type') != 'rate_limit_event':
        return None
    info = event.get('rate_limit_info')
    if not isinstance(info, dict):
        return None
    used, key = info.get('utilization'), info.get('rateLimitType')
    if not _number(used, 1) or key not in _WINDOWS:
        return None
    stamp = observed_at or _now()
    window = _window(key, used * 100, info.get('resetsAt'), stamp)
    return dict(provider='claude_code', windows=[window], observed_at=stamp,
                freshness='live', source='claude_cli_event', error=None)


def normalize_statusline(payload, *, observed_at=None):
    """Accept only official rate_limits fields, never the surrounding session JSON."""
    limits = payload.get('rate_limits') if isinstance(payload, dict) else None
    if not isinstance(limits, dict):
        return None
    stamp, windows = observed_at or _now(), []
    for key in _WINDOWS:
        raw = limits.get(key)
        if isinstance(raw, dict):
            window = _window(key, raw.get('used_percentage'), raw.get('resets_at'), stamp)
            if window:
                windows.append(window)
    return (dict(provider='claude_code', windows=windows, observed_at=stamp,
                 freshness='live', source='claude_cli_statusline', error=None) if windows else None)


def _clean(usage):
    """Only normalized observations may enter manager state or presentation."""
    if not isinstance(usage, dict) or usage.get('provider') != 'claude_code':
        return unavailable()
    windows = []
    values = usage.get('windows')
    values = values if isinstance(values, list) else []
    for key in _WINDOWS:
        raw = next((w for w in values if isinstance(w, dict) and w.get('key') == key), None)
        if not raw:
            continue
        stamp = raw.get('observed_at') or usage.get('observed_at')
        if not observed_timestamp({'observed_at': stamp}):
            continue
        window = _window(key, raw.get('used_percent'), raw.get('resets_at'), stamp, raw.get('reset_text'))
        if window:
            windows.append(window)
    result = unavailable()
    if windows:
        result.update(windows=windows, observed_at=min(windows, key=observed_timestamp)['observed_at'],
                      freshness='live', error=None)
    if usage.get('source') in ('claude_cli_event', 'claude_cli_statusline', 'claude_cli_usage', 'claude_oauth_usage'):
        result['source'] = usage['source']
    error = usage.get('error')
    if isinstance(error, dict) and error.get('code') in _ERRORS:
        result['error'] = unavailable(error['code'])['error']
    return result


def _merge(previous, observation):
    previous, observation = _clean(previous), _clean(observation)
    by_key = {w['key']: w for w in previous['windows']}
    for window in observation['windows']:
        old = by_key.get(window['key'])
        if not old or observed_timestamp(window) >= observed_timestamp(old):
            by_key[window['key']] = window
    result = dict(observation, windows=[by_key[k] for k in _WINDOWS if k in by_key])
    if not observation['windows'] and previous['windows']:
        result['source'] = previous['source']
    if result['windows']:
        # Receiving one window does not make another window's older value fresh.
        result['observed_at'] = min(result['windows'], key=observed_timestamp)['observed_at']
    return result


def record_event(store, profile_id, account_identity, event):
    """Record a real turn's CLI observation only for its still-current account."""
    observation = normalize_event(event)
    if not observation or not _identity(account_identity):
        return False
    def save(data):
        profile = store.profile(profile_id, data)
        if (profile.get('removed_at') or profile.get('auth_mode') != 'claude_code'
                or profile.get('claude_account_identity') != account_identity
                or not profile.get('claude_status', {}).get('logged_in')):
            return Unchanged(False)
        profile['usage'] = _merge(profile.get('usage'), observation)
        return True
    try:
        return store.mutate(save)
    except (ValueError, RuntimeError, OSError):
        # Quota display must never fail an otherwise valid user turn.
        return False


def read_quota(profile, *, root=None):
    """Account-checked subscription metadata; never an inference turn."""
    status = auth_status(profile['id'])
    code = ('not_logged_in' if not status.get('logged_in') else
            'account_unverified' if not _identity(status.get('account_identity')) else
            'interactive_usage_required')
    usage = unavailable(code)
    if code == 'interactive_usage_required':
        if root is not None:
            from .claude_usage_api import query
            windows, error = query(root, profile)
        else:
            from .claude_usage_terminal import query
            windows, error = query(profile['id'])
        usage = unavailable(error or 'window_unavailable')
        if windows:
            # The login can change while the terminal is open. Do not attach
            # its values to the previous or the replacement account.
            verified = auth_status(profile['id'])
            if verified.get('account_identity') != status.get('account_identity') or not verified.get('logged_in'):
                return dict(status=verified, account_identity=verified.get('account_identity'),
                            usage=unavailable('account_unverified'))
            stamp = _now()
            usage = _clean(dict(provider='claude_code', source='claude_oauth_usage' if root is not None else 'claude_cli_usage', observed_at=stamp,
                                windows=[dict(w, observed_at=stamp) for w in windows], error=None))
    return dict(status=status, account_identity=status.get('account_identity'), usage=usage)


def _binding(profile):
    return (profile.get('auth_mode'), profile.get('claude_account_identity'),
            profile.get('claude_status', {}).get('observed_at'),
            profile.get('claude_status', {}).get('logged_in'), profile.get('removed_at'))


class ClaudeUsage:
    """Coalesced, bounded background metadata refresh with account-scoped values."""
    INTERVAL = REFRESH_INTERVAL
    MANUAL_INTERVAL = 15

    def __init__(self, root, store, *, probe=None, interval=INTERVAL):
        self.root, self.store = Path(root), store
        self.probe, self.interval = probe or (lambda profile: read_quota(profile, root=self.root)), interval
        self.lock = threading.Lock()
        self.running, self.attempted = {}, {}

    def active(self, profile_id):
        with self.lock:
            return profile_id in self.running

    def value(self, profile):
        status = profile.get('claude_status', {})
        result = (_clean(profile.get('usage')) if status.get('logged_in')
                  and _identity(profile.get('claude_account_identity')) else
                  unavailable('not_logged_in' if not status.get('logged_in') else 'account_unverified'))
        now = time.time()
        for window in result['windows']:
            age = max(0, now - observed_timestamp(window))
            window['freshness'] = ('stale' if age > STALE_AFTER or
                                   (window.get('resets_at') and window['resets_at'] <= now) else 'live')
        stamp = observed_timestamp(result)
        result['age_seconds'] = int(max(0, now-stamp)) if stamp else None
        result['freshness'] = ('unknown' if not result['windows'] else 'stale'
                               if any(w['freshness'] == 'stale' for w in result['windows'])
                               or result.get('error') else 'live')
        result['refreshing'] = self.active(profile['id'])
        return result

    def _claim(self, profile, force):
        pid, now = profile['id'], time.monotonic()
        with self.lock:
            delay = self.MANUAL_INTERVAL if force else self.interval
            if len(self.running) >= 2 or pid in self.running or now-self.attempted.get(pid, -delay) < delay:
                return False
            self.running[pid] = True
            self.attempted[pid] = now
            return True

    def schedule(self, profiles=None):
        profiles = self.store.read()['profiles'] if profiles is None else profiles
        for profile in profiles:
            if profile.get('auth_mode') != 'claude_code' or profile.get('removed_at') or profile.get('view_only'):
                continue
            with self.lock:
                if len(self.running) >= 2:
                    break
            if self._claim(profile, False):
                threading.Thread(target=self._run, args=(deepcopy(profile),), daemon=True,
                                 name='claude-quota-' + profile['id'][:8]).start()

    def refresh(self, profile_id, force=True):
        profile = self.store.profile(profile_id)
        if profile.get('auth_mode') != 'claude_code' or profile.get('removed_at'):
            raise ValueError('Claude 프로필을 선택하세요.')
        if self._claim(profile, force):
            self._run(profile)
        return self.value(self.store.profile(profile_id))

    def _run(self, profile):
        try:
            reply = self.probe(profile)
            if not isinstance(reply, dict) or not isinstance(reply.get('status'), dict):
                raise ValueError('Missing official authentication status')
            status = reply['status']
            identity = reply.get('account_identity')
            identity = identity if status.get('logged_in') and _identity(identity) else None
            usage = _clean(reply.get('usage'))
            def save(data):
                current = self.store.profile(profile['id'], data)
                if _binding(current) != _binding(profile) or current.get('removed_at'):
                    return Unchanged(False)
                same = identity and current.get('claude_account_identity') == identity
                saved_status = {key: status[key] for key in ('logged_in', 'method', 'subscription_type', 'cli_version')
                                if key in status}
                email = mask_email(status.get('email') or status.get('masked_email'))
                if email:
                    saved_status['masked_email'] = email
                saved_status.update(observed_at=_now(), state='ready' if status.get('logged_in') else 'login_needed')
                current['claude_status'] = saved_status
                current['claude_account_identity'] = identity
                current['usage'] = _merge(current.get('usage'), usage) if same else usage
                return True
            self.store.mutate(save)
        except (ClaudeError, ValueError, RuntimeError, OSError):
            def fail(data):
                current = self.store.profile(profile['id'], data)
                if _binding(current) != _binding(profile) or current.get('removed_at'):
                    return Unchanged(False)
                current['usage'] = _merge(current.get('usage'), unavailable('refresh_failed'))
                return True
            try:
                self.store.mutate(fail)
            except (ValueError, RuntimeError, OSError):
                pass
        finally:
            with self.lock:
                self.running.pop(profile['id'], None)
