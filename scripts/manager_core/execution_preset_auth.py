"""Borrow prepared accounts over one managed SSH connection; never persist tokens."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import copy
from pathlib import Path
import re
import time
from uuid import UUID

from .proxy_auth import AuthProxyResult, LoginNeededError, account_fingerprint, read_existing_tokens

METHOD = 'account/executionPresetAuthTokens/read'
CAPABILITY = 'codex-manager/execution-preset-auth'
_FIELDS = {'ownerProfileId', 'roleId', 'profileId', 'kind',
           'expectedAccountFingerprint', 'expectedAccountIdentity'}


class ExecutionPresetAuthProxy:
    """A connection-scoped account allowlist, separate from the parent's AuthProxy."""

    def __init__(self, auth, store, authority, generation, *, token_reader=read_existing_tokens,
                 claude_reader=None):
        self.auth, self.store = auth, store
        self.authority = copy.deepcopy(authority)
        self.generation = str(UUID(generation))
        self.profile_id = str(UUID(authority['profile_id']))
        self._reader = token_reader
        self._claude_reader = claude_reader
        self._initialize_id = None
        self._initialize_seen = False
        self._consumed = deque(maxlen=512)
        self._closed = False
        self._workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix='managed-account-read')
        self._pending = {}
        self.execution_presets_version = 0
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
        if not isinstance(params, dict) or set(params) != _FIELDS or params['ownerProfileId'] != self.profile_id:
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
            value = reader(self.store.root, source['id'], role['expected_account_identity'])
            self._check_binding()
            return dict(kind=kind, accessToken=value['accessToken'], chatgptAccountId=None,
                        expiresAt=value['expiresAt'], accountIdentity=value['accountIdentity'])
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

    @staticmethod
    def _rejected(message):
        result = AuthProxyResult()
        if type(message.get('id')) in (str, int):
            result.runtime.append({'id': message['id'], 'error': {
                'code': -32042, 'message': 'The selected managed account is unavailable or changed.'}})
        return result

    def process(self, direction, message):
        method, request_id = message.get('method'), message.get('id')
        if direction == 'runtime' and method == METHOD:
            if type(request_id) not in (str, int) or request_id in self._consumed:
                return self._rejected(message)
            self._consumed.append(request_id)
            if self._closed or len(self._pending) >= 8:
                return self._rejected(message)
            params = copy.deepcopy(message.get('params'))
            self._pending[request_id] = (self._workers.submit(self._credentials, params), time.monotonic() + 25, params)
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
        return self.auth.process(direction, message)

    def poll(self):
        result = self.auth.poll()
        for request_id, (future, deadline, params) in list(self._pending.items()):
            if not future.done() and time.monotonic() < deadline:
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
            except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                result.runtime.extend(self._rejected({'id': request_id}).runtime)
        return result

    def close(self):
        self._closed = True
        self.execution_presets_version = 0
        self._pending.clear()
        self._workers.shutdown(wait=False, cancel_futures=True)


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
