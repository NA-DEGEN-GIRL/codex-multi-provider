"""Bind one stdio app-server to a registered account without copying its login.

Only the current access token and account ID are borrowed from the source home.
Refresh tokens are never selected, sent, persisted, or used to refresh a login.
The source Codex instance remains responsible for renewing its own credentials.

AuthProxy is a transport-independent state machine. Call ``process`` with the
message origin (``frontend`` or ``runtime``), then deliver the returned lists to
the corresponding destinations. Call ``poll`` while the connection is idle so
an unanswered internal login cannot leave the UI waiting forever. Never log
wire messages: the runtime list can contain an access token.
"""

import base64
from collections import deque
import copy
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import time
import uuid


AUTH_SOURCE_ENV = 'CODEX_MANAGER_AUTH_SOURCE'
_ENV_DEFAULT = object()
_PRIVATE_PREFIX = 'codex-manager-auth:'
_AUTH_ERROR = -32041
_BOUND_ERROR = -32042
_ACCOUNT_CONTROLS = frozenset({
    'account/logout', 'account/login/start', 'account/login/cancel',
    'account/sessions/add', 'account/sessions/switch', 'account/sessions/logout',
})


class LoginNeededError(RuntimeError):
    """A deliberately sanitized error safe to report to the manager."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__('The registered account needs a valid existing login.')


@dataclass(frozen=True)
class AuthTokens:
    access_token: str = field(repr=False)
    account_id: str = field(repr=False)

    def login_params(self):
        return {'type': 'chatgptAuthTokens', 'accessToken': self.access_token,
                'chatgptAccountId': self.account_id}

    def refresh_result(self):
        return {'accessToken': self.access_token, 'chatgptAccountId': self.account_id,
                'chatgptPlanType': None}


def _validate_tokens(token, account, now, minimum_validity):
    if not isinstance(token, str) or not token or not isinstance(account, str) or not account.strip():
        raise LoginNeededError('missing_access_token')
    try:
        pieces = token.split('.')
        if len(pieces) != 3 or not all(pieces):
            raise ValueError('JWT structure')
        payload = pieces[1]
        raw = base64.b64decode(payload + '=' * (-len(payload) % 4), altchars=b'-_', validate=True)
        claims = json.loads(raw)
        if not isinstance(claims, dict):
            raise ValueError('JWT claims')
    except (ValueError, UnicodeError, TypeError):
        raise LoginNeededError('invalid_access_token') from None
    expiry = claims.get('exp')
    if (isinstance(expiry, bool) or not isinstance(expiry, (float, int))
            or not math.isfinite(expiry)):
        raise LoginNeededError('invalid_token_expiry')
    if expiry <= now + minimum_validity:
        raise LoginNeededError('expired_access_token')
    # This only checks local expiry/identity metadata. The runtime/backend must
    # authenticate the token; parsing a JWT is not signature verification.
    auth_claims = claims.get('https://api.openai.com/auth', {})
    if not isinstance(auth_claims, dict):
        raise LoginNeededError('invalid_access_token')
    claimed_account = auth_claims.get('chatgpt_account_id')
    if claimed_account is not None and claimed_account != account:
        raise LoginNeededError('account_mismatch')
    return AuthTokens(token, account)


def read_existing_tokens(source_home, *, now=None, minimum_validity=120):
    """Read a source home's current access credential; never write any file."""
    source = Path(source_home) / 'auth.json'
    try:
        if source.stat().st_size > 1024 * 1024:
            raise LoginNeededError('invalid_auth_file')
        data = json.loads(source.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError, UnicodeError):
        raise LoginNeededError('unreadable_auth_source') from None
    if not isinstance(data, dict) or not isinstance(data.get('tokens'), dict):
        raise LoginNeededError('missing_access_token')
    tokens = data['tokens']
    return _validate_tokens(tokens.get('access_token'), tokens.get('account_id'),
                            time.time() if now is None else now, minimum_validity)


def account_fingerprint(account_id):
    """Non-credential identity marker for binding subsequent managed launches."""
    return hashlib.sha256(b'codex-manager-account-v1\0' + account_id.encode('utf-8')).hexdigest()


@dataclass
class AuthProxyResult:
    # Do not include outgoing wire payloads in a repr or manager diagnostics.
    runtime: list = field(default_factory=list, repr=False)
    frontend: list = field(default_factory=list, repr=False)
    events: list = field(default_factory=list)


