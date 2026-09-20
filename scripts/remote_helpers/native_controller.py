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
import socket
import stat
import subprocess
import sys
import time

from ws_client import WebSocketPipe


class RemoteStartError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__('Remote listener startup failed (' + code + ').')


class RemoteMaintenanceError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__('Remote maintenance proof is unavailable (' + code + ').')


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
        raise RuntimeError("A different revision is still running; wait for its jobs to finish.")
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
            if (method == 'thread/managedIdleStatus' and isinstance(error, dict)
                    and error.get('code') == -32600
                    and error.get('message') == 'root actor has no immutable managed source binding'):
                raise RemoteMaintenanceError('remote_idle_binding_missing')
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


def main(profile, revision, operation):
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
    if operation == "native-serve":
        path = socket_path(profile)
        return launch.run(profile, revision, ["app-server", "--listen", "unix://" + str(path)], managed_socket=path)
    if operation == "native-proxy":
        path = socket_path(profile)
        if not _ready(profile, revision, path):
            raise RuntimeError("Remote managed listener is not ready.")
        _forward_agent(profile)
        env = {key: value for key, value in os.environ.items() if not key.startswith("CODEX_")}
        env["CODEX_HOME"] = str(profile / "codex")
        os.execve(executable, [executable, "app-server", "proxy", "--sock", str(path)], env)
    raise ValueError("unknown managed SSH operation")
