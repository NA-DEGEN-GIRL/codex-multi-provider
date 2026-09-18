"""Authenticated, local-only administration of an already connected runtime.

The proxy remains the sole reader of runtime stdout. ``AdminRpcBroker`` writes
through an injected callback and consumes its own responses in the existing
stdout pump. The callback must check the account gate and serialize with normal
frontend writes. It must not wait for a reply while holding the protocol lock.

Only JSON bytes cross the pipe: never use Connection.send/recv (pickle). No RPC
payloads are persisted. The endpoint descriptor contains a per-launch DPAPI
encrypted authentication key, bound to both runtime and proxy process lifetimes.
Timeouts of mutations are uncertain and are never automatically resubmitted.
"""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import math
from multiprocessing.connection import Listener
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time
from uuid import UUID, uuid4
from .catalog_origin import validated_origin

try:
    from .instances import process_identity
    from .providers import _crypt_secret
except ImportError:
    from instances import process_identity
    from providers import _crypt_secret


_VERSION = 1
_MAX_REQUEST = 16 * 1024
_MAX_RESPONSE = 256 * 1024
_MAX_TIMEOUT = 120.0
_PREFIX = 'codex-manager-admin:'
_PIPE_PREFIX = r'\\.\pipe\Codex.ControlCenter.Admin.'
_LOCAL_METHODS = frozenset({'manager/maintenance/acquire', 'manager/maintenance/status', 'manager/maintenance/release'})
_METHODS = frozenset({'thread/managedCloseIdle', 'thread/managedReloadBinding', 'thread/managedIdleStatus',
                      'thread/loaded/list', 'thread/read'}) | _LOCAL_METHODS
_MUTATIONS = frozenset({'thread/managedCloseIdle', 'thread/managedReloadBinding',
                        'manager/maintenance/acquire', 'manager/maintenance/release'})
_ERRORS = frozenset({'invalid_request', 'unavailable', 'stale_runtime', 'not_ready',
                     'busy', 'timeout', 'runtime_error', 'invalid_response', 'closed'})
_CLOSE_LOCK = threading.Lock()
_ACTIVITY_FIELDS = {'activeProcessCount': 'active_process_count', 'activeToolCount': 'active_tool_count',
                    'activeTurnCount': 'active_turn_count', 'activeChildCount': 'active_child_count',
                    'queueUnknownCount': 'queue_state_unknown_count'}
_IDLE_BLOCKERS = frozenset({'threadNotLoaded', 'sourceBindingUnknown', 'sourceMismatch', 'agentActive',
    'pendingClientRequest', 'undrainedRuntimeEvents', 'queuedUserInput', 'queueStateUnknown',
    'parentRelationshipUnknown', 'actorClosing', 'subtreeChanged', 'subtreeStateUnknown', 'actorChanged',
    'activeTurn', 'pendingInputOrAgentMail', 'realtimeConversation', 'backgroundTerminal', 'elicitation',
    'hookWork', 'queuedCoreSubmission', 'pendingInternalActivity', 'coldDescendantRequiresWriterClaim',
    'internalActorWorkOrUnverifiedCleanup'})


def _close_connection(connection):
    # Connection.close is not itself thread-safe on Windows. A deadline and
    # exchange completion can race; never close the same native handle twice.
    with _CLOSE_LOCK:
        try:
            connection.close()
        except OSError:
            pass


class AdminError(RuntimeError):
    """A payload-free error; ``uncertain`` forbids interpreting it as release."""

    def __init__(self, code, *, uncertain=False, rpc_code=None):
        self.code = code if code in _ERRORS else 'unavailable'
        self.uncertain = bool(uncertain)
        self.rpc_code = rpc_code if type(rpc_code) is int else None
        super().__init__('Managed runtime administration: ' + self.code + '.')


def _uuid(value):
    if not isinstance(value, str):
        raise AdminError('invalid_request')
    try:
        canonical = str(UUID(value))
    except ValueError:
        raise AdminError('invalid_request') from None
    if value != canonical:
        raise AdminError('invalid_request')
    return canonical


def _timeout(value):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not 0 < value <= _MAX_TIMEOUT):
        raise AdminError('invalid_request')
    return float(value)


def _cursor(value):
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_+=/:.\-]{1,1024}', value):
        raise AdminError('invalid_request')
    return value


