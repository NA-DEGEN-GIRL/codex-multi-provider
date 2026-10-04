"""Borrow a registered Claude login's current access token without refreshing it.

Only selected-account SSH/delegation and the fixed subscription-usage reader
call this boundary. The official local CLI owns login and refresh; refresh
credentials never leave this module.
"""
from __future__ import annotations

import hashlib
import json
import re
import time

from .claude_auth import ClaudeError, auth_status, config_dir
from .store import Store, identifier


def read_access_token(root, profile_id, expected_account_identity, *, minimum_validity=120):
    """Return access-only JSON with an expiry expressed in Unix seconds."""
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
            raise failure()
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
