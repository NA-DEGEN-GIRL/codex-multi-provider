"""Borrow a registered Claude login's current access token without refreshing it.

Only selected-account SSH/delegation and the fixed subscription-usage reader
call this boundary. The official local CLI owns login and refresh; refresh
credentials never leave this module.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time

from .claude_auth import ClaudeError, auth_status, config_dir
from .store import Store, identifier

_REFRESH_GUARD = threading.Lock()
_REFRESH_LOCKS = {}
_REFRESH_ATTEMPTS = {}
_REFRESH_COOLDOWN = 30


class _Expired(Exception):
    pass


def _refresh_with_cli(profile_id):
    """Let the official CLI renew its own login without a model request.

    A hidden run of its built-in /usage command starts the CLI, which renews an
    expired access token itself. The refresh credential stays inside the CLI;
    this process only reads the renewed access token afterwards. Concurrent
    callers wait for one run, and a failed renewal is not retried for a while.
    """
    with _REFRESH_GUARD:
        lock = _REFRESH_LOCKS.setdefault(profile_id, threading.Lock())
    with lock:
        if time.monotonic() - _REFRESH_ATTEMPTS.get(profile_id, float('-inf')) < _REFRESH_COOLDOWN:
            return
        _REFRESH_ATTEMPTS[profile_id] = time.monotonic()
        from .claude_usage_terminal import query
        query(profile_id, timeout=15)


def read_access_token(root, profile_id, expected_account_identity, *, minimum_validity=120):
    """Return access-only JSON with an expiry expressed in Unix seconds.

    Remote and delegated Claude turns never run the CLI on this host, so its
    login is not renewed by use. An expired or expiring token is renewed once
    by the official CLI (see _refresh_with_cli) before the read is refused.
    """
    try:
        return _read_access_token(root, profile_id, expected_account_identity, minimum_validity)
    except _Expired:
        try:
            _refresh_with_cli(identifier(profile_id))
        except (OSError, ValueError, RuntimeError, ClaudeError):
            pass
    try:
        return _read_access_token(root, profile_id, expected_account_identity, minimum_validity)
    except _Expired:
        raise ClaudeError('claude_account_unavailable',
                          'The selected Claude account needs a current local login.') from None


def _read_access_token(root, profile_id, expected_account_identity, minimum_validity):
    failure = lambda: ClaudeError('claude_account_unavailable',
                                 'The selected Claude account needs a current local login.')
    if (not isinstance(expected_account_identity, str)
            or re.fullmatch(r'[0-9a-f]{64}', expected_account_identity) is None):
        raise failure()
    try:
        profile_id = identifier(profile_id)
        profile = Store(root).profile(profile_id)
        if (profile.get('auth_mode') != 'claude_code'
                or profile.get('claude_account_identity') != expected_account_identity):
            raise failure()
        directory = config_dir(profile_id)
        source = directory / '.credentials.json'
        if any(path.is_symlink() for path in (source, *source.parents)):
            raise failure()
        before = auth_status(profile_id)
        if not before.get('logged_in') or before.get('account_identity') != expected_account_identity:
            raise failure()
        with source.open('rb') as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise failure()
        value = json.loads(raw).get('claudeAiOauth')
        if not isinstance(value, dict):
            raise failure()
        token, expires = value.get('accessToken'), value.get('expiresAt')
        if (not isinstance(token, str) or not 1 <= len(token) <= 65536
                or any(ord(char) <= 32 or ord(char) >= 127 for char in token)
                or type(expires) is not int):
            raise failure()
        expires //= 1000  # Official Claude credential files use milliseconds.
        if expires < int(time.time()) + minimum_validity:
            raise _Expired()
        after = auth_status(profile_id)
        with source.open('rb') as stream:
            unchanged = stream.read(1024 * 1024 + 1)
        if (not after.get('logged_in') or after.get('account_identity') != expected_account_identity
                or hashlib.sha256(raw).digest() != hashlib.sha256(unchanged).digest()):
            raise failure()
        return {'accessToken': token, 'expiresAt': expires,
                'accountIdentity': expected_account_identity}
    except ClaudeError:
        raise failure() from None
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise failure() from None
