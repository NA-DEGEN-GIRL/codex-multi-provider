"""Typed lifecycle inspection for one private SSH runtime; never reads auth."""
from contextlib import contextmanager
import fcntl
import hashlib
from pathlib import Path
import re
import socket
import time

import native_controller as native
from ws_client import WebSocketPipe


# The runtime can refuse the idle/shutdown proof while the process itself is
# healthy: a listener started without the exclusive managed source manifest has
# no idle inventory, and its socket can outlive a closed connection. These are
# read-only observations: they may report the exact process but never idle or a
# completed exit, and they never authorize a restart.
UNAVAILABLE_OBSERVATIONS = (
    'remote_idle_binding_missing',
    'remote_idle_status_unavailable',
    'remote_idle_diagnostics_unavailable',
    'remote_listener_unavailable',
)

# Explicit shared-mode graceful drain. These codes must also be listed in the
# manager's ALLOWED_REMOTE_CODES, otherwise the in-memory bootstrap collapses
# them into remote_maintenance_unverified:
#   remote_drain_unsupported   - unknown binary or SIGHUP would terminate it
#   remote_drain_identity_changed - never signals a different process
#   remote_drain_unverified    - exit without an instance-lock release
#   remote_drain_unaudited     - binary digest outside the audited allowlist
DRAIN_ERROR_CODES = ('remote_drain_unsupported', 'remote_drain_identity_changed',
                     'remote_drain_unverified', 'remote_drain_unaudited')


@contextmanager
def connection(profile):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        try:
            sock.connect(str(native.socket_path(profile)))
        except (FileNotFoundError, ConnectionRefusedError):
            raise native.RemoteMaintenanceError('remote_listener_unavailable') from None
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


def settings_match(profile, revision, expected):
    """Compare generated immutable config bytes, never auth or mutable user files."""
    if (not isinstance(expected, dict) or not 1 <= len(expected) <= 128
            or any(not isinstance(name, str) or not isinstance(digest, str)
                   or not re.fullmatch(r'[0-9a-f]{64}', digest) for name, digest in expected.items())):
        raise ValueError('Invalid settings digest request.')
    directory = profile / 'definitions' / revision
    descriptor = native._descriptor(profile, revision)
    if (Path(descriptor['definition']) != directory or directory.is_symlink()
            or directory.resolve() != directory or not directory.is_dir()):
        raise ValueError('Immutable settings directory is unavailable.')
    actual, size = {}, 0
    for path in directory.rglob('*'):
        if path.is_symlink():
            raise ValueError('Immutable settings symlink.')
        if path.is_dir():
            continue
        name = path.relative_to(directory).as_posix()
        if not path.is_file() or name not in expected or path.suffix not in ('.toml', '.json'):
            return False
        size += path.stat().st_size
        if size > 2 * 1024 * 1024:
            raise ValueError('Immutable settings size limit.')
        actual[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return actual == expected


def _read_only_identity(profile, revision, *, discover_active=False):
    """Prove the running process from its record, executable and birth id only.

    This is the strongest evidence available when the runtime cannot answer the
    idle inventory RPC. It never opens a connection and never claims idle.
    """
    process = native._running(profile, revision, allow_other_revision=discover_active)
    if process is None:
        return None
    actual_revision = process['revision']
    descriptor = native._descriptor(profile, actual_revision)
    # Socket-less processes have no RPC identity. Require exact executable
    # and birth fingerprint before reporting their version.
    executable = Path('/proc') / str(process['pid']) / 'exe'
    expected = Path(descriptor['runtime']) / 'codex'
    if (executable.resolve(strict=True) != expected.resolve(strict=True)
            or native._running(profile, actual_revision) != process):
        raise RuntimeError('Runtime executable identity changed.') from None
    return process, actual_revision


def observe(profile, revision, *, discover_active=False):
    """Version observation cannot supply a missing idle/shutdown proof."""
    try:
        return inspect(profile, revision, discover_active=discover_active)
    except native.RemoteMaintenanceError as error:
        if error.code not in UNAVAILABLE_OBSERVATIONS:
            raise
        observed = _read_only_identity(profile, revision, discover_active=discover_active)
        if observed is None:
            raise
        process, actual_revision = observed
        return dict(process=process, idle=False, exited=False, revision=actual_revision,
                    requested_revision=revision, observation_code=error.code)


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
            # Shared-record workers do not enable the exclusive managed-source
            # counters. Missing evidence is unsupported, not an active request
            # that another poll can drain. In particular an empty loaded list
            # must not turn this into an endless "busy" result.
            if 'app.managed.requests.pending_completion' not in gauges:
                raise native.RemoteMaintenanceError('remote_idle_diagnostics_unavailable')
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
        result = identity(profile, revision)
        if 'expected_settings' in payload:
            descriptor = native._descriptor(profile, revision)
            result['settings_match'] = (
                descriptor['host_identity'] == payload.get('expected_host_identity')
                and Path(descriptor['runtime']).name == payload.get('expected_runtime_bundle')
                and settings_match(profile, revision, payload['expected_settings']))
        return result
    if operation == 'inspect':
        inspect_fn = observe if payload.get('observe_only') is True else inspect
        result = inspect_fn(profile, revision, discover_active=payload.get('discover_active') is True)
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
    if operation == 'drain':
        # Shared-catalog listeners cannot answer managedIdleStatus or
        # managedShutdown. This explicit request is the weaker, named
        # alternative: one exact-pidfd SIGHUP, pending request persisted
        # before the signal, active turns preserved until exit, and success
        # only from process exit plus instance-lock release. It never reports
        # idle and it never escalates a repeated call into a forced signal.
        observed = payload.get('expected_process')
        if not isinstance(observed, dict) or observed.get('revision') != revision:
            raise ValueError('Drain requires an observed process.')
        # Only audited artifacts drain. The manager may supply the digest from
        # its own verified manifest; the helper still requires allowlist membership.
        trusted = payload.get('expected_runtime_sha256')
        if trusted is not None and (not isinstance(trusted, str)
                                    or not re.fullmatch(r'[0-9a-f]{64}', trusted)):
            raise ValueError('Drain requires an audited runtime digest.')
        bundle = payload.get('expected_runtime_bundle')
        if bundle is not None and Path(native._descriptor(profile, revision)['runtime']).name != bundle:
            raise native.RemoteDrainError('remote_drain_unaudited')
        return native.drain(profile, revision, expected_process=observed,
                            wait_seconds=payload.get('wait_seconds'), trusted_sha256=trusted)
    if operation == 'start':
        native.start(profile, revision)
        try:
            result = inspect(profile, revision)
        except native.RemoteMaintenanceError as error:
            # A listener without the exclusive managed source manifest cannot
            # inventory idle work, so a start is verified by exact process and
            # revision identity instead. This is what lets an unchanged
            # connection reuse the listener it already has; it never reports
            # idle, and a restart that needs an exit proof still fails closed.
            if error.code not in ('remote_idle_status_unavailable', 'remote_idle_diagnostics_unavailable'):
                raise
            observed = _read_only_identity(profile, revision)
            if observed is None:
                raise RuntimeError('Remote startup remains unverified.') from None
            process, actual_revision = observed
            result = dict(process=process, idle=False, exited=False, revision=actual_revision)
        if result['process'] is None:
            raise RuntimeError('Remote startup remains unverified.')
        return result
    raise ValueError('Unsupported lifecycle operation.')
