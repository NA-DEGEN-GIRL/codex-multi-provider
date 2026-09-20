"""Typed lifecycle inspection for one private SSH runtime; never reads auth."""
from contextlib import contextmanager
import fcntl
from pathlib import Path
import socket
import time

import native_controller as native
from ws_client import WebSocketPipe


@contextmanager
def connection(profile):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        sock.connect(str(native.socket_path(profile)))
        sock.settimeout(None)
        with sock.makefile('rb', buffering=0) as reader, sock.makefile('wb') as writer:
            try:
                pipe = WebSocketPipe(reader, writer, max_message=1024 * 1024)
                pipe.handshake(timeout=5)
                deadline = time.monotonic() + 30
                identity = 0

                def request(method, params):
                    nonlocal identity
                    identity += 1
                    return native._control_request(pipe, identity, method, params, deadline)

                request('initialize', {'clientInfo': {'name': 'codex_control_maintenance', 'version': '1'},
                                       'capabilities': {'experimentalApi': True}})
                yield request
            finally:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


def identity(profile, revision):
    """Verify a reusable listener, without assuming its work is idle."""
    process = native._running(profile, revision)
    if process is None:
        return {'process': None, 'idle': native._instance_lock_released(profile),
                'exited': True, 'revision': revision}
    with connection(profile) as request:
        diagnostics = request('server/diagnostics', {})
        if diagnostics.get('process', {}).get('id') != process['pid']:
            raise RuntimeError('Runtime diagnostics identity changed.')
    if native._running(profile, revision) != process:
        raise RuntimeError('Runtime identity changed during observation.')
    return {'process': process, 'idle': False, 'exited': False, 'revision': revision}


def inspect(profile, revision, *, discover_active=False):
    """Advisory only; shutdown rechecks all clients under the runtime fence."""
    lock_path = profile / 'native-start.lock'
    if lock_path.is_symlink() or not lock_path.is_file():
        raise RuntimeError('Lifecycle lock is unavailable.')
    with lock_path.open('r+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        requested_revision = revision
        process = (native._running(profile, revision, allow_other_revision=True) if discover_active
                   else native._running(profile, revision))
        if process is None:
            return {'process': None, 'idle': native._instance_lock_released(profile),
                    'exited': True, 'revision': revision}
        if discover_active:
            revision = process.get('revision')
            # Prepared definitions can coexist with an older live listener. The
            # actual descriptor must belong to this profile and host, and must
            # never be reported as having applied the requested configuration.
            native._descriptor(profile, revision)
        with connection(profile) as request:
            diagnostics = request('server/diagnostics', {})
            if diagnostics.get('process', {}).get('id') != process['pid']:
                raise RuntimeError('Runtime diagnostics identity changed.')
            gauges = {item['name']: item['value'] for item in diagnostics.get('gauges', [])}
            # codex-diagnostics registers gauges on first increment. The current
            # request gauge must exist; unused process/login/setup gauges may
            # be absent. This is advisory; managedShutdown rechecks real state.
            idle = gauges.get('app.managed.requests.pending_completion') == 1 and all(
                gauges.get('app.managed.' + name, 0) == 0 for name in
                ('processes.running', 'logins.running', 'sandbox_setups.running'))
            loaded, cursor, cursors = [], None, set()
            for _ in range(4):
                page = request('thread/loaded/list', {'limit': 256, 'cursor': cursor})
                data = page.get('data')
                if not isinstance(data, list) or any(not isinstance(item, str) for item in data):
                    raise RuntimeError('Runtime inventory is unavailable.')
                loaded.extend(data)
                cursor = page.get('nextCursor')
                if cursor is None:
                    break
                if not isinstance(cursor, str) or cursor in cursors:
                    raise RuntimeError('Runtime inventory changed.')
                cursors.add(cursor)
            if cursor is not None or len(loaded) != len(set(loaded)):
                raise RuntimeError('Runtime inventory exceeds the maintenance bound.')
            covered = set()
            for thread_id in loaded:
                thread = request('thread/read', {'threadId': thread_id, 'includeTurns': False}).get('thread', {})
                if thread.get('id') != thread_id or 'parentThreadId' not in thread:
                    raise RuntimeError('Runtime parentage is unavailable.')
                if thread['parentThreadId'] in loaded:
                    continue
                status = request('thread/managedIdleStatus', {'threadId': thread_id})
                ids = status.get('observedThreadIds')
                blockers = status.get('blockers')
                if (status.get('threadId') != thread_id or status.get('proofScope') != 'advisory'
                        or not isinstance(ids, list) or thread_id not in ids or not isinstance(blockers, list)):
                    raise RuntimeError('Runtime subtree is unavailable.')
                idle = idle and all(item.get('kind') == 'coldDescendantRequiresWriterClaim' for item in blockers)
                covered.update(ids)
            idle = idle and set(loaded).issubset(covered)
            if native._running(profile, revision) != process:
                raise RuntimeError('Runtime identity changed during observation.')
            return {'process': process, 'idle': idle, 'exited': False, 'revision': revision,
                    'requested_revision': requested_revision}


def dispatch(payload):
    binding = payload['binding']
    profile = Path(binding['remote_launcher']).parent
    expected = Path.home() / '.local/share/codex-control-center/profiles' / binding['profile_id']
    if profile != expected or profile.resolve() != profile:
        raise ValueError('Profile path mismatch.')
    revision = binding['revision']
    native._descriptor(profile, revision)
    operation = payload['operation']
    if operation == 'identity':
        return identity(profile, revision)
    if operation == 'inspect':
        result = inspect(profile, revision, discover_active=payload.get('discover_active') is True)
        if result['process'] is not None:
            actual = native._descriptor(profile, result['revision'])
            result['runtime_bundle'] = Path(actual['runtime']).name
            result['host_identity'] = actual['host_identity']
        return result
    if operation == 'stop':
        observed = payload['expected_process']
        if not isinstance(observed, dict) or observed.get('revision') != revision:
            raise ValueError('Shutdown requires an observed process.')
        native.stop(profile, revision, expected_process=observed)
        result = inspect(profile, revision)
        if result['exited'] is not True or result['idle'] is not True:
            raise RuntimeError('Remote shutdown remains unverified.')
        return result
    if operation == 'start':
        native.start(profile, revision)
        result = inspect(profile, revision)
        if result['process'] is None:
            raise RuntimeError('Remote startup remains unverified.')
        return result
    raise ValueError('Unsupported lifecycle operation.')
