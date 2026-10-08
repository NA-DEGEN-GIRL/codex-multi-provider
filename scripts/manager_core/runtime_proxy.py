"""Managed, stdio-only Codex runtime launcher.

The native bootstrap inherits app stdio and calls this program. Runtime traffic
is forwarded to that same app, with a sanitized passive activity snapshot written
separately. No raw RPCs, credentials, prompts, or command output are logged here.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
import signal
import subprocess
import sys
import threading
import time
from uuid import UUID

try:
    from .app_transport import RuntimeObserver
    from .proxy_auth import AuthProxy
    from .native_account_guard import NativeAccountGuard
    from .permission_selection import PermissionSelectionProxy
    from .runtime_admin import AdminError, AdminRpcBroker, AdminServer, MaintenanceBarrier
    from .notification_policy import NotificationPolicy
    from .pipe_writer import PipeWriter
    from .serve_ledger import ProviderReturn, ServeLedger, account_tag
except ImportError:
    # The native bootstrap invokes this file by path. Load the package from its
    # sibling scripts directory so admin's shared process/DPAPI helpers retain
    # their relative imports in that production entry point too.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from manager_core.app_transport import RuntimeObserver
    from manager_core.proxy_auth import AuthProxy
    from manager_core.native_account_guard import NativeAccountGuard
    from manager_core.permission_selection import PermissionSelectionProxy
    from manager_core.runtime_admin import AdminError, AdminRpcBroker, AdminServer, MaintenanceBarrier
    from manager_core.notification_policy import NotificationPolicy
    from manager_core.pipe_writer import PipeWriter
    from manager_core.serve_ledger import ProviderReturn, ServeLedger, account_tag

MAX_FRAME_BYTES = 32 * 1024 * 1024

# The NTSTATUS exit code of a Windows process whose DLL initialization failed
# while it was still starting, before any runtime code ran (seen while a launch
# wave of many profiles started together).
STATUS_DLL_INIT_FAILED = 0xC0000142
# Such a start is replaced, once per delay, only while the app's input is still
# replayable: the runtime wrote nothing, initialize never completed, and no
# runtime child outlived this window. The jitter spreads the profiles of one
# launch wave that failed together.
START_WINDOW_SECONDS = 10.0
START_RETRY_DELAYS = (0.5, 2.0)
START_RETRY_JITTER = 0.5
START_REPLAY_LIMIT = 4 * 1024 * 1024


def transient_start_failure(exit_code, *, windows=os.name == 'nt') -> bool:
    """Whether a runtime exit is a Windows process-start failure worth another start.

    0xC0000142 as a process exit code is the loader failing before the
    runtime's own code ran, whenever this launcher noticed it: under load the
    launcher can be busy for seconds after the spawn. The start window is
    enforced by sealing the replay instead. An access violation is never
    retried: it is runtime code that crashed, and Windows Error Reporting holds
    a crashed process for about 20 s, past any early-crash window, before this
    launcher sees its exit.
    """
    if not windows or type(exit_code) is not int:
        return False
    return exit_code & 0xFFFFFFFF == STATUS_DLL_INIT_FAILED


def exit_status(code) -> int:
    """The os._exit argument (a C int) that reproduces a child's exit code.

    Windows reports NTSTATUS codes such as 0xC0000142 as unsigned DWORDs; the
    same 32 bits as a signed int give the app that exact process exit code.
    """
    code = int(code) & 0xFFFFFFFF if os.name == 'nt' else int(code)
    return code - (1 << 32) if code > 0x7FFFFFFF else code


def runtime_creation_flags() -> int:
    """Share this launcher's console with the runtime when it has one.

    The native bootstrap gives this launcher its own windowless console, and
    CREATE_NO_WINDOW made every runtime start yet another console host during
    its process start. A console-subsystem runtime started without a console
    to inherit would open a visible window, so only then keep CREATE_NO_WINDOW.
    GetConsoleWindow() is 0 for a windowless console; GetConsoleCP() is 0 only
    without any console.
    """
    if os.name != 'nt':
        return 0
    try:
        import ctypes
        if ctypes.WinDLL('kernel32').GetConsoleCP():
            return 0
    except (OSError, AttributeError):
        pass
    return subprocess.CREATE_NO_WINDOW


def runtime_environment(source: dict) -> dict:
    env = {name: value for name, value in source.items()
           if not name.upper().startswith('CODEX_MANAGER_')}
    if source.get('CODEX_MANAGER_SSH_ORIGINAL_PATH') is not None:
        for name in tuple(env):
            if name.upper() == 'PATH':
                env.pop(name)
        env['PATH'] = source['CODEX_MANAGER_SSH_ORIGINAL_PATH']
    # This non-secret registry path is a runtime configuration input. Launcher
    # metadata and the linked-account credential source remain stripped.
    if source.get('CODEX_MANAGER_RECORD_CATALOG'):
        env['CODEX_MANAGER_RECORD_CATALOG'] = source['CODEX_MANAGER_RECORD_CATALOG']
    if source.get('CODEX_MANAGER_MANAGED_SOURCES'):
        env['CODEX_MANAGER_MANAGED_SOURCES'] = source['CODEX_MANAGER_MANAGED_SOURCES']
    if source.get('CODEX_MANAGER_SHARED_CATALOG'):
        env['CODEX_MANAGER_SHARED_CATALOG'] = source['CODEX_MANAGER_SHARED_CATALOG']
    for name in ('CODEX_MANAGER_SHARED_EXECUTION', 'CODEX_MANAGER_SHARED_WRITER_ID',
                 'CODEX_MANAGER_NEW_THREAD_HOME'):
        if source.get(name):
            env[name] = source[name]
    if source.get('CODEX_MANAGER_PROJECT_ALIASES'):
        env['CODEX_MANAGER_PROJECT_ALIASES'] = source['CODEX_MANAGER_PROJECT_ALIASES']
    if source.get('CODEX_MANAGER_EXECUTION_PRESETS'):
        env['CODEX_MANAGER_EXECUTION_PRESETS'] = source['CODEX_MANAGER_EXECUTION_PRESETS']
    # The Windows sandbox grants its writable temp root (TEMP/TMP) on first
    # setup, and Windows propagates that ACE to every file already below it.
    # A small per-profile temp keeps that setup fast whatever the user's TEMP
    # holds; without the directory the runtime keeps the inherited TEMP.
    if temp := source.get('CODEX_MANAGER_RUNTIME_TEMP'):
        try:
            os.makedirs(temp, exist_ok=True)
        except OSError:
            temp = None
        if temp:
            for name in tuple(env):
                if name.upper() in ('TEMP', 'TMP'):
                    env.pop(name)
            env['TEMP'] = env['TMP'] = temp
    env['CODEX_CLI_PATH'] = source['CODEX_MANAGER_REAL_RUNTIME']
    env.pop('ELECTRON_RUN_AS_NODE', None)
    return env


def managed_client_message(message, external, permission=None, provider_return=None):
    """Restore a returning provider's settings, apply the profile model binding,
    then the remembered permission choice.

    The provider return only acts on resumes the desktop adapter marked as a
    return from another provider; the binding then still validates the effort.
    proxy() resolves it earlier, on its frontend reader outside protocol_lock,
    and passes None here.
    The permission decoration only re-attaches a selection that this profile
    observed before; a request that carries its own selection is untouched.
    """
    if provider_return is not None:
        message = provider_return(message)
    message = external.request(message)
    if permission is not None:
        message = permission.to_runtime(message)
    return message


def read_frames(stream, limit=MAX_FRAME_BYTES):
    """Read bounded newline frames; a malicious frame cannot grow memory forever."""
    pending = bytearray()
    while True:
        chunk = stream.read1(65536) if hasattr(stream, 'read1') else stream.read(65536)
        if not chunk:
            if pending:
                yield bytes(pending)
            return
        for part in chunk.splitlines(keepends=True):
            pending.extend(part)
            if len(pending) > limit:
                raise ValueError('Runtime protocol frame exceeds the configured limit.')
            if part.endswith(b'\n'):
                yield bytes(pending)
                pending.clear()


class RawInput:
    """The app's stdin without sys.stdin's BufferedReader.

    The reader stays blocked here while the app waits for a runtime that has
    already exited. Blocked in BufferedReader.read1 it holds the reader's lock,
    and interpreter shutdown then aborts closing sys.stdin (0xC0000005 replaced
    the runtime's exit code). os.read holds no lock, and stdin's descriptor is
    never closed at shutdown.
    """

    def __init__(self, descriptor):
        self.descriptor = descriptor

    def read(self, size):
        return os.read(self.descriptor, size)


class RuntimeInput:
    """Ordered input to the current runtime child, replayable until sealed.

    Until the runtime's first output, its start window, the replay bound or an
    admin write (whose admission check must not be repeated) seals it, every
    body is kept exactly as written: it already passed auth, permission,
    notification, observer and ledger handling, so a replacement child gets
    byte-identical input without any of that running twice. A failed write to
    a child that may still be replaced is held back and reported as a protocol
    gap only if no replacement follows.
    """

    def __init__(self, stream, failed, *, limit=START_REPLAY_LIMIT):
        self.lock = threading.Lock()
        self.failed, self.limit = failed, limit
        self.replay = []
        self.replay_bytes = 0
        self.deferred_gap = False
        self.closed = False
        self.generation = 0
        self.writer = PipeWriter(stream, self._failure(0))

    def _failure(self, generation):
        def failed():
            with self.lock:
                if generation != self.generation:
                    return  # The writer of a replaced child.
                if self.replay is not None:
                    self.deferred_gap = True
                    return
            self.failed()
        return failed

    def write(self, body, before_write=None):
        gap = False
        try:
            with self.lock:
                if self.closed:
                    raise OSError('Runtime pipe is closed.')
                if self.replay is not None:
                    if before_write is not None or self.replay_bytes + len(body) > self.limit:
                        gap = self._seal()
                    else:
                        self.replay.append(body)
                        self.replay_bytes += len(body)
                try:
                    self.writer.write(body, before_write)
                except OSError:
                    if self.replay is None:
                        raise
                    # Kept for a replacement; the exited child cannot read it.
        finally:
            # The seal took the deferred gap over: report it even when this
            # write to the same exited child raises.
            if gap:
                self.failed()

    def _seal(self):
        gap = self.deferred_gap
        self.replay, self.replay_bytes, self.deferred_gap = None, 0, False
        return gap

    def seal(self):
        if self.replay is None:
            return
        with self.lock:
            gap = self._seal() if self.replay is not None else False
        if gap:
            self.failed()

    def replaceable(self):
        with self.lock:
            return self.replay is not None and not self.closed

    def replace(self, spawn):
        """Start a child with spawn() and give it the kept input first.

        The lock places every concurrent write either inside the replay or
        after it. Returns the new child, or None once sealed or closed.
        """
        with self.lock:
            if self.replay is None or self.closed:
                return None
            child = spawn()
            try:
                writer = PipeWriter(child.stdin, self._failure(self.generation + 1))
            except BaseException:
                try:
                    child.kill()
                except OSError:
                    pass
                raise
            old, self.writer = self.writer, writer
            self.generation += 1
            self.deferred_gap = False
            for body in self.replay:
                try:
                    writer.write(body)
                except OSError:
                    break  # This child exited too; its writer reports that.
        old.close()
        return child

    def close(self):
        with self.lock:
            self.closed = True
            writer = self.writer
        writer.close()

    def snapshot(self):
        with self.lock:
            writer = self.writer
        return writer.snapshot()


def proxy(runtime: Path, arguments: list[str], observer_path: Path, profile_id: str,
          source_environment: dict) -> int:
    auth = AuthProxy(source_home=source_environment.get('CODEX_MANAGER_AUTH_SOURCE'),
                     expected_account_fingerprint=source_environment.get('CODEX_MANAGER_EXPECTED_ACCOUNT_FINGERPRINT'))
    native_guard = NativeAccountGuard(source_environment)
    from manager_core.external_profile import ExternalProfile
    external = ExternalProfile(source_environment)
    permission = (PermissionSelectionProxy(source_environment['CODEX_MANAGER_ROOT'], profile_id)
                  if source_environment.get('CODEX_MANAGER_ROOT') else None)
    storage_args = (['-c', 'sqlite_home=' + json.dumps(source_environment['CODEX_RECORD_HOME'])]
                    if source_environment.get('CODEX_RECORD_HOME') else [])
    creationflags = runtime_creation_flags()
    started = None

    def spawn():
        nonlocal started
        # Taken before the process exists, so the measured uptime of every
        # runtime child (also a replacement started under the input lock) can
        # only overstate its life.
        started = time.monotonic()
        return subprocess.Popen([str(runtime), *arguments, *storage_args],
                                env=runtime_environment(source_environment),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=None, creationflags=creationflags)

    child = spawn()
    # Set once the child exits. A runtime that refuses its stores exits before
    # initialize completes; stderr stays inherited (a pipe here could deadlock).
    last_exit = None
    # Process-start failures that a replacement child took over (main loop).
    start_retries = []
    from manager_core.catalog_projection import CatalogProjectionIds
    projections = CatalogProjectionIds(source_environment.get('CODEX_MANAGER_SHARED_CATALOG')
                                       or source_environment.get('CODEX_MANAGER_RECORD_CATALOG'))
    # Content-free per-request usage rows beside the observer snapshot. An
    # instances directory is only searched for other profiles' settings when
    # the observer lives in the managed layout (never an arbitrary parent).
    try:
        usage = ServeLedger(observer_path.parent, profile_id, account_tag(source_environment))
    except (OSError, ValueError, RuntimeError):
        usage = None
    instances = observer_path.parent.parent if observer_path.parent.parent.name == 'instances' else None
    provider_return = ProviderReturn(instances, usage, account_tag(source_environment), observer_path.parent.name)
    observer = RuntimeObserver(profile_id, runtime_pid=child.pid, read_only_projection=projections, usage=usage)
    from manager_core.record_edits import RecordEdits
    record_edits = RecordEdits(source_environment, projections.origins)
    notifications = NotificationPolicy()
    stop = threading.Event()
    record_signals = None
    if source_environment.get('CODEX_MANAGER_RECORD_SIGNALS'):
        from manager_core.record_signals import RecordSignals
        record_signals = RecordSignals(source_environment['CODEX_MANAGER_RECORD_SIGNALS'], profile_id)
    protocol_lock = threading.RLock()
    runtime_output = RuntimeInput(child.stdin, observer.gap)
    frontend_output = PipeWriter(sys.stdout.buffer, observer.gap)
    from manager_core.windows_sandbox_setup import SandboxSetupRecovery

    def emit_setup(message):
        with protocol_lock:
            observer.consume('server', message)
            frontend_output.write(json.dumps(message, ensure_ascii=False).encode('utf-8') + b'\n')

    sandbox_setup = (SandboxSetupRecovery(runtime, [*arguments, *storage_args],
                     runtime_environment(source_environment), emit_setup) if os.name == 'nt' else None)
    admin_server = None
    admin_state = 'disabled'
    app_bridge_state = 'pending' if source_environment.get('CODEX_APP_TOOLS_PIPE_PATH') else 'not_provided'
    generation = source_environment.get('CODEX_MANAGER_GENERATION')
    try:
        maintenance = MaintenanceBarrier(generation) if generation else None
    except AdminError:
        maintenance = None

    def account_ready():
        if external.binding: return True
        return (auth.state == 'ready') if auth.bound else (observer.account.get('state') == 'signed_in' and native_guard.matches())

    def write_message(message, direction, before_write=None):
        if direction == 'client':
            # A marked provider return was already resolved on the frontend
            # reader (pump), outside protocol_lock.
            message = notifications.to_runtime(managed_client_message(message, external, permission))
        body = json.dumps(message, separators=(',', ':'), ensure_ascii=False).encode('utf-8') + b'\n'
        runtime_output.write(body, before_write)
        observer.consume(direction, message)
        if maintenance is not None and direction == 'client':
            maintenance.observe_client(message)

    def admin_send(message, deadline, lease):
        with protocol_lock:
            # This check happens after lock acquisition. A timed-out close must
            # not be written later simply because frontend traffic held the lock.
            if time.monotonic() >= deadline:
                raise AdminError('timeout')
            if stop.is_set() or child.poll() is not None:
                raise AdminError('closed')
            state = observer.snapshot()
            if not (account_ready() and state['initialized'] and state['stream_complete'] and state['connected']):
                raise AdminError('not_ready')
            if maintenance is not None:
                maintenance.authorize_admin(message['method'], lease)
            def admitted_in_time():
                with protocol_lock:
                    if time.monotonic() < deadline and not stop.is_set():
                        return True
                    rejected = {'id': message['id'], 'error': {
                        'code': -32043, 'message': 'Administration request expired before transmission.'}}
                    observer.consume('server', rejected)
                    admin_broker.consume_runtime(rejected)
                    return False
            write_message(message, 'client', admitted_in_time)

    admin_broker = AdminRpcBroker(admin_send)

    def admin_dispatch(method, params, timeout, lease):
        if method.startswith('manager/maintenance/'):
            deadline = time.monotonic() + timeout
            with protocol_lock:
                if time.monotonic() >= deadline:
                    raise AdminError('timeout')
                if maintenance is None or stop.is_set() or child.poll() is not None:
                    raise AdminError('closed')
                if method == 'manager/maintenance/acquire' and admin_broker.pending_mutation_count():
                    raise AdminError('busy')
                status = maintenance.request(method, params, observer.snapshot(), account_ready())
                status['pendingMutationCount'] += admin_broker.pending_mutation_count()
                return status
        return admin_broker.request(method, params, timeout, lease)

    def reject_frontend(message, code, explanation):
        if type(message.get('id')) in (int, str):
            body = json.dumps({'id': message['id'], 'error': {'code': code, 'message': explanation}},
                              separators=(',', ':')).encode('utf-8') + b'\n'
            frontend_output.write(body)

    def snapshot():
        # Auth state is an enum; never include token-source paths or auth payloads.
        target = observer_path
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + '.tmp-' + str(os.getpid()))
        with protocol_lock:
            value = observer.snapshot()
            value['generation'] = source_environment.get('CODEX_MANAGER_GENERATION')
            app_pid = source_environment.get('CODEX_MANAGER_APP_PROCESS_ID')
            if app_pid is not None and app_pid.isdigit():
                value['app_process_id'] = int(app_pid)
            value['auth_binding'] = {'bound': auth.bound, 'state': auth.state, 'reason': auth.reason}
            if native_guard.enabled:
                value['native_account'] = {'matches': native_guard.matches(),
                                          'account_fingerprint': native_guard.expected}
            value['admin_channel'] = {'state': admin_state}
            value['native_app_bridge'] = {'state': app_bridge_state}
            value['shared_catalog'] = {'enabled': bool(source_environment.get('CODEX_MANAGER_SHARED_CATALOG'))}
            value['canonical_storage'] = {'enabled': bool(source_environment.get('CODEX_RECORD_HOME'))}
            value['record_edits_version'] = 1
            value['runtime_observer_version'] = 2
            value['execution_presets_version'] = (1 if source_environment.get('CODEX_MANAGER_EXECUTION_PRESETS_VERSION') == '1' else 0)
            value['transport'] = {'to_runtime': runtime_output.snapshot(),
                                  'to_app': frontend_output.snapshot()}
            value['shared_execution_version'] = 1 if source_environment.get('CODEX_MANAGER_SHARED_EXECUTION') == '1' else 0
            if usage is not None:
                value['serve_ledger'] = {**usage.status(), 'provider_returns_restored': provider_return.restored}
            if maintenance is not None:
                # Status has no lease token or pipe authentication material.
                value['maintenance'] = maintenance.status(value, account_ready())
                value['maintenance']['pendingMutationCount'] += admin_broker.pending_mutation_count()
            if auth.account_fingerprint is not None:
                value['auth_binding']['account_fingerprint'] = auth.account_fingerprint
            if last_exit is not None:
                value['last_exit'] = dict(last_exit)
            if start_retries:
                value['start_retries'] = [dict(item) for item in start_retries]
        try:
            temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
            os.replace(temporary, target)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def heartbeat():
        while not stop.wait(2):
            try:
                with protocol_lock:
                    outcome = auth.poll()
                    for outgoing in outcome.runtime:
                        write_message(outgoing, 'client')
                    for outgoing in outcome.frontend:
                        if not notifications.to_frontend(outgoing):
                            continue
                        body = json.dumps(outgoing, separators=(',', ':'), ensure_ascii=False).encode('utf-8') + b'\n'
                        frontend_output.write(body)
            except OSError:
                observer.gap()
            # A Windows reader can briefly prevent atomic replacement of the
            # status file. That is not missing runtime traffic. Keep the last
            # complete snapshot and retry publication on the next heartbeat.
            try:
                snapshot()
            except OSError:
                pass

    def pump(stream, direction):
        try:
            for frame in read_frames(stream):
                if direction == 'runtime':
                    # This runtime's output may reach the app: never replace it.
                    runtime_output.seal()
                if stop.is_set():
                    break
                try:
                    message = json.loads(frame)
                    if not isinstance(message, dict):
                        raise ValueError('Expected a JSON object.')
                except (ValueError, UnicodeError, RecursionError):
                    observer.gap()
                    if auth.bound or native_guard.enabled or (maintenance is not None and maintenance.transaction_id is not None):
                        # Malformed input must not bypass the account gate.
                        raise ValueError('Invalid bound runtime protocol message.')
                    target = runtime_output if direction == 'frontend' else frontend_output
                    target.write(frame)
                    continue
                if direction == 'frontend':
                    if (message.get('method') in ('thread/start', 'thread/resume', 'thread/fork')
                            and source_environment.get('CODEX_MANAGER_ROOT')):
                        try:
                            from manager_core.project_trust import sync as sync_project_trust
                            sync_project_trust(source_environment['CODEX_MANAGER_ROOT'], profile_id)
                        except (OSError, ValueError):
                            pass  # The runtime still enforces its own trust check.
                    # Restoring a returning provider's settings reads other
                    # profiles' files. Only this reader waits for it; the
                    # runtime-to-app stream needs protocol_lock and never does.
                    # On failure the runtime ignores the unknown marker field.
                    try:
                        message = provider_return(message)
                    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
                        pass
                with protocol_lock:
                    if direction == 'runtime':
                        if record_signals is not None:
                            record_signals.observe(message)
                        if permission is not None:
                            permission.from_runtime(message)
                        observer.consume('server', message)
                        if maintenance is not None:
                            maintenance.observe_runtime(message)
                        if admin_broker.consume_runtime(message):
                            continue
                    elif AdminRpcBroker.is_reserved_request(message):
                        reject_frontend(message, -32043, 'Managed runtime request IDs are reserved.')
                        continue
                    elif maintenance is not None and maintenance.blocks(message):
                        reject_frontend(message, -32044, 'The managed instance is preparing an update. New work is temporarily paused.')
                        continue
                    elif native_guard.blocks(message):
                        reject_frontend(message, -32045, '다시 로그인하려면 이 프로필의 Codex 창을 닫고, 작업 공간의 같은 프로필에서 ‘이 프로필에 로그인’을 누르세요. 등록된 계정으로 로그인하면 작업을 계속할 수 있습니다.')
                        continue
                    outcome = auth.process(direction, message)
                    for outgoing in outcome.runtime:
                        if sandbox_setup is not None and sandbox_setup.request(outgoing):
                            continue
                        edits = (record_edits.handle(outgoing) if record_edits.catalog
                                 and outgoing.get('method') == 'thread/name/set'
                                 and observer.initialized and account_ready() else None)
                        if edits is not None:
                            observer.consume('client', outgoing)
                            for edited in edits:
                                observer.consume('server', edited)
                                if notifications.to_frontend(edited):
                                    body = json.dumps(edited, ensure_ascii=False, separators=(',', ':')).encode('utf-8') + b'\n'
                                    frontend_output.write(body)
                            continue
                        write_message(outgoing, 'client')
                    for outgoing in outcome.frontend:
                        if sandbox_setup is not None and not sandbox_setup.response(outgoing):
                            continue
                        if not notifications.to_frontend(outgoing):
                            continue
                        # Runtime messages have already been observed above. Proxy
                        # errors carry no runtime evidence and are not added again.
                        body = json.dumps(outgoing, separators=(',', ':'), ensure_ascii=False).encode('utf-8') + b'\n'
                        frontend_output.write(body)
        except (OSError, ValueError, TypeError, RecursionError):
            if direction == 'runtime':
                runtime_output.seal()
            observer.gap()
        finally:
            if direction == 'frontend':
                runtime_output.close()

    # Windows broadcasts Ctrl+C to processes attached to the same console.
    # Do not generate a duplicate event from this intermediate launcher.
    if hasattr(signal, 'SIGINT'):
        signal.signal(signal.SIGINT, lambda *_: None)
    # The runtime shares this console. A Ctrl+Break raised there must not end
    # this launcher before it records the runtime's own exit.
    if hasattr(signal, 'SIGBREAK'):
        signal.signal(signal.SIGBREAK, lambda *_: None)
    if os.name != 'nt':
        signal.signal(signal.SIGTERM, lambda *_: child.send_signal(signal.SIGTERM))
    admin_wanted = bool(os.name == 'nt' and maintenance is not None and source_environment.get('CODEX_MANAGER_ROOT'))
    app_bridge_requested = False

    def start_admin():
        nonlocal admin_server, admin_state
        try:
            managed_root = Path(source_environment['CODEX_MANAGER_ROOT']).resolve()
            expected_observer = managed_root / 'work/control-center/instances' / profile_id / 'runtime-state.json'
            if observer_path.resolve() != expected_observer.resolve():
                raise AdminError('unavailable')
            admin_server = AdminServer(managed_root, profile_id, generation, child.pid, admin_dispatch).start()
            admin_state = 'ready'
        except (AdminError, OSError, ValueError, RuntimeError):
            # UI/stdin forwarding can continue, but update/handoff must fail
            # closed when authenticated administration is unavailable (also
            # when its accept thread could not start).
            admin_state = 'unavailable'

    def publish_app_connection():
        nonlocal app_bridge_state
        try:
            from manager_core.native_app_bridge import publish
            publish(source_environment)
            app_bridge_state = 'ready'
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError):
            # Runtime traffic remains usable. Navigation requires its own
            # fresh descriptor and verified app PID; no global fallback.
            app_bridge_state = 'unavailable'

    def start_app_bridge():
        nonlocal app_bridge_requested
        if (app_bridge_requested or os.name != 'nt' or admin_state != 'ready'
                or not source_environment.get('CODEX_APP_TOOLS_PIPE_PATH')):
            return
        app_bridge_requested = True
        threading.Thread(target=publish_app_connection, daemon=True).start()

    def rebind_admin():
        # The descriptor names the runtime process; the manager matches it
        # against this snapshot's runtime_process_id.
        nonlocal admin_server, admin_state
        if admin_server is not None:
            try:
                admin_server.rebind_runtime(child.pid)
                return
            except (AdminError, OSError, ValueError, RuntimeError):
                admin_server.close()
                admin_server = None
                admin_state = 'unavailable'
        if admin_wanted:
            # Also when the first child had already exited before the
            # endpoint could bind to it.
            start_admin()
            start_app_bridge()

    if admin_wanted:
        start_admin()
    try:
        snapshot()
    except OSError:
        pass  # Status publication is retried; app-server traffic can still start.
    incoming = threading.Thread(target=pump, args=(RawInput(sys.stdin.fileno()), 'frontend'), daemon=True)
    outgoing = threading.Thread(target=pump, args=(child.stdout, 'runtime'), daemon=True)
    ticker = threading.Thread(target=heartbeat, daemon=True)
    incoming.start()
    outgoing.start()
    ticker.start()
    start_app_bridge()
    while True:
        try:
            if runtime_output.replaceable():
                exit_code = child.wait(timeout=max(0.0, started + START_WINDOW_SECONDS - time.monotonic()))
            else:
                exit_code = child.wait()
        except subprocess.TimeoutExpired:
            runtime_output.seal()  # A start that outlived its window is never replaced.
            exit_code = child.wait()
        uptime = time.monotonic() - started
        exited_at = datetime.now(timezone.utc).isoformat()
        outgoing.join(timeout=5)
        attempt = len(start_retries)
        # Replace only a Windows process-start failure: the runtime's reader
        # finished without reading any output, initialize never completed, and
        # the app still waits with its input intact.
        if (attempt >= len(START_RETRY_DELAYS) or outgoing.is_alive() or observer.initialize_succeeded
                or not transient_start_failure(exit_code) or not runtime_output.replaceable()):
            break
        time.sleep(START_RETRY_DELAYS[attempt] * (1 + random.random() * START_RETRY_JITTER))
        with protocol_lock:
            try:
                # spawn() also restarts the uptime clock.
                replacement = runtime_output.replace(spawn)
            except (OSError, ValueError, RuntimeError):
                replacement = None
            if replacement is not None:
                start_retries.append(dict(exit_code=exit_code, uptime_ms=int(uptime * 1000), exited_at=exited_at))
                child = replacement
                with observer.lock:
                    observer.runtime_pid = child.pid
        if replacement is None:
            break
        try:
            outgoing = threading.Thread(target=pump, args=(child.stdout, 'runtime'), daemon=True)
            outgoing.start()
            rebind_admin()
        except Exception:
            # A replacement without its output reader, or whose endpoint
            # failed unexpectedly, must not run on. The app gets the code of
            # the start failure it replaced.
            try:
                child.kill()
            except OSError:
                pass
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            break
        try:
            snapshot()
        except OSError:
            pass
    # A failed write to a child that was not replaced is a protocol gap now.
    runtime_output.seal()
    last_exit = dict(exit_code=exit_code, uptime_ms=int(uptime * 1000),
                     initialize_completed=observer.initialize_succeeded, exited_at=exited_at)
    if start_retries:
        last_exit['retries'] = len(start_retries)
    stop.set()
    # Only the app's EOF closed it before; after an early runtime exit its
    # writer thread was left behind.
    runtime_output.close()
    if record_signals is not None:
        record_signals.close()
    admin_broker.close()
    if admin_server is not None:
        admin_server.close()
        admin_state = 'closed'
    ticker.join(timeout=3)
    frontend_output.close()
    frontend_output.thread.join(timeout=5)
    observer.disconnected()
    if usage is not None:
        usage.close()
    projections.close()
    try:
        snapshot()
    except OSError:
        pass
    return exit_code


def main(arguments=None) -> int:
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    if arguments[:1] == ['--']:
        arguments.pop(0)
    try:
        runtime = Path(os.environ['CODEX_MANAGER_REAL_RUNTIME'])
        if not runtime.is_absolute() or not runtime.is_file():
            raise ValueError('A managed runtime executable is required.')
        # Helper and version invocations remain ordinary CLI calls.
        if 'app-server' not in arguments:
            return subprocess.call([str(runtime), *arguments], env=runtime_environment(os.environ))
        profile_id = str(UUID(os.environ['CODEX_MANAGER_PROFILE_ID']))
        root = Path(os.environ.get('CODEX_MANAGER_ROOT', Path(__file__).resolve().parents[2])).resolve()
        expected = root / 'work/control-center/instances' / profile_id / 'runtime-state.json'
        observer_path = Path(os.environ['CODEX_MANAGER_OBSERVER_PATH']).resolve()
        if observer_path != expected.resolve():
            raise ValueError('The runtime observer path does not match its managed profile.')
        return proxy(runtime, arguments, observer_path, profile_id, dict(os.environ))
    except (OSError, ValueError, KeyError):
        # Exception messages can include caller paths or values. Return a fixed
        # diagnostic, while detailed non-secret state lives in the manager.
        print('Codex managed runtime could not start. Check Control Center diagnostics.', file=sys.stderr)
        return 78


if __name__ == '__main__':
    try:
        code = main()
    except BaseException:
        # Leave through os._exit below in any case. The exception text can
        # quote caller paths or protocol values, so only a fixed line is shown.
        code = 70
        try:
            print('Codex managed runtime proxy stopped unexpectedly. Check Control Center diagnostics.',
                  file=sys.stderr)
        except (OSError, ValueError, AttributeError):
            pass
    try:
        sys.stderr.flush()
    except (OSError, ValueError, AttributeError):
        pass
    # The observer state is final. Daemon threads can still be blocked on the
    # app's pipes, and finalization must not wait on (or abort over) their io
    # locks; the app gets the runtime's exact exit code.
    os._exit(exit_status(code))
