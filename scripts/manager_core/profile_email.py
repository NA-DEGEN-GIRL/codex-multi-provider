"""Explicit, read-only account labels. Never put full emails in state or logs."""
import base64
import json
from pathlib import Path

from .proxy_auth import account_fingerprint


def email_label(value):
    if (not isinstance(value, str) or len(value) > 320 or value.count('@') != 1
            or any(char.isspace() or ord(char) < 33 or ord(char) == 127 for char in value)
            or any(char in value for char in '<>"\\')):
        return None
    local, domain = value.split('@')
    return value if local and domain and '*' not in value else None


def binding(profile):
    return tuple(profile.get(key) for key in ('id', 'auth_mode', 'home', 'source_home',
        'account_fingerprint', 'claude_account_identity', 'removed_at', 'view_only'))


def _claims(token):
    if not isinstance(token, str) or len(token) > 65536:
        return {}
    pieces = token.split('.')
    if len(pieces) != 3:
        return {}
    try:
        payload = pieces[1]
        value = json.loads(base64.b64decode(payload + '=' * (-len(payload) % 4),
                                           altchars=b'-_', validate=True))
        return value if isinstance(value, dict) else {}
    except (ValueError, UnicodeError):
        return {}


def _codex(profile):
    # Expired logins still help identify which account needs to sign in again.
    # Decoding this local display metadata is NOT an authentication check.
    home = profile.get('home') if profile.get('auth_mode') == 'native' else profile.get('source_home')
    if not home:
        return None
    directory = Path(home).resolve()
    path = directory / 'auth.json'
    if path.is_symlink() or path.resolve().parent != directory:
        return None
    with path.open('rb') as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        return None
    data = json.loads(raw.decode('utf-8-sig'))
    tokens = data.get('tokens') if isinstance(data, dict) else None
    if not isinstance(tokens, dict):
        return None
    account = tokens.get('account_id')
    if not isinstance(account, str) or not account:
        return None
    expected = profile.get('account_fingerprint')
    if expected and expected != account_fingerprint(account):
        return None
    for key in ('id_token', 'access_token'):
        claims = _claims(tokens.get(key))
        identity = claims.get('https://api.openai.com/auth', {})
        if not isinstance(identity, dict) or identity.get('chatgpt_account_id', account) != account:
            return None
        metadata = claims.get('https://api.openai.com/profile', {})
        email = email_label(claims.get('email')) or email_label(
            metadata.get('email') if isinstance(metadata, dict) else None)
        if email:
            return email
    return None


def read(store, profile_id, *, reveal=False):
    if reveal is not True:
        raise ValueError('이메일 표시 버튼을 눌러 주세요.')
    profile = store.profile(profile_id)
    result = dict(profile_id=profile['id'], state='unavailable')
    if profile.get('removed_at') or profile.get('view_only') or profile.get('auth_mode') == 'external':
        return {**result, 'state': 'not_applicable'}
    try:
        if profile.get('auth_mode') == 'claude_code':
            from .claude_auth import auth_status
            status = auth_status(profile['id'], reveal_email=True)
            expected = profile.get('claude_account_identity')
            email = email_label(status.get('account_email')) if status.get('logged_in') else None
            if expected and expected != status.get('account_identity'):
                email = None
        else:
            email = _codex(profile)
        # A concurrent login, removal or reassignment must not label another account.
        if binding(store.profile(profile_id)) != binding(profile):
            return result
        if email:
            return {**result, 'state': 'available', 'email': email}
    except (OSError, ValueError, RuntimeError, TypeError):
        pass  # No credential-bearing paths, CLI output or parser text in errors.
    return result
