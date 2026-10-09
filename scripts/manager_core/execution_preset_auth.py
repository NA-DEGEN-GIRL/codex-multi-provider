"""Borrow prepared accounts over one managed SSH connection; never persist tokens."""
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as WaitTimeout
import copy
from pathlib import Path
import re
import threading
import time
from uuid import UUID

from .claude_auth import canonical_uuid
from .proxy_auth import AuthProxyResult, LoginNeededError, account_fingerprint, read_existing_tokens

METHOD = 'account/executionPresetAuthTokens/read'
CAPABILITY = 'codex-manager/execution-preset-auth'
_FIELDS = {'ownerProfileId', 'roleId', 'profileId', 'kind',
           'expectedAccountFingerprint', 'expectedAccountIdentity'}
# A runtime sends this only after its runner reported a lent long-lived token as refused.
_REJECTED = 'rejectedCredentialId'
# Initialize result key of a runtime that forwards a credential's source to its runner.
SOURCES_CAPABILITY = 'executionPresetCredentialSources'
_WINDOWS_LOGIN = 'windowsLogin'
_PENDING_LIMIT = 8
# A read gets this long once a worker starts it; waiting for a worker is not
# charged. The runtime abandons a request 30 s after sending it, so every answer
# is also bounded from receipt and never sent after the runtime stopped waiting.
_READ_TIMEOUT = 25
_REQUEST_TIMEOUT = 28
_clock = time.monotonic


class _Pending:
    """One runtime request; the worker records when it actually starts."""
    __slots__ = ('params', 'received', 'started', 'future')

    def __init__(self, params):
        self.params, self.received, self.started, self.future = params, _clock(), None, None

    def expired(self, now):
        started = self.started
        return (now >= self.received + _REQUEST_TIMEOUT
                or (started is not None and now >= started + _READ_TIMEOUT))


class LongLivedUnavailable(LoginNeededError):
    """The saved long-lived token was refused or expired and the PC login cannot replace it.

    Its reason is the only one a refused read reports to the runtime."""


