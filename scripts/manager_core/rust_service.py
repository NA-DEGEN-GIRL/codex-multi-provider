"""Private process broker used by the compatibility adapter, not Codex itself."""
import ctypes
from ctypes import wintypes
import errno
import json
import msvcrt
import os
import time
from uuid import uuid4


# Built once: state() makes a broker round trip per running profile.
_kernel = ctypes.WinDLL('kernel32', use_last_error=True)
_peek = _kernel.PeekNamedPipe
_peek.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                  ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]


def enabled():
    return bool(os.environ.get('CODEX_MANAGER_SERVICE_PIPE') and os.environ.get('CODEX_MANAGER_BROKER_TOKEN'))


# The service listens on one pipe instance at a time, so a client that
# arrives while another is being accepted gets ERROR_PIPE_BUSY. Python's
# open() reports that as EINVAL without a winerror.
_BUSY_WAIT = 2


def _busy(error):
    winerror = getattr(error, 'winerror', None)
    return winerror == 231 or (winerror is None and error.errno == errno.EINVAL)


def request(command, **args):
    pipe = os.environ['CODEX_MANAGER_SERVICE_PIPE']
    if not pipe.startswith('CodexControlCenter.service.'):
        raise RuntimeError('관리 서비스 연결 이름이 올바르지 않습니다.')
    args['broker_token'] = os.environ['CODEX_MANAGER_BROKER_TOKEN']
    request_id = uuid4().hex
    payload = json.dumps(dict(id=request_id, version=27, command=command, args=args), ensure_ascii=False).encode('utf-8') + b'\n'
    if len(payload) > 256*1024:
        raise RuntimeError('프로세스 요청이 너무 큽니다.')
    deadline = time.monotonic() + 20
    # Nothing has been sent yet, so only a busy pipe is retried, briefly. A
    # missing pipe means the service is gone: report it at once.
    busy_until, delay = time.monotonic() + _BUSY_WAIT, .001
    while True:
        try:
            stream = open('\\\\.\\pipe\\' + pipe, 'r+b', buffering=0)
            break
        except OSError as error:
            if not _busy(error) or time.monotonic() >= busy_until:
                raise RuntimeError('Rust 관리 서비스에 연결하지 못했습니다.') from error
            time.sleep(delay)
            delay = min(delay * 2, .05)
    with stream:
        view = memoryview(payload)
        while view:
            count = stream.write(view)
            if not count: raise RuntimeError('관리 서비스 연결이 닫혔습니다.')
            view = view[count:]
        handle = msvcrt.get_osfhandle(stream.fileno())
        response = bytearray()
        delay = .0005
        while time.monotonic() < deadline:
            available = wintypes.DWORD()
            if not _peek(handle, None, 0, None, ctypes.byref(available), None):
                raise RuntimeError('Rust 관리 서비스 연결이 종료되었습니다.')
            if available.value:
                response.extend(stream.read(min(available.value, 65536)))
                if len(response) > 8*1024*1024: raise RuntimeError('관리 서비스 응답이 너무 큽니다.')
                if b'\n' in response:
                    value = json.loads(response.split(b'\n', 1)[0])
                    if value.get('id') != request_id: raise RuntimeError('관리 서비스 응답이 다릅니다.')
                    if not value.get('ok'): raise RuntimeError(value.get('error', {}).get('message', '프로세스 관리 실패'))
                    return value['result']
                delay = .0005
            # The broker usually answers within a millisecond; a fixed 10 ms
            # sleep made each of state()'s identity round trips pay it whole.
            time.sleep(delay)
            delay = min(delay * 2, .01)
    raise RuntimeError('프로세스 요청 응답이 지연되었습니다. 중복 실행하지 않았습니다.')


class Process:
    def __init__(self, identity):
        self.pid = identity['process_id']
        self.created = identity['process_created']

    def poll(self):
        current = request('process.identity', pid=self.pid)
        return None if current and current['process_created'] == self.created else 0

    def wait(self, timeout=None):
        import subprocess
        deadline = time.monotonic() + (timeout or 20)
        while self.poll() is None:
            if time.monotonic() >= deadline: raise subprocess.TimeoutExpired('managed-profile', timeout)
            time.sleep(.1)
        return 0


def launch(profile, executable, environment, *, embed, reopen=False):
    return Process(request('process.launch', profile_id=profile['id'], generation=profile['generation'],
                           executable=executable, environment=environment, embed=embed, reopen=reopen))


def abort_launch(profile, process):
    """Stop the tree of a launch whose identity was never saved.

    The service acts only when its process-owner.json record for this profile
    names this generation and this exact process lifetime."""
    return request('process.abort_launch', profile_id=profile['id'], generation=profile['generation'],
                   process_id=process.pid, process_created=process.created)
