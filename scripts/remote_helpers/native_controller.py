"""Profile-scoped Unix transport for the original desktop's SSH adapter.

Only the managed SSH shim invokes these operations. Native bootstrap and pkill
payloads are replaced completely; none of them are forwarded to the stock CLI.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import subprocess
import sys
import time

from ws_client import WebSocketPipe


class RemoteStartError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__('Remote managed listener startup failed (' + code + ').')


class RemoteMaintenanceError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__('Remote maintenance proof is unavailable (' + code + ').')


class RemoteDrainError(RuntimeError):
    """A refusal that must never be retried with a signal of another kind."""

    def __init__(self, code):
        self.code = code
        super().__init__('Remote graceful drain is unavailable (' + code + ').')


class RemoteRevisionConflict(RuntimeError):
    """An exact-revision start cannot proceed while another revision is live."""

    code = 'remote_revision_conflict'

    def __init__(self):
        super().__init__('A different revision is still running; wait for its jobs to finish.')


# Exact runtime answers that mean "this instance cannot provide that proof".
# They are typed instead of generic so a shared-catalog listener is never
# mistaken for a busy instance, a transport failure, or a completed exit.
MAINTENANCE_ERROR_CODES = {
    ('thread/managedIdleStatus', -32600, 'root actor has no immutable managed source binding'):
        'remote_idle_binding_missing',
    # A shared-catalog listener is launched without CODEX_MANAGER_MANAGED_SOURCES
    # (launch.py drops it so record routing stays catalog-owned), so the runtime
    # reports that the idle proof does not exist in this mode at all.
    ('thread/managedIdleStatus', -32600, 'managed idle status is not enabled for this instance'):
        'remote_idle_status_unavailable',
    ('server/managedShutdown', -32600, 'managed shutdown requires the exact managed process'):
        'remote_shutdown_unavailable',
}


def _start_failure(logfile, offset):
    """Classify only this launch's bounded output; never return arbitrary logs."""
    try:
        with logfile.open('rb') as stream:
            stream.seek(0, os.SEEK_END)
            end = stream.tell()
            stream.seek(max(offset, end - 16384))
            lines = stream.read(16384).splitlines()
        if b'Codex manager remote launcher error: remote_configuration_changed' in lines:
            return 'remote_configuration_changed'
    except OSError:
        pass
    return 'remote_runtime_exited'


def socket_path(profile):
    # Linux's sockaddr_un path is limited to 108 bytes. CODEX_HOME itself can be
    # long, so use an explicit private socket and `proxy --sock` on both sides.
    directory = Path("/tmp") / ("codex-control-" + str(os.getuid()))
    if directory.is_symlink():
        raise ValueError("socket directory symlink")
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("socket directory ownership")
    path = directory / (profile.name.replace("-", "") + ".sock")
    if len(os.fsencode(path)) >= 104 or path.is_symlink():
        raise ValueError("socket path")
    return path


def _descriptor(profile, revision):
    import launch
    if not re.fullmatch(r"[0-9a-f]{64}", revision):
        raise ValueError("revision")
    data = launch._read(profile / "definitions" / (revision + ".json"))
    identity = Path("/etc/machine-id").read_text().strip()
    actual_host = hashlib.sha256((identity + "\\0" + str(os.getuid()) + "\\0" + str(Path.home())).encode()).hexdigest()
    if not data.get("host_identity") or data["host_identity"] != actual_host:
        raise ValueError("SSH host or OS user changed")
    runtime = Path(data["runtime"])
    if (data["revision"] != revision or data["profile_id"] != profile.name or
            runtime.is_symlink() or runtime.parent.resolve() != (profile.parent.parent / "runtime").resolve()):
        raise ValueError("binding")
    return data


def _process_start(pid):
    try:
        # comm can contain spaces or ')'; fields after the final ')' are stable.
        text = Path(f"/proc/{int(pid)}/stat").read_text()
        fields = text[text.rfind(")") + 2:].split()
        return None if fields[0] == "Z" else fields[19]
    except (OSError, ValueError, TypeError, IndexError):
        return None


def process_record(revision, path):
    return {"pid": os.getpid(), "process_start": _process_start(os.getpid()),
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "revision": revision, "socket": str(path)}


