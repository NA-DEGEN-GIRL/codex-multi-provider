"""Use one managed Codex instance's existing app-tools pipe.

No app.asar edits, debugger listener, global URL handler, or synthetic model
messages. The pipe server PID and birth time must match the selected instance.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import struct
import time
from uuid import UUID, uuid4

from .instances import process_identity
from .store import atomic_json

MAX_FRAME = 8 * 1024 * 1024
PIPE = re.compile(r'\\\\\.\\pipe\\[A-Za-z0-9._-]{1,200}\Z')
TOOLS = frozenset({'list_threads', 'list_projects', 'read_thread', 'navigate_to_codex_page'})


class AppBridgeError(RuntimeError):
    def __init__(self, code, message, *, uncertain=False):
        self.code, self.uncertain = code, uncertain
        super().__init__(message)


def encode_frame(message):
    data = json.dumps(message, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if not 0 < len(data) <= MAX_FRAME: raise ValueError('Native app request exceeds frame bounds.')
    return struct.pack('<I', len(data)) + data


class NativePipe:
    def __init__(self, path, *, expected_identity=None, timeout=5):
        if os.name != 'nt' or not isinstance(path, str) or not PIPE.fullmatch(path):
            raise AppBridgeError('invalid_app_pipe', '유효한 Windows 앱 연결이 필요합니다.')
        import _winapi
        self.api = _winapi
        self.handle = None
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.handle = _winapi.CreateFile(path, 0xC0000000, 0, 0, 3, 0x40000000, 0)
                break
            except OSError as error:
                if error.winerror != 231 or time.monotonic() >= deadline:
                    raise AppBridgeError('app_pipe_unavailable', '선택한 Codex 앱의 연결이 준비되지 않았습니다.') from None
                try: _winapi.WaitNamedPipe(path, min(200, max(1, int((deadline-time.monotonic())*1000))))
                except OSError: pass
        try:
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.GetNamedPipeServerProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
            kernel.GetNamedPipeServerProcessId.restype = wintypes.BOOL
            pid = wintypes.ULONG()
            if not kernel.GetNamedPipeServerProcessId(self.handle, ctypes.byref(pid)):
                raise AppBridgeError('app_identity_unknown', '실제 앱 프로세스를 확인하지 못했습니다.')
            self.identity = process_identity(pid.value)
            if self.identity is None or (expected_identity is not None and self.identity != expected_identity):
                raise AppBridgeError('app_identity_changed', '선택한 계정의 Codex 앱 프로세스가 바뀌었습니다.')
        except Exception:
            self.close()
            raise

    def close(self):
        if self.handle is not None:
            self.api.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self): return self
    def __exit__(self, *args): self.close()

    def _io(self, writing, value, deadline):
        if self.handle is None: raise AppBridgeError('app_pipe_closed', '앱 연결이 종료되었습니다.')
        ov, error = (self.api.WriteFile(self.handle, value, overlapped=True) if writing else
                     self.api.ReadFile(self.handle, value, overlapped=True))
        try:
            if error == 997:
                milliseconds = max(0, int((deadline-time.monotonic())*1000))
                if self.api.WaitForMultipleObjects([ov.event], False, milliseconds) != 0:
                    raise AppBridgeError('app_response_timeout', '앱 응답 시간이 초과되었습니다. 같은 이동을 자동 반복하지 않습니다.', uncertain=True)
        except BaseException:
            ov.cancel()
            ov.GetOverlappedResult(True)
            raise
        count, error = ov.GetOverlappedResult(True)
        if error or count <= 0: raise AppBridgeError('app_pipe_closed', '앱 연결이 응답 전에 종료되었습니다.', uncertain=True)
        return count if writing else bytes(ov.getbuffer())[:count]

    def _read(self, size, deadline):
        data = bytearray()
        while len(data) < size:
            data.extend(self._io(False, size-len(data), deadline))
        return bytes(data)

    def request(self, method, params=None, *, timeout=20):
        if method not in ('tools/list', 'tools/call'): raise ValueError('Unsupported app bridge operation.')
        if method == 'tools/call':
            if not isinstance(params, dict) or params.get('tool') not in TOOLS or params.get('namespace') != 'codex_app':
                raise ValueError('Unsupported manager app tool.')
        request_id = 'manager-' + uuid4().hex
        message = dict(id=request_id, jsonrpc='2.0', method=method)
        if params is not None: message['params'] = params
        frame = encode_frame(message); offset = 0; deadline = time.monotonic() + timeout
        try:
            while offset < len(frame): offset += self._io(True, frame[offset:], deadline)
            for _ in range(32):
                size = struct.unpack('<I', self._read(4, deadline))[0]
                if not 0 < size <= MAX_FRAME: raise ValueError('Native app response exceeds frame bounds.')
                response = json.loads(self._read(size, deadline))
                if not isinstance(response, dict) or response.get('jsonrpc') != '2.0': raise ValueError('Invalid app response.')
                if response.get('id') != request_id: continue
                if 'error' in response:
                    raise AppBridgeError('app_request_rejected', 'Codex 앱이 요청을 처리하지 못했습니다.', uncertain=True)
                if 'result' not in response: raise ValueError('Missing native app result.')
                return response['result']
            raise ValueError('Unmatched native app responses.')
        except (OSError, ValueError) as error:
            raise AppBridgeError('app_response_unverified', '앱의 응답을 확인하지 못했습니다.', uncertain=offset > 0) from error


def publish(environment):
    """Called only by an isolated manager runtime, using its inherited pipe."""
    path = environment.get('CODEX_APP_TOOLS_PIPE_PATH')
    if not path: return None
    root = Path(environment['CODEX_MANAGER_ROOT']).resolve(strict=True)
    profile_id = str(UUID(environment['CODEX_MANAGER_PROFILE_ID']))
    generation = str(UUID(environment['CODEX_MANAGER_GENERATION']))
    expected = root / 'work/control-center/profiles' / profile_id / 'codex'
    if Path(environment['CODEX_HOME']).resolve(strict=True) != expected or expected.resolve() != expected:
        raise ValueError('App bridge requires the manager-owned profile HOME.')
    with NativePipe(path) as pipe:
        identity = pipe.identity
        if Path(identity['executable_path']).name.casefold() != 'chatgpt.exe':
            raise ValueError('App tool pipe is not hosted by the native Codex app.')
    directory = root / 'work/control-center/instances' / profile_id
    directory.mkdir(parents=True, exist_ok=True)
    if directory.resolve(strict=True) != directory: raise ValueError('App bridge descriptor directory changed.')
    descriptor = dict(version=1, profile_id=profile_id, generation=generation, pipe_path=path, app=identity)
    atomic_json(directory / 'native-app-bridge.json', descriptor)
    return descriptor


class AppBridge:
    def __init__(self, root, profile, *, pipe_factory=NativePipe):
        self.profile, self.pipe_factory = profile, pipe_factory
        pid = str(UUID(profile['id']))
        root = Path(root).resolve(strict=True)
        self.path = root / 'work/control-center/instances' / pid / 'native-app-bridge.json'
        if self.path.is_symlink() or self.path.resolve(strict=True) != self.path or self.path.stat().st_size > 16384:
            raise ValueError('Invalid app bridge descriptor.')
        self.descriptor = json.loads(self.path.read_text(encoding='utf-8'))
        expected = {k: profile[k] for k in ('process_id', 'process_created', 'executable_path')}
        if (self.descriptor.get('version') != 1 or self.descriptor.get('profile_id') != pid
                or self.descriptor.get('generation') != profile.get('generation') or self.descriptor.get('app') != expected):
            raise AppBridgeError('app_identity_changed', '이 프로필의 최신 Codex 연결이 필요합니다.')

    def request(self, method, params=None, *, timeout=20):
        with self.pipe_factory(self.descriptor['pipe_path'], expected_identity=self.descriptor['app']) as pipe:
            return pipe.request(method, params, timeout=timeout)

    def tools(self):
        result = self.request('tools/list', {'threadStartKind':'all'})
        if not isinstance(result,dict) or not isinstance(result.get('tools'),list): raise ValueError('App tool inventory missing.')
        return result['tools']

    def call(self, tool, arguments, context_thread_id, *, timeout=30):
        if tool not in TOOLS: raise ValueError('Unsupported manager app tool.')
        # The app's MCP client also supplies manager-like correlation IDs when
        # executor turn metadata is absent; this does not start a model turn.
        result = self.request('tools/call', dict(tool=tool, arguments=arguments, namespace='codex_app',
            threadId=str(UUID(context_thread_id)), turnId='manager-ui-' + uuid4().hex,
            callId='manager-ui-' + uuid4().hex), timeout=timeout)
        if not isinstance(result,dict) or result.get('success') is not True:
            raise AppBridgeError('app_tool_failed', '선택한 앱이 요청을 완료하지 못했습니다.')
        items = result.get('contentItems')
        if not isinstance(items,list) or len(items)!=1 or items[0].get('type')!='inputText':
            raise ValueError('Unexpected manager app tool response.')
        return json.loads(items[0]['text'])
