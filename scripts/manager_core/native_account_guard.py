"""Keep an executing native profile bound to its verified Windows identity.

The runtime still owns OAuth refresh in this profile's HOME. This guard neither
copies credentials nor logs tokens. Changing accounts belongs to the separate
login flow after the executing instance has closed.
"""
from pathlib import Path

from .proxy_auth import LoginNeededError, account_fingerprint, read_existing_tokens
from .runtime_admin import MaintenanceBarrier


class NativeAccountGuard:
    def __init__(self, environment):
        self.expected = (environment.get('CODEX_MANAGER_EXPECTED_ACCOUNT_FINGERPRINT')
                         if not environment.get('CODEX_MANAGER_AUTH_SOURCE') else None)
        self.home = Path(environment.get('CODEX_HOME', '')).resolve()
        self.login = (environment.get('CODEX_MANAGER_NATIVE_LOGIN') == '1'
                      and not environment.get('CODEX_MANAGER_AUTH_SOURCE'))

    @property
    def enabled(self):
        return self.expected is not None or self.login

    def matches(self):
        if not self.enabled:
            return True
        path = self.home / 'auth.json'
        try:
            if path.is_symlink() or path.resolve(strict=True).parent != self.home or path.stat().st_nlink != 1:
                return False
            # Expiry is handled by the native runtime's own OAuth refresh. Here
            # only the saved identity is checked; this is not server auth proof.
            tokens = read_existing_tokens(self.home, now=0, minimum_validity=0)
            if self.login and self.expected is None:
                # First native login binds this process once. A later account
                # switch cannot silently start work under a different identity.
                self.expected = account_fingerprint(tokens.account_id)
            return account_fingerprint(tokens.account_id) == self.expected
        except (OSError, ValueError, LoginNeededError):
            return False

    def blocks(self, message):
        if not self.enabled:
            return False
        method = message.get('method')
        if method in ('account/login/start', 'account/login/cancel', 'account/logout'):
            return not self.login
        if self.login and method in ('configRequirements/read','config/batchWrite',
                'config/value/write','windowsSandbox/readiness','windowsSandbox/setupStart'):
            return False
        if method in (None, 'initialize', 'initialized') or MaintenanceBarrier.allows_drain(method):
            return False
        return not self.matches()