def _running(profile, revision, *, allow_other_revision=False):
    import launch
    path = profile / "native-instance.json"
    if not path.exists():
        return None
    data = launch._read(path)
    if (not data.get("process_start") or _process_start(data.get("pid")) != data.get("process_start") or
            data.get("boot_id") != Path("/proc/sys/kernel/random/boot_id").read_text().strip()):
        return None
    if data.get("revision") != revision and not allow_other_revision:
        raise RemoteRevisionConflict()
    return data


def _forward_agent(profile):
    source = os.environ.get("SSH_AUTH_SOCK")
    path = profile / "forwarded-ssh-agent.sock"
    if source and source != str(path):
        try:
            info = Path(source).stat()
            if stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid():
                temporary = profile / (".agent-" + str(os.getpid()))
                if temporary.exists() or temporary.is_symlink():
                    raise ValueError("agent temporary conflict")
                os.symlink(source, temporary)
                os.replace(temporary, path)
        except FileNotFoundError:
            pass
    return str(path)


def _ready(profile, revision, path):
    if _running(profile, revision) is None or not path.exists() or not stat.S_ISSOCK(path.stat().st_mode):
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.3)
            probe.connect(str(path))
        return True
    except OSError:
        return False


def start(profile, revision):
    import launch
    _descriptor(profile, revision)
    lock_path = profile / "native-start.lock"
    if lock_path.is_symlink():
        raise ValueError("start lock symlink")
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = socket_path(profile)
        existing = _running(profile, revision)
        process = None
        if existing is None:
            # Do not create another listener if a stdio runtime owns this profile.
            instance_lock = profile / "instance.lock"
            if instance_lock.is_symlink():
                raise ValueError("instance lock symlink")
            with instance_lock.open("a+b") as held:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            logfile = profile / "native-runtime.log"
            if logfile.is_symlink():
                raise ValueError("log symlink")
            environment = dict(os.environ)
            environment["SSH_AUTH_SOCK"] = _forward_agent(profile)
            with logfile.open("ab", buffering=0) as output:
                os.chmod(logfile, 0o600)
                log_offset = output.tell()
                process = subprocess.Popen([sys.executable, str(profile / "launch.py"), revision, "native-serve"],
                    stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                    env=environment, start_new_session=True)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if _ready(profile, revision, path):
                return 0
            if process is not None and process.poll() is not None:
                raise RemoteStartError(_start_failure(logfile, log_offset))
            time.sleep(0.1)
        if process is None:
            # The exact revision is recorded as running and was never relaunched,
            # so a missing/unconnectable private socket is not a launch timeout.
            # Report the listener state itself; the proxy cannot attach to it.
            raise RemoteStartError('remote_listener_unavailable')
        raise RemoteStartError('remote_start_timeout')


def _control_request(pipe, request_id, method, params, deadline):
    request = {"id": request_id, "method": method, "params": params}
    pipe.send_json(request)
    received_bytes = 0
    for _ in range(4096):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Remote maintenance response timed out; exit is unverified.")
        message = pipe.receive_json(timeout=remaining)
        received_bytes += len(json.dumps(message, ensure_ascii=False).encode())
        if received_bytes > 16 * 1024 * 1024:
            raise RuntimeError("Remote maintenance response exceeds its total limit.")
        if not isinstance(message, dict):
            raise RuntimeError("Invalid remote maintenance response.")
        # An admin connection must never answer a user's approval/auth request.
        if "method" in message and "id" in message:
            raise RuntimeError("The runtime requested client input; maintenance is deferred.")
        if message.get("id") != request_id:
            continue
        if "error" in message:
            error = message['error']
            if isinstance(error, dict):
                classified = MAINTENANCE_ERROR_CODES.get((method, error.get('code'), error.get('message')))
                if classified is not None:
                    raise RemoteMaintenanceError(classified)
            raise RuntimeError("The runtime could not verify maintenance; updates remain pending.")
        result = message.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("Missing remote maintenance result.")
        return result
    raise RuntimeError("Remote maintenance notification limit exceeded.")


