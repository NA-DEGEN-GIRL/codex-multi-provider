"""Managed, stdio-only Codex runtime launcher.

The native bootstrap inherits app stdio and calls this program. Runtime traffic
is forwarded to that same app, with a sanitized passive activity snapshot written
separately. No raw RPCs, credentials, prompts, or command output are logged here.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
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
    from .runtime_admin import AdminError, AdminRpcBroker, AdminServer, MaintenanceBarrier
    from .notification_policy import NotificationPolicy
    from .pipe_writer import PipeWriter
except ImportError:
    # The native bootstrap invokes this file by path. Load the package from its
    # sibling scripts directory so admin's shared process/DPAPI helpers retain
    # their relative imports in that production entry point too.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from manager_core.app_transport import RuntimeObserver
    from manager_core.proxy_auth import AuthProxy
    from manager_core.native_account_guard import NativeAccountGuard
    from manager_core.runtime_admin import AdminError, AdminRpcBroker, AdminServer, MaintenanceBarrier
    from manager_core.notification_policy import NotificationPolicy
    from manager_core.pipe_writer import PipeWriter

MAX_FRAME_BYTES = 32 * 1024 * 1024


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
    env['CODEX_CLI_PATH'] = source['CODEX_MANAGER_REAL_RUNTIME']
    env.pop('ELECTRON_RUN_AS_NODE', None)
    return env


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


def proxy(runtime: Path, arguments: list[str], observer_path: Path, profile_id: str,
          source_environment: dict) -> int:
    auth = AuthProxy(source_home=source_environment.get('CODEX_MANAGER_AUTH_SOURCE'),
                     expected_account_fingerprint=source_environment.get('CODEX_MANAGER_EXPECTED_ACCOUNT_FINGERPRINT'))
    native_guard = NativeAccountGuard(source_environment)
    from manager_core.external_profile import ExternalProfile
    external = ExternalProfile(source_environment)
    storage_args = (['-c', 'sqlite_home=' + json.dumps(source_environment['CODEX_RECORD_HOME'])]
                    if source_environment.get('CODEX_RECORD_HOME') else [])
    child = subprocess.Popen([str(runtime), *arguments, *storage_args],
                             env=runtime_environment(source_environment),
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=None, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    from manager_core.catalog_projection import CatalogProjectionIds
    projections = CatalogProjectionIds(source_environment.get('CODEX_MANAGER_SHARED_CATALOG')
                                       or source_environment.get('CODEX_MANAGER_RECORD_CATALOG'))
    observer = RuntimeObserver(profile_id, runtime_pid=child.pid, read_only_projection=projections)
    from manager_core.record_edits import RecordEdits
    record_edits = RecordEdits(source_environment, projections.origins)
    notifications = NotificationPolicy()
    stop = threading.Event()
    record_signals = None
    if source_environment.get('CODEX_MANAGER_RECORD_SIGNALS'):
        from manager_core.record_signals import RecordSignals
        record_signals = RecordSignals(source_environment['CODEX_MANAGER_RECORD_SIGNALS'], profile_id)
    protocol_lock = threading.RLock()
    runtime_output = PipeWriter(child.stdin, observer.gap)
    frontend_output = PipeWriter(sys.stdout.buffer, observer.gap)
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
            message = notifications.to_runtime(external.request(message))
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
            value['transport'] = {'to_runtime': runtime_output.snapshot(),
                                  'to_app': frontend_output.snapshot()}
            value['shared_execution_version'] = 1 if source_environment.get('CODEX_MANAGER_SHARED_EXECUTION') == '1' else 0
            if maintenance is not None:
                # Status has no lease token or pipe authentication material.
                value['maintenance'] = maintenance.status(value, account_ready())
                value['maintenance']['pendingMutationCount'] += admin_broker.pending_mutation_count()
            if auth.account_fingerprint is not None:
                value['auth_binding']['account_fingerprint'] = auth.account_fingerprint
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
                with protocol_lock:
                    if direction == 'runtime':
                        if record_signals is not None:
                            record_signals.observe(message)
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
                        if not notifications.to_frontend(outgoing):
                            continue
                        # Runtime messages have already been observed above. Proxy
                        # errors carry no runtime evidence and are not added again.
                        body = json.dumps(outgoing, separators=(',', ':'), ensure_ascii=False).encode('utf-8') + b'\n'
                        frontend_output.write(body)
        except (OSError, ValueError, TypeError, RecursionError):
            observer.gap()
        finally:
            if direction == 'frontend':
                runtime_output.close()

    # Windows broadcasts Ctrl+C to processes attached to the same console.
    # Do not generate a duplicate event from this intermediate launcher.
    if hasattr(signal, 'SIGINT'):
        signal.signal(signal.SIGINT, lambda *_: None)
    if os.name != 'nt':
        signal.signal(signal.SIGTERM, lambda *_: child.send_signal(signal.SIGTERM))
    if os.name == 'nt' and maintenance is not None and source_environment.get('CODEX_MANAGER_ROOT'):
        try:
            managed_root = Path(source_environment['CODEX_MANAGER_ROOT']).resolve()
            expected_observer = managed_root / 'work/control-center/instances' / profile_id / 'runtime-state.json'
            if observer_path.resolve() != expected_observer.resolve():
                raise AdminError('unavailable')
            admin_server = AdminServer(managed_root, profile_id, generation, child.pid, admin_dispatch).start()
            admin_state = 'ready'
        except (AdminError, OSError, ValueError):
            # UI/stdin forwarding can continue, but update/handoff must fail
            # closed when authenticated administration is unavailable.
            admin_state = 'unavailable'
    try:
        snapshot()
    except OSError:
        pass  # Status publication is retried; app-server traffic can still start.
    incoming = threading.Thread(target=pump, args=(sys.stdin.buffer, 'frontend'), daemon=True)
    outgoing = threading.Thread(target=pump, args=(child.stdout, 'runtime'), daemon=True)
    ticker = threading.Thread(target=heartbeat, daemon=True)
    incoming.start()
    outgoing.start()
    ticker.start()
    if os.name == 'nt' and source_environment.get('CODEX_APP_TOOLS_PIPE_PATH') and admin_state == 'ready':
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
        threading.Thread(target=publish_app_connection, daemon=True).start()
    exit_code = child.wait()
    outgoing.join(timeout=5)
    stop.set()
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
    raise SystemExit(main())