class AuthProxy:
    """Authenticate before forwarding normal UI requests, or fail closed.

    An omitted source_home uses CODEX_MANAGER_AUTH_SOURCE. Explicit None disables
    binding (unless a fixture token_loader was supplied). token_loader must return
    AuthTokens and is called anew for each login/refresh; it must not log secrets.
    All process/poll calls for one proxy must be serialized by its transport.
    """

    def __init__(self, source_home=_ENV_DEFAULT, *, token_loader=None,
                 expected_account_id=None, expected_account_fingerprint=None,
                 clock=time.time, max_queued=1024, max_queued_bytes=8 * 1024 * 1024,
                 auth_timeout=30, minimum_validity=120):
        if source_home is _ENV_DEFAULT:
            source_home = os.environ.get(AUTH_SOURCE_ENV)
        self.bound = bool(source_home) or token_loader is not None
        self.state = 'wait_initialize' if self.bound else 'disabled'
        self.reason = None
        self._clock = clock
        self._minimum_validity = minimum_validity
        self._loader = token_loader or (lambda: read_existing_tokens(
            source_home, now=self._clock(), minimum_validity=minimum_validity))
        self._account_id = expected_account_id
        self._expected_account_fingerprint = expected_account_fingerprint
        self.account_fingerprint = None
        self._initialize_id = None
        self._initialize_seen = False
        self._initialize_ok = False
        self._initialized = None
        self._login_id = _PRIVATE_PREFIX + str(uuid.uuid4())
        self._login_deadline = None
        self._timeout = auth_timeout
        self._pending = deque()
        self._max_queued = max_queued
        self._max_queued_bytes = max_queued_bytes
        self._pending_bytes = 0
        self._consumed_refresh_ids = deque(maxlen=256)

    @staticmethod
    def _error(message, reason, *, bound_control=False):
        if 'id' not in message:
            return None
        return {'id': message['id'], 'error': {
            'code': _BOUND_ERROR if bound_control else _AUTH_ERROR,
            'message': ('이 프로필은 등록된 계정에 연결되어 있습니다. 다시 로그인하려면 이 프로필의 Codex 창을 닫고, 작업 공간의 같은 프로필에서 ‘이 프로필에 로그인’을 누르세요. 다른 프로필을 선택할 필요는 없습니다.'
                        if bound_control else 'This profile needs login. Select it in Codex workspace and use its profile login button to sign in to the registered account.'),
            'data': {'status': 'account_bound' if bound_control else 'login_needed', 'reason': reason},
        }}

    def _fail(self, result, reason):
        if self.state != 'login_needed':
            result.events.append({'state': 'login_needed', 'reason': reason})
        self.state = 'login_needed'
        self.reason = reason
        self._login_deadline = None
        while self._pending:
            error = self._error(self._pending.popleft(), reason)
            if error is not None:
                result.frontend.append(error)
        self._pending_bytes = 0

    def _read_tokens(self):
        try:
            tokens = self._loader()
            if not isinstance(tokens, AuthTokens):
                raise LoginNeededError('invalid_auth_source')
            tokens = _validate_tokens(tokens.access_token, tokens.account_id,
                                      self._clock(), self._minimum_validity)
        except LoginNeededError:
            raise
        except Exception:
            # OS/parser/adapter exceptions can include path or credential data.
            raise LoginNeededError('unreadable_auth_source') from None
        if self._account_id is not None and self._account_id != tokens.account_id:
            raise LoginNeededError('account_mismatch')
        fingerprint = account_fingerprint(tokens.account_id)
        if (self._expected_account_fingerprint is not None
                and self._expected_account_fingerprint != fingerprint):
            raise LoginNeededError('account_mismatch')
        self._account_id = tokens.account_id
        self.account_fingerprint = fingerprint
        return tokens

    def _start_login(self, result):
        if not self._initialize_ok or self.state in ('authenticating', 'ready', 'login_needed'):
            return
        # App-server is ready after the initialize response. The native desktop
        # client can read configRequirements without ever sending initialized;
        # waiting for that optional notification deadlocks its startup.
        if self._initialized is not None:
            result.runtime.append(self._initialized)
            self._initialized = None
        try:
            tokens = self._read_tokens()
        except LoginNeededError as error:
            self._fail(result, error.reason)
            return
        self.state = 'authenticating'
        self._login_deadline = self._clock() + self._timeout
        result.runtime.append({'id': self._login_id, 'method': 'account/login/start',
                               'params': tokens.login_params()})
        result.events.append({'state': 'authenticating'})

    def poll(self):
        result = AuthProxyResult()
        if self._login_deadline is not None and self._clock() >= self._login_deadline:
            self._fail(result, 'auth_timeout')
        return result

    def process(self, direction, message):
        if direction not in ('frontend', 'runtime'):
            raise ValueError('Message direction must be frontend or runtime.')
        if not isinstance(message, dict):
            raise TypeError('AuthProxy accepts individual JSON-RPC objects.')
        result = self.poll()
        if not self.bound:
            getattr(result, 'runtime' if direction == 'frontend' else 'frontend').append(message)
            return result
        if direction == 'runtime':
            self._from_runtime(message, result)
        else:
            self._from_frontend(message, result)
        return result

    def _from_frontend(self, message, result):
        method = message.get('method')
        request_id = message.get('id')
        if isinstance(request_id, str) and request_id.startswith(_PRIVATE_PREFIX):
            if method is not None:
                error = self._error(message, 'reserved_request_id', bound_control=True)
                if error is not None:
                    result.frontend.append(error)
            return
        if method is None:
            # Refresh requests never reach the frontend; discard stale/spurious
            # replies to one so they cannot replace the manager's fresh token.
            if request_id not in self._consumed_refresh_ids:
                result.runtime.append(message)
            return
        if method in _ACCOUNT_CONTROLS:
            error = self._error(message, 'manager_controls_account', bound_control=True)
            if error is not None:
                result.frontend.append(error)
            result.events.append({'state': self.state, 'reason': 'account_control_denied'})
            return
        if method == 'initialize' and not self._initialize_seen and self.state != 'login_needed':
            self._initialize_seen = True
            self._initialize_id = request_id
            initialize = copy.deepcopy(message)
            params = initialize.setdefault('params', {})
            if not isinstance(params, dict):
                self._fail(result, 'invalid_initialize')
                error = self._error(message, self.reason)
                if error is not None:
                    result.frontend.append(error)
                return
            capabilities = params.get('capabilities') or {}
            if not isinstance(capabilities, dict):
                self._fail(result, 'invalid_initialize')
                error = self._error(message, self.reason)
                if error is not None:
                    result.frontend.append(error)
                return
            params['capabilities'] = capabilities
            capabilities['experimentalApi'] = True
            result.runtime.append(initialize)
            return
        if method == 'initialized':
            if self.state == 'login_needed':
                return
            if self._initialize_ok:
                result.runtime.append(message)
            else:
                self._initialized = copy.deepcopy(message)
            return
        if self.state == 'ready':
            result.runtime.append(message)
        elif self.state == 'login_needed':
            error = self._error(message, self.reason)
            if error is not None:
                result.frontend.append(error)
        elif len(self._pending) >= self._max_queued:
            self._fail(result, 'auth_queue_full')
            error = self._error(message, self.reason)
            if error is not None:
                result.frontend.append(error)
        else:
            # The count limit alone permits many large turn payloads to pile up
            # during login. Measure wire bytes before retaining another copy.
            try:
                message_bytes = len(json.dumps(message, ensure_ascii=False,
                                               separators=(',', ':')).encode('utf-8'))
            except (TypeError, ValueError, UnicodeError, RecursionError):
                message_bytes = self._max_queued_bytes + 1
            if self._pending_bytes + message_bytes > self._max_queued_bytes:
                self._fail(result, 'auth_queue_full')
                error = self._error(message, self.reason)
                if error is not None:
                    result.frontend.append(error)
            else:
                self._pending.append(copy.deepcopy(message))
                self._pending_bytes += message_bytes

    def _from_runtime(self, message, result):
        method = message.get('method')
        request_id = message.get('id')
        if method == 'account/updated' and self.state == 'ready':
            params = message.get('params')
            if not isinstance(params, dict) or params.get('authMode') != 'chatgptAuthTokens':
                self._fail(result, 'bound_auth_changed')
            result.frontend.append(message)
            return
        if method == 'account/chatgptAuthTokens/refresh':
            if 'id' not in message:
                self._fail(result, 'invalid_refresh_request')
                return
            self._consumed_refresh_ids.append(request_id)
            try:
                if self.state not in ('authenticating', 'ready'):
                    raise LoginNeededError(self.reason or 'refresh_before_auth')
                params = message.get('params') or {}
                previous = params.get('previousAccountId')
                tokens = self._read_tokens()
                if previous is not None and previous != tokens.account_id:
                    raise LoginNeededError('account_mismatch')
                result.runtime.append({'id': request_id, 'result': tokens.refresh_result()})
            except LoginNeededError as error:
                self._fail(result, error.reason)
                result.runtime.append(self._error(message, error.reason))
            except (AttributeError, TypeError):
                self._fail(result, 'invalid_refresh_request')
                result.runtime.append(self._error(message, self.reason))
            return
        if method is None and request_id == self._login_id:
            # Suppress both success and error responses, including any echoed
            # credentials from a buggy or incompatible runtime.
            if self.state != 'authenticating':
                return
            if message.get('error') is not None:
                self._fail(result, 'auth_rejected')
            elif not isinstance(message.get('result'), dict) or message['result'].get('type') != 'chatgptAuthTokens':
                self._fail(result, 'invalid_auth_response')
            else:
                self._login_deadline = None
                self.state = 'ready'
                self.reason = None
                result.events.append({'state': 'ready', 'account_fingerprint': self.account_fingerprint})
                result.runtime.extend(self._pending)
                self._pending.clear()
                self._pending_bytes = 0
            return
        if (method is None and self._initialize_seen and not self._initialize_ok
                and request_id == self._initialize_id):
            result.frontend.append(message)
            if self.state == 'login_needed':
                # A late/duplicate initialize response must never revive a
                # connection already rejected by the queue/auth guards.
                return
            if message.get('error') is not None or not isinstance(message.get('result'), dict):
                self._fail(result, 'initialize_failed')
                return
            self._initialize_ok = True
            self._start_login(result)
            return
        result.frontend.append(message)