class ExecutionPresetAuthProxy:
    """A connection-scoped account allowlist, separate from the parent's AuthProxy.

    A Claude account is lent from the profile's saved long-lived token when the runtime
    declared SOURCES_CAPABILITY and the role's prepared authority has credential_sources == 1
    (its helpers accept a credential's source); otherwise, and as a fallback, the PC login's
    access token is borrowed. Only then does a result carry credentialSource/credentialId.
    """

    def __init__(self, auth, store, authority, generation, *, token_reader=read_existing_tokens,
                 claude_reader=None, long_lived_reader=None):
        self.auth, self.store = auth, store
        self.authority = copy.deepcopy(authority)
        self.generation = str(UUID(generation))
        self.profile_id = str(UUID(authority['profile_id']))
        self._reader = token_reader
        self._claude_reader = claude_reader
        self._long_lived_reader = long_lived_reader
        self._initialize_id = None
        self._initialize_seen = False
        self._consumed = deque(maxlen=512)
        self._closed = False
        # One worker per admitted request: a slow renewal of one account never
        # queues another account's read. Turns of one Claude login wait on a
        # single shared read instead (see _claude_read).
        self._workers = ThreadPoolExecutor(max_workers=_PENDING_LIMIT, thread_name_prefix='managed-account-read')
        self._pending = {}
        self._reads_lock = threading.Lock()
        self._claude_reads = {}
        # Long-lived credential IDs lent over this connection. A runtime's rejection report is
        # recorded only for one of these; it is remote input and never builds a path.
        self._lent = set()
        self.execution_presets_version = 0
        self.credential_sources = 0
        self._check_binding()

    def __getattr__(self, name):
        return getattr(self.auth, name)

    def _check_binding(self):
        if self._closed:
            raise LoginNeededError('managed_transport_closed')
        profile = self.store.profile(self.profile_id)
        authority = self.authority
        if (authority.get('schema_version') != 1 or profile.get('generation') != self.generation
                or not isinstance(authority.get('roles'), dict)
                or len(authority['roles']) > 256):
            raise LoginNeededError('managed_binding_changed')
        binding = next((item for item in profile.get('remote_bindings', [])
                        if 'ssh:' + str(item.get('alias')) == authority.get('host_id')), None)
        if (not binding or binding.get('prepared') is not True
                or binding.get('revision') != authority.get('revision')
                or binding.get('host_identity') != authority.get('host_identity')
                or not re.fullmatch('[0-9a-f]{64}', authority.get('host_identity') or '')):
            raise LoginNeededError('managed_binding_changed')

    def _credentials(self, params):
        self._check_binding()
        if self.execution_presets_version != 1:
            raise LoginNeededError('managed_runtime_not_ready')
        if (not isinstance(params, dict) or set(params) not in (_FIELDS, _FIELDS | {_REJECTED})
                or params['ownerProfileId'] != self.profile_id):
            raise LoginNeededError('unprepared_account')
        rejected = params.get(_REJECTED)
        if rejected is not None and (params['kind'] != 'claude' or not canonical_uuid(rejected)):
            raise LoginNeededError('unprepared_account')
        role_id = params['roleId']
        if role_id is None:
            role = self.authority.get('main_auth')
        elif isinstance(role_id, str) and len(role_id) <= 128:
            role = self.authority['roles'].get(role_id)
        else:
            role = None
        if not isinstance(role, dict) or role.get('auth_source') != 'manager_proxy':
            raise LoginNeededError('unprepared_account')
        kind = ('openai' if role.get('model_provider') == 'openai' else 'claude')
        if (params['kind'] != kind or params['profileId'] != role.get('profile_id')
                or params['expectedAccountFingerprint'] != role.get('expected_account_fingerprint')
                or params['expectedAccountIdentity'] != role.get('expected_account_identity')):
            raise LoginNeededError('unprepared_account')
        source = self.store.profile(str(UUID(role['profile_id'])))
        if kind == 'claude':
            if self._claude_reader is None:
                from .claude_borrowed_auth import read_access_token
                reader = read_access_token
            else:
                reader = self._claude_reader
            if (source.get('auth_mode') != 'claude_code'
                    or source.get('claude_account_identity') != role.get('expected_account_identity')):
                raise LoginNeededError('account_mismatch')
            sources = self.credential_sources == 1 and role.get('credential_sources') == 1
            value = self._claude_read(reader, source['id'], role['expected_account_identity'],
                                      sources, rejected)
            self._check_binding()
            result = dict(kind=kind, accessToken=value['accessToken'], chatgptAccountId=None,
                          expiresAt=value['expiresAt'], accountIdentity=value['accountIdentity'])
            if sources:
                result.update(credentialSource=value['credentialSource'], credentialId=value['credentialId'])
            return result
        if source.get('auth_mode') in ('external', 'claude_code'):
            raise LoginNeededError('account_mismatch')
        expected = role.get('expected_account_fingerprint')
        if (not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected)
                or source.get('account_fingerprint') != expected):
            raise LoginNeededError('account_mismatch')
        home = source.get('home') if source.get('auth_mode') == 'native' else source.get('source_home')
        if not home or not Path(home).is_absolute():
            raise LoginNeededError('missing_account_source')
        tokens = self._reader(home)
        if account_fingerprint(tokens.account_id) != expected:
            raise LoginNeededError('account_mismatch')
        self._check_binding()
        return dict(kind=kind, accessToken=tokens.access_token, chatgptAccountId=tokens.account_id,
                    expiresAt=None, accountIdentity=None)

    def _claude_read(self, reader, profile_id, identity, sources=False, rejected=None):
        """Validated requests for one Claude login share the read in flight.

        Several turns starting together then cause one renewal, not one queued
        read each. A finished read is never reused: a later request reads again.
        A read that reports a rejected credential never joins one that may lend it.
        """
        key = (profile_id, identity, sources, rejected)
        with self._reads_lock:
            shared = self._claude_reads.get(key)
            owner = shared is None
            if owner:
                shared = self._claude_reads[key] = Future()
        if owner:
            try:
                shared.set_result(self._choose(reader, profile_id, identity, rejected) if sources
                                  else reader(self.store.root, profile_id, identity))
            except BaseException as error:
                shared.set_exception(error)
            finally:
                with self._reads_lock:
                    del self._claude_reads[key]
        try:
            return shared.result(timeout=_REQUEST_TIMEOUT)
        except WaitTimeout:
            raise LoginNeededError('managed_account_timeout') from None

    def _choose(self, reader, profile_id, identity, rejected):
        """The saved long-lived token, else the PC login; each result names its source.

        A rejection is recorded only for a credential this connection lent. Another rejected
        ID still keeps that credential out of this read, so a runtime that reconnected keeps
        working with the PC login.
        """
        root = self.store.root
        with self._reads_lock:
            known = rejected in self._lent
        if self._long_lived_reader is None:
            from .claude_long_lived_auth import lend
        else:
            lend = self._long_lived_reader
        try:
            value = lend(root, profile_id, identity, rejected if known else None)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            value = None  # The PC login still serves the turn.
        if value is not None and value.get('credentialId') != rejected:
            with self._reads_lock:
                self._lent.add(value['credentialId'])
            return value
        try:
            value = reader(root, profile_id, identity)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            from .claude_long_lived_auth import refusal_reason
            reason = refusal_reason(root, profile_id)
            if reason is not None:
                raise LongLivedUnavailable(reason) from None
            raise
        return dict(value, credentialSource=_WINDOWS_LOGIN, credentialId=None)

    def _run(self, pending):
        pending.started = _clock()
        return self._credentials(pending.params)

    @staticmethod
    def _rejected(message, reason=None):
        result = AuthProxyResult()
        if type(message.get('id')) in (str, int):
            error = {'code': -32042, 'message': 'The selected managed account is unavailable or changed.'}
            if reason is not None:
                error['data'] = {'reason': reason}
            result.runtime.append({'id': message['id'], 'error': error})
        return result

    def process(self, direction, message):
        method, request_id = message.get('method'), message.get('id')
        if direction == 'runtime' and method == METHOD:
            if type(request_id) not in (str, int) or request_id in self._consumed:
                return self._rejected(message)
            self._consumed.append(request_id)
            if self._closed or len(self._pending) >= _PENDING_LIMIT:
                return self._rejected(message)
            pending = _Pending(copy.deepcopy(message.get('params')))
            pending.future = self._workers.submit(self._run, pending)
            self._pending[request_id] = pending
            return AuthProxyResult()
        if direction == 'frontend':
            if method == METHOD:
                return AuthProxyResult(frontend=[{'id': request_id, 'error': {
                    'code': -32042, 'message': 'Managed credential requests are reserved.'}}])
            if method is None and request_id in self._consumed:
                return AuthProxyResult()
            if method == 'initialize' and not self._initialize_seen:
                self._check_binding()
                self._initialize_seen, self._initialize_id = True, request_id
                message = copy.deepcopy(message)
                caps = message.setdefault('params', {}).setdefault('capabilities', {})
                if not isinstance(caps, dict):
                    raise ValueError('invalid initialize capabilities')
                extensions = caps.setdefault('extensions', {})
                if not isinstance(extensions, dict):
                    raise ValueError('invalid initialize extensions')
                extensions[CAPABILITY] = {'profileId': self.profile_id}
        elif (method is None and self._initialize_seen and request_id == self._initialize_id
              and isinstance(message.get('result'), dict)):
            self.execution_presets_version = 1 if type(message['result'].get('executionPresetsVersion')) is int and message['result']['executionPresetsVersion'] == 1 else 0
            sources = message['result'].get(SOURCES_CAPABILITY)
            self.credential_sources = 1 if type(sources) is int and sources == 1 else 0
        return self.auth.process(direction, message)

    def poll(self):
        result = self.auth.poll()
        now = _clock()
        for request_id, pending in list(self._pending.items()):
            future, params = pending.future, pending.params
            if not future.done() and not pending.expired(now):
                continue
            del self._pending[request_id]
            if self._closed:
                future.cancel()
                continue
            try:
                if not future.done():
                    future.cancel()
                    raise LoginNeededError('managed_account_timeout')
                value = future.result()
                self._check_binding()
                source = self.store.profile(params['profileId'])
                if ((value['kind'] == 'openai' and source.get('account_fingerprint') != params['expectedAccountFingerprint'])
                        or (value['kind'] == 'claude' and source.get('claude_account_identity') != params['expectedAccountIdentity'])):
                    raise LoginNeededError('account_mismatch')
                result.runtime.append({'id': request_id, 'result': value})
            except LongLivedUnavailable as error:
                result.runtime.extend(self._rejected({'id': request_id}, error.reason).runtime)
            except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                result.runtime.extend(self._rejected({'id': request_id}).runtime)
        return result

    def close(self):
        self._closed = True
        self.execution_presets_version = 0
        self.credential_sources = 0
        self._pending.clear()
        self._workers.shutdown(wait=False, cancel_futures=True)
        with self._reads_lock:
            self._lent.clear()


def wrap_ssh_auth(auth, source_environment, event):
    """Load authority from the manager-owned immutable prepared snapshot, never wire input."""
    root, generation = source_environment.get('CODEX_MANAGER_ROOT'), source_environment.get('CODEX_MANAGER_GENERATION')
    if not root or not generation:
        return auth
    from .store import Store
    from .remote import RemoteManager
    store = Store(root)
    profile = store.profile(event['profile_id'])
    binding = next((item for item in profile.get('remote_bindings', [])
                    if item.get('alias') == event['alias'] and item.get('revision') == event['revision']), None)
    if not binding or binding.get('execution_presets_version') != 1:
        return auth
    authority = RemoteManager(root).execution_preset_authority(profile, binding)
    return ExecutionPresetAuthProxy(auth, store, authority, generation)
