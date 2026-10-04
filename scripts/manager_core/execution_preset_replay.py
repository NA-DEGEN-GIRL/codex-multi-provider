"""Nonblocking replay of explicit task presets before a managed SSH turn."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import time
from uuid import UUID, uuid4

from .proxy_auth import AuthProxyResult

PREFIX = '__codex_manager_preset_replay:'
ERROR_CODE = -32045


def _reference(value):
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {'id', 'revision'}
            or str(UUID(value['id'])) != value['id'] or type(value['revision']) is not int or value['revision'] < 1):
        raise ValueError('invalid preset reference')
    return dict(value)


def _snapshot(path, maximum, *, missing=None):
    """Read one atomic snapshot, with no blocking Store/file lock in the pump."""
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError('unsafe preset snapshot path')
    try:
        if path.stat().st_size > maximum:
            raise ValueError('oversized preset snapshot')
        with path.open('rb') as stream:
            data = stream.read(maximum + 1)
    except FileNotFoundError:
        if missing is not None:
            return deepcopy(missing)
        raise
    if len(data) > maximum:
        raise ValueError('oversized preset snapshot')
    return json.loads(data)


class SelectionSource:
    """Frozen connection authority plus current explicit manager task bindings."""
    def __init__(self, root, generation, authority):
        from .store import Store
        store = Store(root)
        self.state_path = store.path
        self.presets_path = store.directory / 'execution-presets.json'
        self.authority = deepcopy(authority)
        self.profile_id = str(UUID(authority['profile_id']))
        self.generation = str(UUID(generation))
        self.binding_path = store.directory / 'profiles' / self.profile_id / 'ssh-bindings.json'

    def __call__(self, thread_id):
        from .execution_presets import MAX_BYTES, _task_identity
        from .ssh_shim import validate_binding
        thread_id = str(UUID(thread_id))
        state = _snapshot(self.state_path, 16 * 1024 * 1024)
        profile = next((value for value in state['profiles'] if value['id'] == self.profile_id), None)
        if not profile or profile.get('generation') != self.generation or profile.get('removed_at'):
            raise ValueError('profile generation changed')
        host_id = self.authority['host_id']
        alias = host_id.removeprefix('ssh:')
        saved = [value for value in profile.get('remote_bindings', []) if value.get('alias') == alias
                 and value.get('revision') == self.authority['revision']]
        if (len(saved) != 1 or saved[0].get('prepared') is not True or saved[0].get('execution_presets_version') != 1
                or saved[0].get('host_identity') != self.authority['host_identity']):
            raise ValueError('prepared SSH binding changed')
        launch = _snapshot(self.binding_path, 256000)
        bindings = [value for value in launch['bindings'] if value.get('alias') == alias]
        if (launch.get('profile_id') != self.profile_id or launch.get('generation') != self.generation
                or len(bindings) != 1 or validate_binding(bindings[0], self.profile_id) != validate_binding(saved[0], self.profile_id)):
            raise ValueError('active SSH binding changed')
        presets = _snapshot(self.presets_path, MAX_BYTES, missing={'version': 1, 'bindings': {}})
        if presets.get('version') != 1 or not isinstance(presets.get('bindings'), dict):
            raise ValueError('invalid preset snapshot')
        key, identity = _task_identity(self.profile_id, host_id, thread_id)
        bound = presets['bindings'].get(key)
        if bound is None:
            return False, None  # Never apply a new-task default to a loaded task.
        if any(bound.get(key) != value for key, value in identity.items()):
            raise ValueError('task preset identity changed')
        return True, _reference({'id': bound['preset_id'], 'revision': bound['revision']} if bound.get('preset_id') else None)


def selection_source(source_environment, event, auth):
    authority = getattr(auth, 'authority', None)
    root, generation = source_environment.get('CODEX_MANAGER_ROOT'), source_environment.get('CODEX_MANAGER_GENERATION')
    if not root or not generation or not isinstance(authority, dict):
        return None
    if (authority.get('profile_id') != event['profile_id'] or authority.get('host_id') != 'ssh:' + event['alias']
            or authority.get('revision') != event['revision']):
        raise ValueError('preset replay authority mismatch')
    return SelectionSource(root, generation, authority)


class ExecutionPresetReplay:
    """One serialized protocol-pump state machine; no threads or network calls."""
    def __init__(self, source, *, clock=time.monotonic, timeout=15, max_pending=8, max_bytes=8 * 1024 * 1024):
        self.source, self.clock = source, clock
        self.timeout, self.max_pending, self.max_bytes = timeout, max_pending, max_bytes
        self.prefix = PREFIX + uuid4().hex + ':'
        self.pending = {}
        self.bytes = 0
        self.closed = False

    @staticmethod
    def reserved(message):
        return isinstance(message.get('id'), str) and message['id'].startswith(PREFIX)

    @staticmethod
    def _error(message, text):
        result = AuthProxyResult()
        if type(message.get('id')) in (str, int):
            result.frontend.append({'id': message['id'], 'error': {'code': ERROR_CODE, 'message': text}})
        return result

    def pending_count(self):
        return len(self.pending)

    def _finish(self, thread_id):
        pending = self.pending.pop(thread_id)
        self.bytes -= pending['bytes']
        return pending

    def frontend(self, message, *, ready, applied):
        if message.get('method') != 'turn/start':
            return None
        try:
            thread_id = (message.get('params') or {}).get('threadId')
            if self.closed:
                raise ValueError('closed')
            for pending_thread, pending in list(self.pending.items()):
                original = pending['message']
                if type(message.get('id')) is type(original.get('id')) and message.get('id') == original.get('id'):
                    if message == original:
                        return AuthProxyResult()  # Idempotent transport retry.
                    self._finish(pending_thread)
                    return self._error(message, '전송 중인 요청이 변경되었습니다. 새 요청으로 다시 시도하세요.')
            if thread_id in self.pending:
                return self._error(message, '이 작업의 프리셋을 확인하고 있습니다. 확인이 끝나면 다시 시도하세요.')
            explicit, selected = self.source(thread_id)
            if not explicit:
                return None
            selected = _reference(selected)
            if not ready:
                raise ValueError('not ready')
            if type(message.get('id')) not in (str, int):
                raise ValueError('turn request id required')
            if thread_id in applied and applied[thread_id] == selected:
                return None
            size = len(json.dumps(message, ensure_ascii=False, allow_nan=False).encode('utf-8'))
            if len(self.pending) >= self.max_pending or self.bytes + size > self.max_bytes:
                return self._error(message, '여러 작업의 프리셋을 확인하고 있습니다. 잠시 후 다시 시도하세요.')
            request_id = self.prefix + uuid4().hex
            self.pending[thread_id] = dict(message=deepcopy(message), selection=selected, request_id=request_id,
                ack=False, notified=False, deadline=self.clock() + self.timeout, bytes=size)
            self.bytes += size
            return AuthProxyResult(runtime=[{'id': request_id, 'method': 'thread/settings/update',
                'params': {'threadId': thread_id, 'executionPreset': selected}}])
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, AttributeError):
            return self._error(message, '저장된 SSH 프리셋을 확인하지 못했습니다. 다시 연결하거나 프리셋을 다시 선택하세요.')

    def _release(self, thread_id, *, ready):
        pending = self.pending[thread_id]
        if not pending['ack'] or not pending['notified']:
            return AuthProxyResult()
        pending = self._finish(thread_id)
        try:
            explicit, selected = self.source(thread_id)
            if (self.closed or not ready or self.clock() >= pending['deadline']
                    or not explicit or _reference(selected) != pending['selection']):
                raise ValueError('selection changed')
            return AuthProxyResult(runtime=[pending['message']])
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, AttributeError):
            return self._error(pending['message'], '확인 중 작업 또는 SSH 계정 설정이 변경되었습니다. 다시 시도하세요.')

    def runtime(self, message, *, ready):
        """Return (private response consumed, additional pump outcome)."""
        if self.reserved(message):
            match = next((thread for thread, item in self.pending.items() if item['request_id'] == message['id']), None)
            if match is None or 'method' in message:
                return True, AuthProxyResult()
            pending = self.pending[match]
            if 'error' in message or message.get('result') != {}:
                pending = self._finish(match)
                return True, self._error(pending['message'], '선택한 프리셋을 아직 사용할 수 없습니다. SSH 설정을 적용하거나 준비된 프리셋을 선택하세요.')
            pending['ack'] = True
            return True, self._release(match, ready=ready)
        if 'id' in message:
            return False, AuthProxyResult()
        params = message.get('params') or {}
        thread_id = params.get('threadId') if isinstance(params, dict) else None
        if thread_id not in self.pending:
            return False, AuthProxyResult()
        if message.get('method') == 'thread/closed':
            pending = self._finish(thread_id)
            return False, self._error(pending['message'], '프리셋 확인 중 작업이 닫혔습니다. 작업을 다시 열어 시도하세요.')
        if message.get('method') == 'thread/settings/updated':
            settings = params.get('threadSettings')
            if isinstance(settings, dict) and 'executionPreset' in settings:
                try:
                    self.pending[thread_id]['notified'] = _reference(settings['executionPreset']) == self.pending[thread_id]['selection']
                except (ValueError, KeyError, TypeError, AttributeError):
                    self.pending[thread_id]['notified'] = False
            return False, self._release(thread_id, ready=ready)
        return False, AuthProxyResult()

    def poll(self, *, ready):
        result = AuthProxyResult()
        for thread_id, pending in list(self.pending.items()):
            if self.closed or not ready or self.clock() >= pending['deadline']:
                pending = self._finish(thread_id)
                result.frontend.extend(self._error(pending['message'],
                    '프리셋 적용을 확인하지 못해 요청을 보내지 않았습니다. SSH에 다시 연결한 뒤 시도하세요.').frontend)
        return result

    def close(self):
        self.closed = True
        return self.poll(ready=False)