def validate_request(method, params):
    """Reject extra arguments; admin is not a general-purpose RPC tunnel."""
    if not isinstance(method, str) or method not in _METHODS or not isinstance(params, dict):
        raise AdminError('invalid_request')
    if method in _LOCAL_METHODS:
        if method == 'manager/maintenance/status' and not params:
            return {}
        required = {'transactionId'} if method == 'manager/maintenance/acquire' else {'transactionId', 'leaseToken'}
        if set(params) != required:
            raise AdminError('invalid_request')
        return {key: _uuid(params[key]) for key in required}
    if method == 'thread/managedReloadBinding':
        required = {'threadId', 'hostId', 'sourceStoreId', 'ownerProfileId', 'ownershipEpoch', 'recordRevision'}
        if set(params) != required:
            raise AdminError('invalid_request')
        host, source = params['hostId'], params['sourceStoreId']
        if not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:\-]{0,159}', host):
            raise AdminError('invalid_request')
        if not isinstance(source, str) or not source.startswith('manager:'):
            raise AdminError('invalid_request')
        source = 'manager:' + _uuid(source[8:])
        for key in ('ownershipEpoch', 'recordRevision'):
            if type(params[key]) is not int or not 0 < params[key] <= 2**64 - 1:
                raise AdminError('invalid_request')
        return {'threadId': _uuid(params['threadId']), 'hostId': host,
                'sourceStoreId': source, 'ownerProfileId': _uuid(params['ownerProfileId']),
                'ownershipEpoch': params['ownershipEpoch'], 'recordRevision': params['recordRevision']}
    if method == 'thread/loaded/list':
        if set(params) - {'cursor', 'limit'}:
            raise AdminError('invalid_request')
        limit = params.get('limit', 100)
        if type(limit) is not int or not 1 <= limit <= 256:
            raise AdminError('invalid_request')
        return {'cursor': _cursor(params.get('cursor')), 'limit': limit}
    allowed = {'threadId', 'includeTurns'} if method == 'thread/read' else {'threadId'}
    if set(params) - allowed or 'threadId' not in params:
        raise AdminError('invalid_request')
    result = {'threadId': _uuid(params['threadId'])}
    if method == 'thread/read':
        if params.get('includeTurns', False) is not False:
            raise AdminError('invalid_request')
        result['includeTurns'] = False
    return result


def _id_list(value):
    if not isinstance(value, list) or len(value) > 1024:
        raise AdminError('invalid_response')
    result = [_uuid(item) for item in value]
    if len(result) != len(set(result)):
        raise AdminError('invalid_response')
    return result