def _instance_lock_released(profile):
    path = profile / "instance.lock"
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("Remote instance lock is unavailable; exit is unverified.")
    with path.open("r+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True


# --- shared-mode graceful drain (SIGHUP) ------------------------------------
#
# A shared-catalog listener is launched without CODEX_MANAGER_MANAGED_SOURCES
# (launch.py drops it so record routing stays catalog-owned), so
# `server/managedShutdown` and `thread/managedIdleStatus` answer
# -32600 "not enabled for this instance" and no idle or writer-release proof
# exists in that mode. The standalone app-server still installs a *graceful*
# SIGHUP handler (lib.rs `shutdown_signal`: SIGHUP => GracefulOnly) that waits
# for running assistant turns before it stops accepting and exits, and the
# graceful teardown branch drains background tasks and shuts thread actors
# down. A repeated SIGHUP can never force that drain; only a Forceable signal
# (SIGINT/SIGTERM) can, and this helper never sends one.
#
# The contract is deliberately weaker than `server/managedShutdown` and is
# named accordingly: exact pidfd signal only, refusal unless the installed
# binary and the live process both show the drain implementation, success only
# from process exit plus instance-lock release, and never an "idle" claim.
DRAIN_FILE = "native-drain.json"
DRAIN_SCHEMA = 1
DRAIN_WAIT_DEFAULT = 5.0
DRAIN_WAIT_MAX = 15.0
DRAIN_SCAN_BYTES = 512 * 1024 * 1024
DRAIN_REQUEST_BYTES = 65536
# Compiled into the standalone app-server's graceful signal drain. Provenance
# limit: presence proves the drain implementation is in the installed binary;
# it does not say which signal maps to it. Only the audited digest below is
# accepted as that anchor; the markers and the caught-signal mask are extra
# defence in depth, never a substitute.
DRAIN_MARKERS = (
    b"entering graceful restart drain",
    b"shutdown signal restart: waiting for",
    b"received second shutdown signal; forcing restart",
)
# Provenance anchor. Markers only show that the drain implementation is compiled
# into the file, and the caught-signal mask only shows that this live process
# handles SIGHUP; neither proves that SIGHUP maps to the GracefulOnly branch.
# Only these audited artifacts are drained:
#   artifacts/remote/linux-x86_64/manifest.json
#   version 0.153.4-managed-e29fcb2680f61520, build_source_sha256 e29fcb26...
#   version 0.153.4-managed-308e564225e01416, build_source_sha256 308e5642...
#   version 0.153.4-managed-d7ab12a792bcd355, build_source_sha256 d7ab12a7...
#   (revisions 94-95: app-server/src/lib.rs still maps SIGHUP to GracefulOnly)
# The manager may pass the digest from its own verified manifest; that digest
# must still be in this allowlist, so an unknown bundle always refuses.
DRAIN_AUDITED_SHA256 = frozenset({
    "4c6ca2dd15f100ea740ac01d956bb1898c1b37f6c5d9bfc269b405640cc6249a",
    "37fdb54943696c8048ecdac72a102423147e3a039dcc9e53ebfa97bcf7ebc938",
    "e85e7046798622783e66ee9ea97b2e36c8c53bea05b04252f80a8396261b727a",
})
DRAIN_HASH_BYTES = 1024 * 1024 * 1024
# The helper is deployed to Linux only; these stay import-safe elsewhere so a
# Windows checkout can still read the module constants in tests and tooling.
DRAIN = getattr(signal, "SIGHUP", None)
SIGHUP_BIT = (1 << (DRAIN - 1)) if DRAIN is not None else 0


def signal_mask_from_status(text):
    """Caught-signal mask from /proc/<pid>/status text; None when unavailable."""
    for line in text.splitlines():
        if line.startswith("SigCgt:"):
            try:
                return int(line.split(":", 1)[1].strip(), 16)
            except ValueError:
                return None
    return None


def catches_hangup(pid):
    """True/False/None: does this exact live process handle SIGHUP itself?"""
    if DRAIN is None:
        return None
    try:
        text = Path("/proc/" + str(int(pid)) + "/status").read_text()
    except (OSError, ValueError, TypeError):
        return None
    mask = signal_mask_from_status(text)
    return None if mask is None else bool(mask & SIGHUP_BIT)


def drain_markers(path):
    """Find the graceful-drain markers inside one installed binary."""
    found = set()
    overlap = max(len(marker) for marker in DRAIN_MARKERS) - 1
    remaining = DRAIN_SCAN_BYTES
    tail = b""
    with open(path, "rb") as stream:
        while remaining > 0 and len(found) < len(DRAIN_MARKERS):
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            window = tail + chunk
            for marker in DRAIN_MARKERS:
                if marker not in found and marker in window:
                    found.add(marker)
            tail = window[-overlap:]
    return found


def _drain_record(profile):
    """Read the persisted request for this profile; None when absent."""
    import launch
    path = profile / DRAIN_FILE
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > DRAIN_REQUEST_BYTES:
        raise RemoteDrainError('remote_drain_unverified')
    record = launch._read(path)
    if not isinstance(record, dict) or record.get('schema') != DRAIN_SCHEMA:
        raise RemoteDrainError('remote_drain_unverified')
    return record


def _drain_write(profile, record):
    """Persist the request/outcome before and after signalling, never after."""
    import launch
    launch._atomic(profile / DRAIN_FILE, record)


def _drain_identity(profile, revision):
    """Exact live listener identity, or None when nothing owns the profile."""
    observed = _running(profile, revision)
    if observed is None:
        return None
    if observed.get("socket") != str(socket_path(profile)):
        raise RemoteDrainError('remote_drain_identity_changed')
    descriptor = _descriptor(profile, revision)
    if _process_start(observed["pid"]) != observed.get("process_start"):
        return None
    # Device/inode identity against the live link, never a path comparison.
    _drain_executable(descriptor, observed["pid"])
    return observed


def _drain_digest(path):
    """Digest one installed artifact; None when it exceeds the bound."""
    digest = hashlib.sha256()
    read = 0
    with open(path, "rb") as stream:
        while read < DRAIN_HASH_BYTES:
            chunk = stream.read(min(4 * 1024 * 1024, DRAIN_HASH_BYTES - read))
            if not chunk:
                return digest.hexdigest()
            read += len(chunk)
            digest.update(chunk)
    return None


def _same_file_stat(live, expected):
    """True when both paths are the same device/inode right now.

    A resolved path string cannot see a bundle replaced in place: the
    descriptor still names the same file while the running process executes
    another inode. Device/inode identity is compared instead.
    """
    try:
        left, right = os.stat(live), os.stat(expected)
    except (OSError, ValueError, TypeError):
        return False
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _drain_executable(descriptor, pid):
    """The live `/proc/<pid>/exe` link, proven to be the expected runtime.

    Returns the live link path so callers hash the running artifact instead of
    the descriptor path. An in-place overwrite of a *running* file cannot be
    detected from the file itself; a replaced inode always is.
    """
    expected = Path(descriptor["runtime"]) / "codex"
    if expected.is_symlink() or not expected.is_file():
        raise RemoteDrainError('remote_drain_unsupported')
    live = Path("/proc", str(pid), "exe")
    if not _same_file_stat(live, expected):
        raise RemoteDrainError('remote_drain_identity_changed')
    return live


def _drain_capability(descriptor, observed, *, trusted_sha256=None):
    """Refuse an unaudited binary or a process that would die on SIGHUP."""
    if DRAIN is None or not hasattr(signal, "pidfd_send_signal"):
        # Checked before any journal write: an unsupported interpreter must not
        # leave a pending unsent request behind.
        raise RemoteDrainError('remote_drain_unsupported')
    live = _drain_executable(descriptor, observed["pid"])
    digest = _drain_digest(live)
    if digest is None or digest not in DRAIN_AUDITED_SHA256:
        raise RemoteDrainError('remote_drain_unaudited')
    if trusted_sha256 is not None and digest != trusted_sha256:
        raise RemoteDrainError('remote_drain_unaudited')
    if set(DRAIN_MARKERS).difference(drain_markers(live)):
        raise RemoteDrainError('remote_drain_unsupported')
    if catches_hangup(observed["pid"]) is not True:
        raise RemoteDrainError('remote_drain_unsupported')
    return live, digest


def _process_gone(process):
    """Boot-aware gone proof: a record from another boot cannot be live."""
    if not isinstance(process, dict):
        return False
    boot = process.get("boot_id")
    if not isinstance(boot, str) or not boot:
        return False
    try:
        current = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return False
    if boot != current:
        return True
    return _process_start(process.get("pid")) != process.get("process_start")


def _drain_handle(pid):
    """A pidfd for exactly this process. The kernel never retargets a pidfd."""
    if not hasattr(os, "pidfd_open"):
        raise RemoteDrainError('remote_drain_unsupported')
    try:
        handle = os.pidfd_open(int(pid), 0)
    except OSError:
        raise RemoteDrainError('remote_drain_identity_changed') from None
    try:
        try:
            info = Path("/proc/self/fdinfo/" + str(handle)).read_text()
        except OSError:
            raise RemoteDrainError('remote_drain_unsupported') from None
        target = None
        for line in info.splitlines():
            if line.startswith("Pid:"):
                target = line.split(":", 1)[1].strip()
                break
        if target != str(int(pid)):
            raise RemoteDrainError('remote_drain_identity_changed')
        return handle
    except BaseException:
        os.close(handle)
        raise


def _drain_send(handle):
    # CPython exposes pidfd signalling on the signal module, not on os.
    if DRAIN is None or not hasattr(signal, "pidfd_send_signal"):
        raise RemoteDrainError('remote_drain_unsupported')
    signal.pidfd_send_signal(handle, DRAIN, None, 0)


def _drain_close(handle):
    os.close(handle)


def _drain_result(state, process, revision, *, drain_requested, exited, lock_released, idle,
                  signal_sent=False, markers=(), requested_at=None, sha256=None,
                  record_state=None):
    """Common manager shape {revision, process, idle, exited} plus drain detail.

    `idle` is never derived from an inventory: it is true only once this exact
    process is gone and the profile's instance lock is released.
    """
    labels = sorted(marker.decode("utf-8", "replace") if isinstance(marker, (bytes, bytearray))
                    else str(marker) for marker in markers)
    return dict(state=state, drain_requested=drain_requested, exited=exited, idle=idle,
                revision=revision, process=process,
                signal="SIGHUP", signal_sent=signal_sent, lock_released=lock_released,
                markers=labels, requested_at=requested_at, runtime_sha256=sha256,
                record_state=record_state)


def _drain_wait(profile, observed, deadline):
    """Wait for exit plus lock release; never reports idle and never signals."""
    while True:
        if _process_start(observed["pid"]) != observed["process_start"]:
            if _instance_lock_released(profile):
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def drain(profile, revision, *, expected_process=None, wait_seconds=None, trusted_sha256=None):
    """Explicit, per-profile graceful drain for a shared-mode listener."""
    _descriptor(profile, revision)
    lock_path = profile / "native-start.lock"
    if lock_path.is_symlink() or not lock_path.is_file():
        raise ValueError("start lock symlink")
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _drain_locked(profile, revision, expected_process=expected_process,
                             wait_seconds=wait_seconds, trusted_sha256=trusted_sha256)


def _drain_locked(profile, revision, *, expected_process=None, wait_seconds=None,
                  trusted_sha256=None):
    """Hold native-start.lock so a reconnect cannot spawn during the drain."""
    try:
        wait = DRAIN_WAIT_DEFAULT if wait_seconds is None else float(wait_seconds)
    except (TypeError, ValueError):
        raise ValueError("drain wait") from None
    wait = min(max(wait, 0.0), DRAIN_WAIT_MAX)
    if expected_process is not None:
        if not isinstance(expected_process, dict) or expected_process.get("revision") != revision:
            raise ValueError("Drain requires an observed process.")
    observed = _drain_identity(profile, revision)
    record = _drain_record(profile)
    if observed is None:
        if expected_process is not None and not _process_gone(expected_process):
            raise RemoteDrainError('remote_drain_identity_changed')
        released = _instance_lock_released(profile)
        if not released:
            if expected_process is None:
                raise RemoteDrainError('remote_drain_unverified')
            # The listener exited but the profile lock is still held: teardown
            # is not proven. Stay retryable and never report idle.
            return _drain_result('draining', expected_process, revision, drain_requested=True,
                                 exited=False, lock_released=False, idle=False,
                                 markers=DRAIN_MARKERS)
        if record is not None and record.get('state') != 'drained':
            _drain_write(profile, {**record, 'state': 'drained', 'exited_at': time.time(),
                                   'lock_released': True})
        return _drain_result('exited', None, revision, drain_requested=False, exited=True,
                             lock_released=True, idle=True, markers=DRAIN_MARKERS,
                             record_state=None if record is None else 'drained')
    if expected_process is not None and observed != expected_process:
        raise RemoteDrainError('remote_drain_identity_changed')
    if record is not None and record.get('process') != observed:
        # A record for a listener that no longer exists is a completed episode,
        # not a claim about this process. Retire it under native-start.lock only
        # with boot-aware gone proof; a live other process always refuses.
        if not _process_gone(record.get('process')):
            raise RemoteDrainError('remote_drain_identity_changed')
        record = None
    signal_sent = False
    # Retry is allowed only for a request that was persisted but never sent.
    # SIGHUP is GracefulOnly and a repeat cannot force the drain, so re-sending
    # to the same exact identity is idempotent; a 'signalled' record never
    # signals again.
    may_signal = record is None or record.get('state') == 'drain_requested'
    if may_signal:
        descriptor = _descriptor(profile, revision)
        live, digest = _drain_capability(descriptor, observed, trusted_sha256=trusted_sha256)
        handle = _drain_handle(observed["pid"])
        try:
            record = dict(schema=DRAIN_SCHEMA, state='drain_requested', revision=revision,
                          process=observed, executable=str(live), runtime_sha256=digest,
                          markers=sorted(marker.decode() for marker in DRAIN_MARKERS),
                          signal='SIGHUP', requested_at=time.time(), requested_by_pid=os.getpid())
            # The request is durable before any signal, so a dropped SSH
            # connection cannot lose the fact that this exact process was
            # asked to drain. A crash here is retried, never waited on.
            _drain_write(profile, record)
            if _running(profile, revision) != observed or catches_hangup(observed["pid"]) is not True:
                raise RemoteDrainError('remote_drain_identity_changed')
            # Recheck the live artifact immediately before the signal: the
            # descriptor path may have been replaced while this ran.
            if _drain_digest(_drain_executable(descriptor, observed["pid"])) != digest:
                raise RemoteDrainError('remote_drain_identity_changed')
            _drain_send(handle)
            signal_sent = True
            record = {**record, 'state': 'signalled', 'signalled_at': time.time()}
            _drain_write(profile, record)
        except ProcessLookupError:
            # The process exited between identity checks: the record stays
            # 'drain_requested' and the exit proof below decides the outcome.
            pass
        finally:
            _drain_close(handle)
    else:
        # Background polling a signalled request must stay cheap: no binary
        # hash, no marker scan, no /proc exe read. The record's schema and
        # audited digest still have to agree, and the process identity was
        # already matched above.
        digest = record.get('runtime_sha256')
        if record.get('schema') != DRAIN_SCHEMA or digest not in DRAIN_AUDITED_SHA256:
            raise RemoteDrainError('remote_drain_unverified')
        if trusted_sha256 is not None and digest != trusted_sha256:
            raise RemoteDrainError('remote_drain_unaudited')
    deadline = time.monotonic() + wait
    if _drain_wait(profile, observed, deadline):
        record = {**record, 'state': 'drained', 'exited_at': time.time(), 'lock_released': True}
        _drain_write(profile, record)
        return _drain_result('exited', None, revision, drain_requested=True, exited=True,
                             lock_released=True, idle=True, signal_sent=signal_sent,
                             markers=DRAIN_MARKERS, requested_at=record.get('requested_at'),
                             sha256=digest, record_state='drained')
    return _drain_result('draining', observed, revision, drain_requested=True, exited=False,
                         lock_released=False, idle=False, signal_sent=signal_sent,
                         markers=DRAIN_MARKERS, requested_at=record.get('requested_at'),
                         sha256=digest, record_state=record.get('state'))


def stop(profile, revision, *, expected_process=None):
    _descriptor(profile, revision)
    lock_path = profile / "native-start.lock"
    if lock_path.is_symlink():
        raise ValueError("start lock symlink")
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _stop_locked(profile, revision, expected_process=expected_process)


def _stop_locked(profile, revision, *, expected_process=None):
    """Gracefully stop one observed listener with native-start.lock held."""
    observed = _running(profile, revision)
    if expected_process is not None:
        if observed is not None and observed != expected_process:
            raise RuntimeError("Remote runtime identity changed before shutdown.")
        if (observed is None and _process_start(expected_process.get("pid")) ==
                expected_process.get("process_start")):
            raise RuntimeError("Expected remote process exit is not verified.")
    if observed is None:
        if _instance_lock_released(profile):
            return 0
        raise RuntimeError("Another runtime still owns this profile; maintenance is deferred.")
    path = socket_path(profile)
    if observed.get("socket") != str(path):
        raise RuntimeError("Remote socket identity changed.")
    deadline = time.monotonic() + 90
    # `app-server proxy` relays bytes; it does not translate WebSocket frames.
    # Use the existing bounded client directly on the exact private socket.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect(str(path))
        connection.settimeout(None)
        with connection.makefile("rb", buffering=0) as reader, connection.makefile("wb") as writer:
            try:
                pipe = WebSocketPipe(reader, writer, max_message=1024 * 1024)
                pipe.handshake(timeout=5)
                _control_request(pipe, 1, "initialize", {
                    "clientInfo": {"name": "codex_control_maintenance", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                }, deadline)
                if _running(profile, revision) != observed:
                    raise RuntimeError("Remote runtime identity changed during maintenance.")
                response = _control_request(pipe, 2, "server/managedShutdown", {
                    "processId": observed["pid"],
                }, deadline)
                if (response.get("processId") != observed["pid"] or
                        response.get("shutdownRequested") is not True or
                        response.get("writerReleaseVerified") is not True):
                    raise RuntimeError("Remote runtime did not provide a graceful shutdown proof.")
                # A closed listener is not proof of process exit. Preserve the
                # narrower acknowledged fence even if later teardown hangs or
                # this SSH connection drops; recovery still requires exit/lock.
                import launch
                launch._atomic(profile / 'native-shutdown.json', dict(schema=1,
                    process=observed, shutdown_requested=True, writer_release_verified=True,
                    acknowledged_at=time.time()))
            finally:
                # Wake the reader before closing its file, including on timeout.
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
    while time.monotonic() < deadline:
        if _process_start(observed["pid"]) != observed["process_start"]:
            if _instance_lock_released(profile):
                return 0
            # Child/OS handle teardown can finish just after the process
            # becomes a zombie. Keep waiting for both pieces of evidence.
        time.sleep(0.1)
    raise RuntimeError("Remote runtime exit and profile lock release are not both verified; settings and updates remain pending.")


def main(profile, revision, operation, *, expected_process=None, wait_seconds=None,
         trusted_sha256=None):
    import launch
    descriptor = _descriptor(profile, revision)
    executable = str(Path(descriptor["runtime"]) / "codex")
    if operation == "native-probe":
        return 0 if Path(executable).is_file() else 86
    if operation == "native-version":
        os.execve(executable, [executable, "--version"], dict(os.environ))
    if operation == "native-start":
        return start(profile, revision)
    if operation == "native-stop":
        # Old runtimes reject the RPC. Never fall back to a process-name kill.
        return stop(profile, revision)
    if operation == "native-drain":
        # Explicit shared-mode lifecycle request: one exact pidfd SIGHUP,
        # persisted before the signal, never a fallback kill and never a
        # second signal on repeat. The result is structured, not a bare exit.
        result = drain(profile, revision, expected_process=expected_process,
                       wait_seconds=wait_seconds, trusted_sha256=trusted_sha256)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if operation == "native-serve":
        path = socket_path(profile)
        return launch.run(profile, revision, ["app-server", "--listen", "unix://" + str(path)], managed_socket=path)
    if operation == "native-proxy":
        path = socket_path(profile)
        if not _ready(profile, revision, path):
            raise RemoteStartError('remote_listener_unavailable')
        _forward_agent(profile)
        env = {key: value for key, value in os.environ.items() if not key.startswith("CODEX_")}
        env["CODEX_HOME"] = str(profile / "codex")
        os.execve(executable, [executable, "app-server", "proxy", "--sock", str(path)], env)
    raise ValueError("unknown managed SSH operation")