def sanitize_result(method, params, value):
    """Expose only typed lifecycle metadata, never histories or error bodies."""
    try:
        if not isinstance(value, dict):
            raise ValueError()
        if method in _LOCAL_METHODS:
            booleans = ('held', 'frontendMutationBlocked', 'initialized', 'streamComplete', 'accountReady', 'connected')
            if any(type(value.get(key)) is not bool for key in booleans):
                raise ValueError()
            result = {key: value[key] for key in booleans}
            result['generation'] = _uuid(value['generation'])
            result['transactionId'] = _uuid(value['transactionId']) if value.get('transactionId') is not None else None
            for key in ('pendingMutationCount', 'pendingApprovalCount', *_ACTIVITY_FIELDS):
                if type(value.get(key)) is not int or not 0 <= value[key] <= 1_000_000:
                    raise ValueError()
                result[key] = value[key]
            if method == 'manager/maintenance/acquire':
                if not result['held'] or result['transactionId'] != params['transactionId']:
                    raise ValueError()
                result['leaseToken'] = _uuid(value['leaseToken'])
            elif method == 'manager/maintenance/release' and result['held']:
                raise ValueError()
            elif method == 'manager/maintenance/status' and params and result['transactionId'] != params['transactionId']:
                raise ValueError()
            if 'sshBinding' in value:
                binding = value['sshBinding']
                if not isinstance(binding, dict):
                    raise ValueError()
                alias, revision, fingerprint = (binding.get(key) for key in ('hostAlias', 'revision', 'accountFingerprint'))
                if (not isinstance(alias, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', alias)
                        or not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{64}', revision)
                        or (fingerprint is not None and (not isinstance(fingerprint, str)
                                                       or not re.fullmatch(r'[0-9a-f]{64}', fingerprint)))
                        or binding.get('authState') not in ('wait_initialize', 'disabled', 'authenticating', 'ready', 'login_needed')):
                    raise ValueError()
                result['sshBinding'] = {'profileId': _uuid(binding['profileId']), 'hostAlias': alias,
                                        'revision': revision, 'accountFingerprint': fingerprint,
                                        'authState': binding['authState']}
            return result
        if method == 'thread/managedIdleStatus':
            ids = _id_list(value['observedThreadIds'])
            host, source = value.get('hostId'), value.get('sourceStoreId')
            if (value.get('threadId') != params['threadId'] or params['threadId'] not in ids
                    or value.get('proofScope') != 'advisory' or type(value.get('idle')) is not bool
                    or not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:\-]{0,159}', host)
                    or not isinstance(source, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:\-]{0,159}', source)):
                raise ValueError()
            blockers = value['blockers']
            if not isinstance(blockers, list) or len(blockers) > 8192:
                raise ValueError()
            clean = []
            for blocker in blockers:
                if not isinstance(blocker, dict) or blocker.get('threadId') not in ids:
                    raise ValueError()
                kind = blocker.get('kind')
                if not isinstance(kind, str) or kind not in _IDLE_BLOCKERS:
                    raise ValueError()
                clean.append({'threadId': blocker['threadId'], 'kind': kind})
            if value['idle'] != (not clean):
                raise ValueError()
            return {'threadId': params['threadId'], 'hostId': host, 'sourceStoreId': source,
                    'observedThreadIds': ids, 'idle': value['idle'], 'blockers': clean, 'proofScope': 'advisory'}
        if method == 'thread/managedReloadBinding':
            if any(value.get(key) != expected for key, expected in params.items()) or value.get('bindingReloaded') is not True:
                raise ValueError()
            ids = _id_list(value['activatedThreadIds'])
            if params['threadId'] not in ids:
                raise ValueError()
            return {**params, 'activatedThreadIds': ids, 'bindingReloaded': True}
        if method == 'thread/managedCloseIdle':
            ids = _id_list(value['closedThreadIds'])
            if (value.get('threadId') != params['threadId']
                    or value.get('writerReleaseVerified') is not True
                    or params['threadId'] not in ids):
                raise ValueError()
            return {'threadId': params['threadId'], 'closedThreadIds': ids,
                    'writerReleaseVerified': True}
        if method == 'thread/loaded/list':
            return {'data': _id_list(value['data']), 'nextCursor': _cursor(value.get('nextCursor'))}
        if method == 'thread/read':
            thread = value['thread']
            if not isinstance(thread, dict) or thread.get('id') != params['threadId']:
                raise ValueError()
            status = thread['status']
            if not isinstance(status, dict) or status.get('type') not in {'idle', 'active', 'notLoaded', 'systemError'}:
                raise ValueError()
            clean_status = {'type': status['type']}
            if status['type'] == 'active':
                flags = status.get('activeFlags', [])
                if (not isinstance(flags, list) or len(flags) > 2
                        or any(flag not in ('waitingOnApproval', 'waitingOnUserInput') for flag in flags)):
                    raise ValueError()
                clean_status['activeFlags'] = flags
            clean_thread = {'id': params['threadId'], 'status': clean_status}
            origin = validated_origin(thread)
            if origin is not None:
                clean_thread.update(extra={'managedRecord': origin}, path=None, canAcceptDirectInput=False)
            for key in ('sessionId', 'parentThreadId', 'forkedFromId'):
                if key in thread:
                    clean_thread[key] = _uuid(thread[key]) if thread[key] is not None else None
            return {'thread': clean_thread}
        raise ValueError()
    except (AdminError, KeyError, TypeError, ValueError):
        raise AdminError('invalid_response', uncertain=method in _MUTATIONS) from None


def _encode(value, limit):
    try:
        body = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode('utf-8')
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise AdminError('invalid_request') from None
    if len(body) > limit:
        raise AdminError('invalid_request')
    return body


def _decode(body):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    try:
        value = json.loads(body, object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise AdminError('invalid_request') from None


class AdminRpcBroker:
    """``send_request(message, deadline, lease)`` checks admission under its lock."""

    def __init__(self, send_request):
        self._send = send_request
        self._lock = threading.Lock()
        self._prefix = _PREFIX + uuid4().hex + ':'
        self._pending = {}
        self._closed = False

    @staticmethod
    def is_reserved_request(message):
        private_id = isinstance(message.get('id'), str) and message['id'].startswith(_PREFIX)
        method = message.get('method')
        return private_id or (isinstance(method, str) and (
            method.startswith('manager/maintenance/') or method.startswith('thread/managed')))

    def consume_runtime(self, message):
        request_id = message.get('id')
        if not isinstance(request_id, str) or not request_id.startswith(self._prefix):
            return False
        # Late responses must stay hidden after timeout. Only response envelopes
        # settle a pending request; a server request must not fabricate proof.
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is not None and 'method' not in message:
                if 'error' in message:
                    error = message['error']
                    code = error.get('code') if isinstance(error, dict) else None
                    pending['error'] = AdminError('runtime_error',
                        uncertain=pending['method'] in _MUTATIONS, rpc_code=code)
                elif 'result' in message:
                    try:
                        pending['result'] = sanitize_result(pending['method'], pending['params'], message['result'])
                    except AdminError as error:
                        pending['error'] = error
                else:
                    pending['error'] = AdminError('invalid_response', uncertain=pending['method'] in _MUTATIONS)
                pending['event'].set()
        return True

    def request(self, method, params, timeout=15, maintenance_lease=None):
        params = validate_request(method, params)
        if method in _LOCAL_METHODS:
            raise AdminError('invalid_request')
        timeout = _timeout(timeout)
        deadline = time.monotonic() + timeout
        request_id = self._prefix + uuid4().hex
        pending = {'method': method, 'params': params, 'event': threading.Event()}
        with self._lock:
            if self._closed:
                raise AdminError('closed')
            if len(self._pending) >= 4:
                raise AdminError('busy')
            self._pending[request_id] = pending
        try:
            try:
                self._send({'id': request_id, 'method': method, 'params': params}, deadline, maintenance_lease)
            except AdminError:
                raise
            except (OSError, ValueError, RuntimeError):
                raise AdminError('unavailable', uncertain=method in _MUTATIONS) from None
            if not pending['event'].wait(max(0, deadline - time.monotonic())):
                raise AdminError('timeout', uncertain=method in _MUTATIONS)
            if 'error' in pending:
                raise pending['error']
            return pending['result']
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def close(self):
        with self._lock:
            self._closed = True
            for pending in self._pending.values():
                pending['error'] = AdminError('closed', uncertain=pending['method'] in _MUTATIONS)
                pending['event'].set()

    def pending_mutation_count(self):
        with self._lock:
            return sum(pending['method'] in _MUTATIONS for pending in self._pending.values())


class MaintenanceBarrier:
    """A process-local frontend barrier, not proof that runtime writers stopped.

    Every method is called under the proxy protocol lock. Existing response
    messages and explicit interrupts can drain; new writes and unknown methods
    are rejected while held. A replacement proxy always starts with no lease.
    """

    _READS = frozenset({
        'account/read', 'account/rateLimits/read', 'thread/read', 'thread/list',
        'thread/loaded/list', 'thread/turns/list', 'thread/items/list', 'thread/timeline/list',
        'thread/queue/list', 'thread/backgroundTerminals/list', 'thread/goal/get',
        'thread/section/list', 'thread/search', 'thread/searchOccurrences',
        'model/list', 'config/read', 'config/requirements/read', 'skills/list',
        'mcpServerStatus/list', 'app/list', 'plugin/list', 'experimentalFeature/list',
        'fs/readFile', 'fs/readDirectory', 'fs/getMetadata',
    })
    _CANCELS = frozenset({'turn/interrupt', 'thread/realtime/stop', 'process/kill',
                         'command/exec/terminate', 'thread/backgroundTerminals/terminate'})

    def __init__(self, generation):
        self.generation = _uuid(generation)
        self.transaction_id = None
        self._token = None
        self._pending = set()
        self._server_pending = set()

    @staticmethod
    def _key(message):
        value = message.get('id')
        return (type(value).__name__, value) if type(value) in (int, str) else None

    def observe_client(self, message):
        key = self._key(message)
        method = message.get('method')
        if method is None:
            self._server_pending.discard(key)
        elif key is not None and method not in self._READS and method not in self._CANCELS:
            if not AdminRpcBroker.is_reserved_request(message):
                self._pending.add(key)

    def observe_runtime(self, message):
        key = self._key(message)
        if message.get('method') is None:
            self._pending.discard(key)
        elif key is not None:
            self._server_pending.add(key)
        if message.get('method') == 'serverRequest/resolved':
            params = message.get('params')
            if isinstance(params, dict):
                self._server_pending.discard(self._key({'id': params.get('requestId')}))

    def blocks(self, message):
        method = message.get('method')
        if self.transaction_id is None or method is None:
            return False
        return not self.allows_drain(method)

    @classmethod
    def allows_drain(cls, method):
        """Reads and cancellation can proceed while admission of new work stops."""
        return method in cls._READS or method in cls._CANCELS

    def authorize_admin(self, method, lease):
        if self.transaction_id is not None and method in _MUTATIONS:
            if (not isinstance(lease, dict) or lease.get('transactionId') != self.transaction_id
                    or lease.get('leaseToken') != self._token):
                raise AdminError('busy')

    def status(self, observer, account_ready):
        result = {'held': self.transaction_id is not None, 'transactionId': self.transaction_id,
                'generation': self.generation, 'frontendMutationBlocked': self.transaction_id is not None,
                'pendingMutationCount': len(self._pending), 'pendingApprovalCount': len(self._server_pending),
                'initialized': observer.get('initialized') is True,
                'streamComplete': observer.get('stream_complete') is True,
                'connected': observer.get('connected') is True, 'accountReady': account_ready is True}
        for name, source in _ACTIVITY_FIELDS.items():
            value = observer.get(source)
            # Missing observations never look like zero active work.
            result[name] = value if type(value) is int and value >= 0 else 1_000_000
        return result

    def request(self, method, params, observer, account_ready):
        params = validate_request(method, params)
        if method not in _LOCAL_METHODS:
            raise AdminError('invalid_request')
        status = self.status(observer, account_ready)
        if method == 'manager/maintenance/acquire':
            if not all(status[key] for key in ('initialized', 'streamComplete', 'connected', 'accountReady')):
                raise AdminError('not_ready')
            if self.transaction_id is not None and self.transaction_id != params['transactionId']:
                raise AdminError('busy')
            if self.transaction_id is None:
                self.transaction_id, self._token = params['transactionId'], str(uuid4())
            return {**self.status(observer, account_ready), 'leaseToken': self._token}
        if params:
            if self.transaction_id != params['transactionId'] or self._token != params['leaseToken']:
                raise AdminError('stale_runtime')
        if method == 'manager/maintenance/release':
            self.transaction_id, self._token = None, None
        return self.status(observer, account_ready)


def _descriptor_path(root, profile_id, generation):
    root = Path(root).resolve()
    relative = Path('work/control-center/instances') / _uuid(profile_id) / 'runtime-admin' / (_uuid(generation) + '.json')
    path = root / relative
    current = root
    for part in relative.parts[:-1]:
        current /= part
        if current.exists() and current.lstat().st_file_attributes & 0x400:
            raise AdminError('unavailable')
    if not path.resolve().is_relative_to(root):
        raise AdminError('unavailable')
    return path


def _private_directory(path):
    """Protect the auth descriptor directory for this Windows user and SYSTEM."""
    path.mkdir(parents=True, exist_ok=True)
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    advapi.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    token = wintypes.HANDLE()
    sid_text = wintypes.LPWSTR()
    descriptor = ctypes.c_void_p()
    try:
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise AdminError('unavailable')
        length = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
        if not 0 < length.value < 65536:
            raise AdminError('unavailable')
        user = ctypes.create_string_buffer(length.value)
        if not advapi.GetTokenInformation(token, 1, user, length, ctypes.byref(length)):
            raise AdminError('unavailable')
        sid = ctypes.cast(user, ctypes.POINTER(ctypes.c_void_p))[0]
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
            raise AdminError('unavailable')
        sddl = 'D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;' + sid_text.value + ')'
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
            raise AdminError('unavailable')
        if not advapi.SetFileSecurityW(str(path), 0x80000004, descriptor):
            raise AdminError('unavailable')
    finally:
        if descriptor:
            kernel.LocalFree(descriptor)
        if sid_text:
            kernel.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
        if token:
            kernel.CloseHandle(token)


def _identity(pid):
    if type(pid) is not int or pid <= 0:
        raise AdminError('stale_runtime')
    identity = process_identity(pid)
    if not identity:
        raise AdminError('stale_runtime')
    return {'pid': pid, 'created': identity['process_created']}


def _matches(identity):
    try:
        return _identity(identity['pid']) == identity
    except (AdminError, TypeError, KeyError):
        return False


def _connect_pipe(address, deadline, abandoned):
    # The stock PipeClient has its own long retry interval. Honor this request's
    # absolute deadline, including a busy endpoint, before authentication starts.
    import _winapi
    from multiprocessing.connection import PipeConnection
    while not abandoned.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AdminError('timeout')
        try:
            _winapi.WaitNamedPipe(address, max(1, min(100, int(remaining * 1000))))
            handle = _winapi.CreateFile(address, _winapi.GENERIC_READ | _winapi.GENERIC_WRITE,
                0, _winapi.NULL, _winapi.OPEN_EXISTING, _winapi.FILE_FLAG_OVERLAPPED, _winapi.NULL)
        except OSError as error:
            if error.winerror in (_winapi.ERROR_SEM_TIMEOUT, _winapi.ERROR_PIPE_BUSY):
                continue
            raise AdminError('unavailable') from None
        try:
            _winapi.SetNamedPipeHandleState(handle, _winapi.PIPE_READMODE_MESSAGE, None, None)
            return PipeConnection(handle)
        except BaseException:
            _winapi.CloseHandle(handle)
            raise
    raise AdminError('timeout')


class AdminServer:
    """``dispatch(method, params, timeout, maintenance_lease)`` returns metadata."""

    def __init__(self, root, profile_id, generation, runtime_pid, dispatch):
        if os.name != 'nt':
            raise AdminError('unavailable')
        self.profile_id, self.generation = _uuid(profile_id), _uuid(generation)
        self.path = _descriptor_path(root, profile_id, generation)
        self.runtime = _identity(runtime_pid)
        self.proxy = _identity(os.getpid())
        self._dispatch = dispatch
        self._key = secrets.token_bytes(32)
        self._address = _PIPE_PREFIX + uuid4().hex
        self._listener = None
        self._stop = threading.Event()
        self._slots = threading.BoundedSemaphore(4)
        self._connections = set()
        self._lock = threading.Lock()

    def start(self):
        if self._listener is not None:
            raise AdminError('busy')
        _private_directory(self.path.parent)
        if self.path.exists():
            try:
                old = _decode(self.path.read_bytes())
                if _matches(old.get('proxy')) or _matches(old.get('runtime')):
                    raise AdminError('busy')
            except (OSError, AdminError) as error:
                if isinstance(error, AdminError) and error.code == 'busy':
                    raise
        # Authentication runs in bounded client workers so an unauthenticated
        # connection cannot monopolize the only accept loop.
        self._listener = Listener(self._address, family='AF_PIPE', authkey=None)
        temporary = None
        try:
            descriptor = {'version': _VERSION, 'profile_id': self.profile_id,
                          'generation': self.generation, 'runtime': self.runtime,
                          'proxy': self.proxy, 'address': self._address,
                          'key_dpapi': base64.b64encode(_crypt_secret(self._key, True)).decode('ascii')}
            handle, temporary = tempfile.mkstemp(prefix='endpoint-', suffix='.tmp', dir=self.path.parent)
            with os.fdopen(handle, 'wb') as output:
                output.write(_encode(descriptor, _MAX_REQUEST))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            self._listener.close()
            self._listener = None
            raise
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)
        threading.Thread(target=self._accept, name='codex-admin-accept', daemon=True).start()
        return self

    def _accept(self):
        while not self._stop.is_set():
            try:
                connection = self._listener.accept()
            except (OSError, TypeError, AttributeError):
                return
            if self._stop.is_set() or not self._slots.acquire(blocking=False):
                _close_connection(connection)
                continue
            with self._lock:
                self._connections.add(connection)
            threading.Thread(target=self._serve, args=(connection,), daemon=True).start()

    def _serve(self, connection):
        from multiprocessing.connection import deliver_challenge, answer_challenge
        # Bound handshake/request receive time independently of dispatch time.
        timer = threading.Timer(5, _close_connection, args=(connection,))
        timer.daemon = True
        timer.start()
        request_id = None
        method = None
        sent = False
        deadline = time.monotonic() + 5
        try:
            deliver_challenge(connection, self._key)
            answer_challenge(connection, self._key)
            envelope = _decode(connection.recv_bytes(_MAX_REQUEST))
            timer.cancel()
            if set(envelope) != {'version', 'profile_id', 'generation', 'runtime', 'request_id', 'deadline', 'method', 'params', 'maintenance_lease'}:
                raise AdminError('invalid_request')
            request_id = _uuid(envelope['request_id'])
            if (envelope['version'] != _VERSION or envelope['profile_id'] != self.profile_id
                    or envelope['generation'] != self.generation or envelope['runtime'] != self.runtime
                    or not _matches(self.runtime) or not _matches(self.proxy) or self._stop.is_set()):
                raise AdminError('stale_runtime')
            deadline = envelope['deadline']
            if type(deadline) not in (int, float) or not math.isfinite(deadline):
                raise AdminError('invalid_request')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AdminError('timeout')
            timeout = _timeout(remaining)
            method = envelope['method']
            params = validate_request(method, envelope['params'])
            lease = envelope['maintenance_lease']
            if lease is not None:
                lease = validate_request('manager/maintenance/status', lease)
                if not lease:
                    raise AdminError('invalid_request')
            sent = True
            result = self._dispatch(method, params, timeout, lease)
            if not _matches(self.runtime) or self._stop.is_set():
                raise AdminError('stale_runtime', uncertain=method in _MUTATIONS)
            result = sanitize_result(method, params, result)
            response = {'ok': True, 'result': result}
        except AdminError as error:
            response = {'ok': False, 'error': {'code': error.code, 'uncertain': error.uncertain,
                                               'rpc_code': error.rpc_code}}
        except Exception:
            response = {'ok': False, 'error': {'code': 'unavailable',
                                               'uncertain': sent and method in _MUTATIONS, 'rpc_code': None}}
        finally:
            timer.cancel()
        try:
            if request_id is not None:
                response.update(version=_VERSION, request_id=request_id,
                                generation=self.generation, runtime=self.runtime)
                timer = threading.Timer(max(.01, min(5, deadline - time.monotonic())),
                                        _close_connection, args=(connection,))
                timer.daemon = True
                timer.start()
                connection.send_bytes(_encode(response, _MAX_RESPONSE))
        except (OSError, AdminError, TypeError):
            pass
        finally:
            timer.cancel()
            _close_connection(connection)
            with self._lock:
                self._connections.discard(connection)
            self._slots.release()

    def close(self):
        self._stop.set()
        if self._listener is not None:
            self._listener.close()
        with self._lock:
            for connection in tuple(self._connections):
                _close_connection(connection)
        try:
            descriptor = _decode(self.path.read_bytes())
            if descriptor.get('address') == self._address:
                self.path.unlink()
        except (OSError, AdminError):
            pass


class AdminClient:
    """Connect only to this profile generation; never spawn or retry a runtime."""

    def __init__(self, root, profile_id, generation):
        if os.name != 'nt':
            raise AdminError('unavailable')
        self.profile_id, self.generation = _uuid(profile_id), _uuid(generation)
        self.path = _descriptor_path(root, profile_id, generation)
        self._maintenance_lease = None

    def _descriptor(self):
        try:
            if self.path.stat().st_size > _MAX_REQUEST or self.path.lstat().st_file_attributes & 0x400:
                raise AdminError('unavailable')
            descriptor = _decode(self.path.read_bytes())
            if (descriptor.get('version') != _VERSION or descriptor.get('profile_id') != self.profile_id
                    or descriptor.get('generation') != self.generation
                    or not _matches(descriptor.get('runtime')) or not _matches(descriptor.get('proxy'))):
                raise AdminError('stale_runtime')
            address = descriptor.get('address')
            if not isinstance(address, str) or not re.fullmatch(re.escape(_PIPE_PREFIX) + r'[0-9a-f]{32}', address):
                raise AdminError('unavailable')
            key = _crypt_secret(base64.b64decode(descriptor['key_dpapi'], validate=True), False)
            if len(key) != 32:
                raise AdminError('unavailable')
            return descriptor, key
        except AdminError:
            raise
        except (OSError, ValueError, KeyError, TypeError):
            raise AdminError('unavailable') from None

    def identities(self):
        """Validated process birth identities, without endpoint or credentials."""
        descriptor, _ = self._descriptor()
        result = {'generation': self.generation}
        for kind in ('runtime', 'proxy'):
            expected = descriptor[kind]
            current = process_identity(expected['pid'])
            if current is None or current.get('process_created') != expected['created']:
                raise AdminError('stale_runtime')
            result[kind] = {**expected, 'executable_path': current['executable_path']}
        return result

    def request(self, method, params, timeout=15, *, maintenance_lease=None):
        params = validate_request(method, params)
        timeout = _timeout(timeout)
        deadline = time.monotonic() + timeout
        descriptor, key = self._descriptor()
        request_id = str(uuid4())
        lease = maintenance_lease if maintenance_lease is not None else self._maintenance_lease
        if lease is not None:
            lease = validate_request('manager/maintenance/status', lease)
            if not lease:
                raise AdminError('invalid_request')
        envelope = {'version': _VERSION, 'profile_id': self.profile_id,
                    'generation': self.generation, 'runtime': descriptor['runtime'],
                    'request_id': request_id, 'deadline': deadline,
                    'method': method, 'params': params, 'maintenance_lease': lease}
        wire = _encode(envelope, _MAX_REQUEST)
        done = threading.Event()
        abandoned = threading.Event()
        state = {'sent': False}
        lock = threading.Lock()

        def exchange():
            from multiprocessing.connection import answer_challenge, deliver_challenge
            connection = None
            try:
                connection = _connect_pipe(descriptor['address'], deadline, abandoned)
                with lock:
                    if abandoned.is_set() or time.monotonic() >= deadline:
                        return
                    state['connection'] = connection
                answer_challenge(connection, key)
                deliver_challenge(connection, key)
                with lock:
                    if abandoned.is_set() or time.monotonic() >= deadline:
                        return
                    # Once send begins the mutating outcome may be uncertain.
                    state['sent'] = True
                connection.send_bytes(wire)
                if not connection.poll(max(0, deadline - time.monotonic())):
                    raise AdminError('timeout', uncertain=method in _MUTATIONS)
                try:
                    response = _decode(connection.recv_bytes(_MAX_RESPONSE))
                except AdminError:
                    raise AdminError('invalid_response', uncertain=method in _MUTATIONS) from None
                if (response.get('version') != _VERSION or response.get('request_id') != request_id
                        or response.get('generation') != self.generation
                        or response.get('runtime') != descriptor['runtime']
                        or not _matches(descriptor['runtime']) or not _matches(descriptor['proxy'])):
                    raise AdminError('stale_runtime', uncertain=method in _MUTATIONS)
                if response.get('ok') is not True:
                    error = response.get('error')
                    if not isinstance(error, dict):
                        raise AdminError('invalid_response', uncertain=method in _MUTATIONS)
                    raise AdminError(error.get('code'), uncertain=error.get('uncertain') is True,
                                     rpc_code=error.get('rpc_code'))
                state['result'] = sanitize_result(method, params, response.get('result'))
            except AdminError as error:
                state['error'] = error
            except Exception:
                state['error'] = AdminError('unavailable', uncertain=state['sent'] and method in _MUTATIONS)
            finally:
                if connection is not None:
                    _close_connection(connection)
                done.set()

        threading.Thread(target=exchange, name='codex-admin-client', daemon=True).start()
        if not done.wait(max(0, deadline - time.monotonic())):
            with lock:
                abandoned.set()
                connection = state.get('connection')
                if connection is not None:
                    _close_connection(connection)
                uncertain = state['sent'] and method in _MUTATIONS
            raise AdminError('timeout', uncertain=uncertain)
        if 'error' in state:
            raise state['error']
        if 'result' not in state:
            raise AdminError('timeout')
        result = state['result']
        if method == 'manager/maintenance/acquire':
            self._maintenance_lease = {'transactionId': result['transactionId'], 'leaseToken': result['leaseToken']}
        elif method == 'manager/maintenance/status' and params:
            self._maintenance_lease = dict(params)
        elif method == 'manager/maintenance/release':
            self._maintenance_lease = None
        return result
