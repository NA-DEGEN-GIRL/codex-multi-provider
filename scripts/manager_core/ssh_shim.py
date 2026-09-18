"""Process-scoped adapter for the unmodified Codex 26.903.9818.0 SSH transport.

Only complete, recognized native management payloads are replaced. The original
SSH options and eight-byte synchronization marker survive unchanged. This module
never opens a network connection during classification or environment preparation.
Unknown native management variants fail closed; ordinary OpenSSH is passed through.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections import deque
from contextlib import nullcontext
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
from uuid import UUID


SUPPORTED_APP_VERSION = "26.903.9818.0"
SUPPORTED_APP_VERSIONS = {SUPPORTED_APP_VERSION, "26.908.4834.0"}
ADAPTER_VERSION = "codex-native-ssh-26.903.9818.0-v1"
PATH_PREFIX = 'PATH="${CODEX_INSTALL_DIR:-$HOME/.local/bin}:$PATH"; export PATH'
HOME_PREFIX = 'CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"; export CODEX_HOME'
CONTROL = '"${CODEX_HOME:-$HOME/.codex}/app-server-control"'
LOG = '"${CODEX_HOME:-$HOME/.codex}/app-server-control/app-server.log"'
AGENT_SOCKET = '"${CODEX_HOME:-$HOME/.codex}/app-server-control/forwarded-ssh-agent.sock"'
AGENT_FORWARD = ('if [ -S "${SSH_AUTH_SOCK:-}" ]; then ln -sfn -- "$SSH_AUTH_SOCK" '
                 + AGENT_SOCKET + '; elif [ ! -S ' + AGENT_SOCKET
                 + ' ]; then rm -f -- ' + AGENT_SOCKET + '; fi')
ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
REVISION = re.compile(r"[0-9a-f]{64}\Z")
NATIVE_MARKER = re.compile(r"printf '%b' '((?:\\[0-3][0-7]{2}){8})'; ")
OPTIONS_WITH_VALUE = set("BbcDEeFIiJLlmOopQRSWw")
OPTIONS_NO_VALUE = set("1246AaCfGgKkMNnqsTtVvXxYy")


class ShimError(RuntimeError):
    """Messages and codes are static and never contain command text or secrets."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def quote_always(value: str) -> str:
    """The native OC/DC outer quoting, distinct from its command argument quote."""
    return "'" + value.replace("'", "'\\''") + "'"


def native_quote(value: str) -> str:
    # Native n.Wn/ez; current codex bare command and generated regexes contain no
    # apostrophes. Implement the full public quote so fixtures remain explicit.
    if value == "":
        return "''"
    if not re.search(r"[^\w@%\-+=:,./]", value, flags=re.ASCII):
        return value
    result = "'" + re.sub(r"('+)", lambda m: "'\"" + m.group(1) + "\"'", value) + "'"
    return re.sub(r"^''|''$", "", result)


def native_wrapper() -> str:
    inner = 'exec /bin/sh -c "$CODEX_REMOTE_PAYLOAD"'
    csh = '; '.join(['set loginsh=1', 'if ( -r /etc/csh.login ) source /etc/csh.login',
                     'if ( -r ~/.login ) source ~/.login', inner])
    return ' '.join([
        'if [ -z "$SHELL" ] || [ ! -x "$SHELL" ]; then echo "Codex remote SSH requires SHELL to point to an executable login shell" >&2; exit 127; fi;',
        'CODEX_REMOTE_PAYLOAD="$1"; export CODEX_REMOTE_PAYLOAD;',
        'case "${SHELL##*/}" in',
        'csh|tcsh) exec "$SHELL" -i -c ' + quote_always(csh) + ' ;;',
        'nu) exec "$SHELL" -l -i -c ' + quote_always('exec /bin/sh -c $env.CODEX_REMOTE_PAYLOAD') + ' ;;',
        'fish|xonsh) exec "$SHELL" -l -i -c ' + quote_always(inner) + ' ;;',
        '*) exec "$SHELL" -l -i -c ' + quote_always(HOME_PREFIX + '; ' + inner) + ' ;;',
        'esac',
    ])


def native_command(body: str, marker: bytes) -> str:
    """Fixture/diagnostic constructor; production uses the GUI's original marker."""
    if len(marker) != 8:
        raise ValueError("The native marker must contain eight bytes.")
    octal = ''.join('\\' + format(byte, '03o') for byte in marker)
    payload = "printf '%b' " + quote_always(octal) + '; ' + PATH_PREFIX + '; ' + body
    return 'sh -c ' + quote_always(native_wrapper()) + ' sh ' + quote_always(payload)


def native_bodies(cli: str = "codex") -> dict[str, str]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", cli):
        raise ValueError("Only a bare native CLI command is supported.")
    command = native_quote(cli)
    return {
        'native-probe': 'if command -v ' + command + ' >/dev/null 2>&1; then exit 0; fi; exit 86',
        'native-version': command + ' --version',
        'native-start': ('if [ "${CODEX_SSH_SKIP_APP_SERVER_BOOT:-}" = "true" ]; then exit 0; fi; '
                         '(umask 077; mkdir -p -- ' + CONTROL + ' && (pkill -9 -U "$(id -u)" -f '
                         + native_quote(cli + '.*[d]esktop-ssh-websocket-v0.sock') + ' || true) && '
                         + AGENT_FORWARD + ' && : >' + LOG + ') && SSH_AUTH_SOCK=' + AGENT_SOCKET
                         + ' nohup ' + command + ' -c features.code_mode_host=true app-server --listen '
                         + native_quote('unix://') + ' >' + LOG + ' 2>&1 &'),
        'native-proxy': AGENT_FORWARD + ' && exec ' + command + ' app-server proxy',
        'native-stop': 'pkill -9 -U "$(id -u)" -f ' + native_quote(cli + '.* app-server.* --listen'),
        'native-platform': 'uname -s',
    }


@dataclass(frozen=True)
class Invocation:
    destination: str | None
    command_index: int | None
    configuration_only: bool = False
    understood: bool = True


def parse_invocation(arguments: list[str]) -> Invocation:
    """Locate destination without interpreting option values or evaluating SSH config."""
    index = 0
    configuration_only = False
    while index < len(arguments):
        argument = arguments[index]
        if argument == '--':
            index += 1
            break
        if not argument.startswith('-') or argument == '-':
            break
        if argument.startswith('--') or len(argument) == 1:
            return Invocation(None, None, understood=False)
        position = 1
        while position < len(argument):
            flag = argument[position]
            if flag in 'GQV':
                configuration_only = True
            if flag in OPTIONS_WITH_VALUE:
                if position + 1 == len(argument):
                    index += 1
                    if index >= len(arguments):
                        return Invocation(None, None, understood=False)
                break
            if flag not in OPTIONS_NO_VALUE:
                return Invocation(None, None, understood=False)
            position += 1
        index += 1
    if index >= len(arguments):
        return Invocation(None, None, configuration_only)
    return Invocation(arguments[index], index + 1 if index + 1 < len(arguments) else None,
                      configuration_only)


def looks_native(value: str) -> bool:
    return (any(marker in value for marker in ('CODEX_REMOTE_PAYLOAD', 'CODEX_SSH_SKIP_APP_SERVER_BOOT',
                                               'Codex remote SSH requires SHELL'))
            or ('pkill -9 -U' in value and ('desktop-ssh-websocket-v0.sock' in value
                                            or ('/app-server-control' in value and 'nohup' in value))))


def decode_native(command: str, cli: str = 'codex') -> tuple[str, str, str]:
    """Return operation, unchanged octal printf prefix, original outer wrapper."""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        raise ShimError('native_wrapper_changed', 'The native SSH command wrapper is not recognized.') from None
    if (len(tokens) != 5 or tokens[:2] != ['sh', '-c'] or tokens[3] != 'sh'
            or tokens[2] != native_wrapper()):
        raise ShimError('native_wrapper_changed', 'The native SSH command wrapper is not recognized.')
    match = NATIVE_MARKER.match(tokens[4])
    if not match:
        raise ShimError('native_marker_changed', 'The native SSH synchronization marker is not recognized.')
    prefix = match.group(0)
    remainder = tokens[4][len(prefix):]
    if not remainder.startswith(PATH_PREFIX + '; '):
        raise ShimError('native_path_prefix_changed', 'The native SSH command prefix is not recognized.')
    body = remainder[len(PATH_PREFIX) + 2:]
    for operation, expected in native_bodies(cli).items():
        if body == expected:
            return operation, prefix, tokens[2]
    raise ShimError('native_command_changed', 'This native SSH operation requires an updated management adapter.')


def validate_binding(binding: dict, profile_id: str) -> dict:
    if not isinstance(binding, dict) or binding.get('profile_id') != profile_id:
        raise ShimError('binding_profile_mismatch', 'The SSH binding belongs to another profile.')
    alias = binding.get('alias')
    if not isinstance(alias, str) or not ALIAS.fullmatch(alias):
        raise ShimError('invalid_binding', 'The SSH binding alias is invalid.')
    revision = binding.get('revision')
    if not isinstance(revision, str) or not REVISION.fullmatch(revision):
        raise ShimError('invalid_binding', 'The SSH binding revision is invalid.')
    paths = {}
    for key in ('remote_python', 'remote_launcher'):
        value = binding.get(key)
        if (not isinstance(value, str) or not value.startswith('/') or '\\' in value
                or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise ShimError('invalid_binding', 'The SSH binding path is invalid.')
        path = PurePosixPath(value)
        if '..' in path.parts or str(path) != value:
            raise ShimError('invalid_binding', 'The SSH binding path is invalid.')
        paths[key] = value
    suffix = '/.local/share/codex-control-center/profiles/' + profile_id + '/launch.py'
    if not paths['remote_launcher'].endswith(suffix):
        raise ShimError('invalid_binding', 'The SSH launcher is outside its managed profile.')
    return {'alias': alias, 'profile_id': profile_id, 'revision': revision, **paths}


def route_arguments(arguments: list[str], manifest: dict) -> tuple[list[str], dict]:
    invocation = parse_invocation(arguments)
    if invocation.configuration_only:
        return list(arguments), {'operation': 'passthrough'}
    if manifest.get('native_compatible') is False:
        # A desktop update need not change SSH. For unknown app bundles accept
        # only a fully decoded native command; never fall through to passthrough.
        if (manifest.get('native_command_validation') != 1 or not invocation.understood
                or invocation.command_index != len(arguments) - 1):
            raise ShimError('unsupported_native_ssh_version', '이 SSH 명령 형식의 호환성을 확인하지 못했습니다.')
        decode_native(arguments[-1], manifest.get('native_cli', 'codex'))
    if not invocation.understood:
        if any(looks_native(arg) for arg in arguments):
            raise ShimError('native_ssh_arguments_changed', 'The native SSH argument layout is not recognized.')
        return list(arguments), {'operation': 'passthrough'}
    if invocation.command_index is None:
        return list(arguments), {'operation': 'passthrough'}
    tail = arguments[invocation.command_index:]
    if not any(looks_native(arg) for arg in tail):
        return list(arguments), {'operation': 'passthrough'}
    if len(tail) != 1:
        raise ShimError('native_ssh_arguments_changed', 'The native SSH command layout is not recognized.')
    if manifest.get('adapter') != ADAPTER_VERSION:
        raise ShimError('adapter_version_mismatch', 'The native SSH adapter version is not compatible.')
    profile_id = str(UUID(manifest.get('profile_id', '')))
    bindings = [validate_binding(item, profile_id) for item in manifest.get('bindings', [])]
    matching = [item for item in bindings if item['alias'] == invocation.destination]
    if len(matching) != 1:
        if invocation.destination in manifest.get('pending_policy_hosts', []):
            raise ShimError('ssh_policy_pending', 'This host must apply the selected profile model settings before SSH reconnects.')
        raise ShimError('host_binding_required', 'Prepare this SSH host for the selected profile in Control Center first.')
    binding = matching[0]
    operation, marker, outer = decode_native(tail[0], manifest.get('native_cli', 'codex'))
    if operation == 'native-platform':
        # Read-only native installer platform detection. Actual installer payloads
        # remain unknown/blocked; the manager owns the separate runtime bundle.
        replacement = 'exec /bin/uname -s'
    else:
        replacement = 'exec ' + ' '.join(shlex.quote(value) for value in (
            binding['remote_python'], binding['remote_launcher'], binding['revision'], operation))
    payload = marker + PATH_PREFIX + '; ' + replacement
    rewritten = 'sh -c ' + quote_always(outer) + ' sh ' + quote_always(payload)
    return arguments[:invocation.command_index] + [rewritten], {
        'operation': operation, 'alias': binding['alias'], 'profile_id': profile_id,
        'revision': binding['revision'],
    }


def prepare_environment(root: Path | str, profile_id: str, host_bindings: list[dict],
                        environment: dict[str, str], *, app_version: str, ssh_proxy: Path | str,
                        real_ssh: Path | str | None = None, selected_model_ids: list[str] | None = None,
                        app_source_sha256: str | None = None) -> dict[str, str]:
    """Write a secret-free per-profile manifest and return a new, scoped environment.

    Caller only invokes for an inactive managed instance. Updating this manifest
    changes no original app, global PATH, SSH config, remote CLI or remote files.
    """
    from .ssh_compatibility import VERIFIED_SOURCES
    compatible = (app_source_sha256 in VERIFIED_SOURCES if app_source_sha256 is not None
                  else app_version in SUPPORTED_APP_VERSIONS)
    root = Path(root).resolve()
    profile_id = str(UUID(profile_id))
    proxy = Path(ssh_proxy).resolve(strict=True)
    if proxy.name.casefold() != 'ssh.exe' or not proxy.is_file():
        raise ShimError('ssh_proxy_missing', 'The managed SSH executable is unavailable.')
    directory = root / 'work/control-center/profiles' / profile_id
    path = directory / 'ssh-bindings.json'
    generation = environment.get('CODEX_MANAGER_GENERATION')
    if generation and path.exists():
        if path.is_symlink() or path.resolve() != path or path.stat().st_size > 256000:
            raise ShimError('manifest_invalid', 'The existing SSH binding manifest is invalid.')
        existing = json.loads(path.read_text(encoding='utf-8'))
        if existing.get('generation') == str(UUID(generation)):
            if (existing.get('profile_id') != profile_id or existing.get('schema') != 1
                    or existing.get('adapter') != ADAPTER_VERSION):
                raise ShimError('manifest_invalid', 'The running generation binding is invalid.')
            # Opening a task also asks for an environment. It must not apply a
            # newly saved policy, overwrite the live binding, or reset inventory.
            return _scoped_environment(root, profile_id, existing, path, proxy, environment)
    # Windows environment names are case-insensitive. Emitting both Path and PATH
    # can make Electron/OpenSSH choose the old value despite successful injection.
    original_path = environment.get('Path', environment.get('PATH', next(
        (value for key, value in environment.items() if key.casefold() == 'path'), '')))
    executable = str(real_ssh or shutil.which('ssh.exe', path=original_path)
                     or shutil.which('ssh', path=original_path) or '')
    candidate = Path(executable)
    if not executable or not candidate.is_absolute() or not candidate.is_file() or candidate.resolve() == proxy:
        raise ShimError('real_ssh_missing', 'An absolute original OpenSSH executable is required.')
    eligible = [item for item in host_bindings if item.get('profile_id') == profile_id and item.get('prepared') is True]
    validated = [(item, validate_binding(item, profile_id)) for item in eligible]
    if len({item['alias'] for _,item in validated}) != len(validated):
        raise ShimError('duplicate_binding', 'An SSH host has duplicate profile bindings.')
    selected = None if selected_model_ids is None else {str(UUID(value)) for value in selected_model_ids}
    bindings, pending = [], []
    model_options = json.loads(environment.get('CODEX_MANAGER_MODEL_OPTIONS', '{}'))
    for original, item in validated:
        models = original.get('model_ids')
        if (original.get('model_options', {}) != model_options or
                (selected is not None and (not isinstance(models,list) or any(not isinstance(m,str) for m in models) or set(models)!=selected))):
            pending.append(item['alias'])
        else:
            bindings.append(item)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {'schema': 1, 'adapter': ADAPTER_VERSION, 'app_version': app_version,
                'compatibility': 'installed-native-fixtures-verified' if compatible else 'validate-each-native-command',
                'native_command_validation': 1,
                'native_compatible': compatible, 'app_source_sha256': app_source_sha256, 'profile_id': profile_id,
                'real_ssh': str(candidate.resolve()), 'original_path': original_path,
                'native_cli': 'codex', 'bindings': bindings, 'pending_policy_hosts': pending}
    # Only saved, concrete SSH aliases may request automatic profile setup.
    # This is metadata only; no remote connection happens on the UI launch path.
    try:
        state = json.loads((directory / 'codex/.codex-global-state.json').read_text(encoding='utf-8'))
        aliases = {entry.get('alias') for entry in state.get('codex-managed-remote-connections', [])
                   if isinstance(entry, dict) and isinstance(entry.get('alias'), str)
                   and ALIAS.fullmatch(entry['alias'])}
    except (OSError, ValueError, TypeError):
        aliases = set()
    manifest['auto_prepare_aliases'] = sorted(aliases)
    manifest['selected_model_ids'] = sorted(selected or [])
    manifest['model_options'] = model_options
    if environment.get('CODEX_MANAGER_PRIMARY_MODEL'):
        manifest['primary_model_id'] = json.loads(environment['CODEX_MANAGER_PRIMARY_MODEL'])['model_id']
    if environment.get('CODEX_MANAGER_GENERATION'):
        from manager_core.ssh_inventory import SshInventory
        generation = str(UUID(environment['CODEX_MANAGER_GENERATION']))
        SshInventory(root).prepare(profile_id, generation)
        manifest.update(generation=generation, inventory_root=str(root))
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, path)
    return _scoped_environment(root, profile_id, manifest, path, proxy, environment)


def _scoped_environment(root, profile_id, manifest, path, proxy, environment):
    original_path = manifest['original_path']
    result = {key: value for key, value in environment.items() if key.casefold() != 'path'}
    result['PATH'] = str(proxy.parent) + os.pathsep + original_path
    result['CODEX_MANAGER_SSH_BINDINGS'] = str(path)
    result['CODEX_MANAGER_SSH_SCRIPT'] = str(root / 'scripts/manager_core/ssh_shim.py')
    result['CODEX_MANAGER_SSH_PYTHON'] = sys.executable
    result['CODEX_MANAGER_SSH_ORIGINAL_PATH'] = original_path
    result['CODEX_MANAGER_SSH_APP_VERSION'] = manifest['app_version']
    result['CODEX_MANAGER_SSH_ADAPTER_VERSION'] = ADAPTER_VERSION
    result['CODEX_MANAGER_PROFILE_ID'] = profile_id
    return result


def _load_manifest(environment: dict[str, str]) -> tuple[dict, Path]:
    value = environment.get('CODEX_MANAGER_SSH_BINDINGS', '')
    path = Path(value)
    if not value or not path.is_absolute() or path.stat().st_size > 256_000:
        raise ShimError('manifest_missing', 'The managed SSH binding manifest is unavailable.')
    manifest = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(manifest, dict) or manifest.get('schema') != 1:
        raise ShimError('manifest_invalid', 'The managed SSH binding manifest is invalid.')
    profile_id = str(UUID(manifest.get('profile_id', '')))
    if path.name != 'ssh-bindings.json' or path.parent.name != profile_id:
        raise ShimError('manifest_invalid', 'The managed SSH manifest does not match the profile.')
    selected = environment.get('CODEX_MANAGER_PROFILE_ID')
    if selected is not None and selected != profile_id:
        raise ShimError('binding_profile_mismatch', 'The SSH binding belongs to another profile.')
    return manifest, path


def _audit(path: Path, event: dict) -> None:
    try:
        allowed = {key: event[key] for key in ('operation', 'alias', 'profile_id', 'revision', 'code', 'stage', 'exit_code') if key in event}
        allowed.update(at=datetime.now(timezone.utc).isoformat(), proxy_pid=os.getpid())
        with path.with_name('ssh-routing.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(allowed, ensure_ascii=True) + '\n')
    except OSError:
        pass


class MarkerGate:
    """Find the native binary marker without decoding or repeating shell output.

    Prefix bytes are forwarded immediately because the GUI performs the same
    filtering. Only the seven-byte search overlap is retained between chunks.
    """

    def __init__(self, marker: bytes, limit: int = 1024 * 1024):
        if len(marker) != 8:
            raise ValueError('Expected the native eight-byte marker.')
        self.marker, self.limit = marker, limit
        self.found = False
        self._tail = b''
        self._seen = 0

    def feed(self, data: bytes) -> tuple[bytes, bytes]:
        if self.found:
            return b'', data
        combined = self._tail + data
        index = combined.find(self.marker)
        if index >= 0:
            end = index + len(self.marker) - len(self._tail)
            self.found = True
            self._tail = b''
            return data[:end], data[end:]
        self._seen += len(data)
        if self._seen > self.limit:
            raise ShimError('native_marker_missing', 'The SSH synchronization marker was not received.')
        self._tail = combined[-7:]
        return data, b''


class OutputWriter:
    """Ordered byte writes without holding the bridge's bidirectional state lock."""

    def __init__(self, stream, on_failure, *, close_stream=False, max_bytes=64 * 1024 * 1024):
        self.stream, self.on_failure = stream, on_failure
        self.close_stream, self.max_bytes = close_stream, max_bytes
        self._condition = threading.Condition()
        self._items = deque()
        self._bytes = 0
        self._finished = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def put(self, data: bytes):
        if not data:
            return
        with self._condition:
            if self._finished:
                raise ShimError('ssh_output_closed', 'The SSH output stream is closed.')
            if self._bytes + len(data) > self.max_bytes:
                raise ShimError('ssh_output_limit', 'The SSH output buffer limit was exceeded.')
            self._items.append(data)
            self._bytes += len(data)
            self._condition.notify()

    def finish(self):
        with self._condition:
            self._finished = True
            self._condition.notify()

    def _run(self):
        try:
            while True:
                with self._condition:
                    while not self._items and not self._finished:
                        self._condition.wait()
                    if not self._items:
                        break
                    data = self._items.popleft()
                # Daemon transport writers can still be blocked when a rejected
                # GUI connection keeps stdio open. Raw descriptors avoid holding
                # Python buffered-stdio locks during interpreter finalization.
                remaining = memoryview(data)
                while remaining:
                    count = os.write(self.stream.fileno(), remaining)
                    if count <= 0:
                        raise OSError('The SSH output stream closed.')
                    remaining = remaining[count:]
                with self._condition:
                    self._bytes -= len(data)
        except (OSError, ValueError):
            self.on_failure()
        finally:
            if self.close_stream:
                try:
                    self.stream.close()
                except OSError:
                    pass


def _proxy_with_auth(executable: Path, arguments: list[str], environment: dict,
                     source_environment: dict, path: Path, event: dict, marker: bytes) -> int:
    """Authenticate the native WebSocket connection using Windows-held access tokens.

    OpenSSH remains the only network client. Token refresh re-reads the selected
    account source locally; no credential files or refresh tokens are uploaded.
    """
    try:
        from .proxy_auth import AuthProxy
        from .websocket_auth import WebSocketAuthBridge, WebSocketProtocolError
    except ImportError:
        try:
            from proxy_auth import AuthProxy
            from websocket_auth import WebSocketAuthBridge, WebSocketProtocolError
        except ImportError:
            raise ShimError('ssh_auth_bridge_missing', 'The SSH account binding component is unavailable.') from None
    auth = AuthProxy(source_home=source_environment.get('CODEX_MANAGER_SSH_AUTH_SOURCE') or source_environment.get('CODEX_MANAGER_AUTH_SOURCE'),
                     expected_account_fingerprint=source_environment.get('CODEX_MANAGER_EXPECTED_ACCOUNT_FINGERPRINT'))
    from manager_core.external_profile import ExternalProfile
    external = ExternalProfile(source_environment)
    external.bind_auth(auth)
    if not auth.bound and not external.binding:
        raise ShimError('ssh_account_binding_required', 'The selected SSH account binding is unavailable.')
    gate = MarkerGate(marker)
    if os.name == 'nt':
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    child = subprocess.Popen([str(executable), *arguments], env=environment,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None)
    stop = threading.Event()
    frontend_done = threading.Event()
    protocol_lock = threading.RLock()
    failure = threading.Event()
    auth_state = [auth.state]
    control = None
    admin_server = None

    def emit(outcome):
        for data in outcome.runtime:
            runtime_writer.put(data)
        for data in outcome.frontend:
            frontend_writer.put(data)
        if auth.state != auth_state[0]:
            auth_state[0] = auth.state
            _audit(path, {**event, 'operation': 'auth-state', 'code': auth.state})

    def fail_transport():
        failure.set()
        stop.set()
        try:
            child.terminate()
        except OSError:
            pass

    runtime_writer = OutputWriter(child.stdin, fail_transport, close_stream=True)
    frontend_writer = OutputWriter(sys.stdout.buffer, fail_transport)
    generation = source_environment.get('CODEX_MANAGER_GENERATION')
    if generation and source_environment.get('CODEX_MANAGER_ROOT'):
        from manager_core.ssh_runtime_control import SshRuntimeControl, endpoint_id
        from manager_core.runtime_admin import AdminServer, AdminError
        from manager_core.ssh_record_delete import delete_remote
        from functools import partial
        record_delete=partial(delete_remote, Path(source_environment['CODEX_MANAGER_ROOT']),
            event['profile_id'],generation,event['alias'],event['revision'])
        control = SshRuntimeControl(auth, event['profile_id'], generation, child.pid,
                                   runtime_writer.put, lock=protocol_lock,
                                   host_alias=event['alias'], revision=event['revision'], record_delete=record_delete)
        try:
            admin_server = AdminServer(source_environment['CODEX_MANAGER_ROOT'],
                endpoint_id(event['profile_id'], event['alias']), generation, child.pid, control.dispatch).start()
            _audit(path, {**event, 'operation': 'admin-ready'})
        except (AdminError, OSError, ValueError):
            _audit(path, {**event, 'operation': 'admin-unavailable'})
    bridge = WebSocketAuthBridge(control or auth)

    def incoming():
        try:
            while not stop.is_set():
                # This daemon may remain blocked after runtime protocol rejection.
                # Never hold sys.stdin's buffered-reader lock across that shutdown.
                data = os.read(sys.stdin.fileno(), 65536)
                if not data:
                    with protocol_lock:
                        bridge.eof('frontend')
                    break
                with protocol_lock:
                    emit(bridge.feed('frontend', data))
        except (OSError, ValueError, TypeError, ShimError, WebSocketProtocolError):
            fail_transport()
        finally:
            frontend_done.set()
            runtime_writer.finish()

    def heartbeat():
        while not stop.wait(0.2):
            if frontend_done.is_set():
                return
            try:
                with protocol_lock:
                    emit(bridge.poll())
            except (OSError, ValueError, TypeError, ShimError, WebSocketProtocolError):
                fail_transport()
                return

    # A native bootstrap Job Object ensures forced Windows termination closes the
    # local transport tree. Ctrl+C already reaches the inherited console group.
    previous_handler = signal.signal(signal.SIGINT, lambda *_: None)
    incoming_thread = threading.Thread(target=incoming, daemon=True)
    heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
    incoming_thread.start()
    heartbeat_thread.start()
    try:
        while not stop.is_set():
            data = child.stdout.read1(65536)
            if not data:
                if not gate.found:
                    raise ShimError('native_marker_missing', 'The SSH synchronization marker was not received.')
                with protocol_lock:
                    bridge.eof('runtime')
                break
            prefix, protocol = gate.feed(data)
            frontend_writer.put(prefix)
            if protocol:
                with protocol_lock:
                    emit(bridge.feed('runtime', protocol))
    except (OSError, ValueError, TypeError, ShimError, WebSocketProtocolError):
        fail_transport()
    finally:
        stop.set()
        if control is not None:
            control.close()
        if admin_server is not None:
            admin_server.close()
        signal.signal(signal.SIGINT, previous_handler)
        child.stdout.close()
        runtime_writer.finish()
        frontend_writer.finish()
    if failure.is_set():
        _audit(path, {**event, 'operation': 'blocked', 'code': 'ssh_auth_transport_failed'})
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        return 125
    code = child.wait()
    # On a normal half-close, deliver all queued response bytes before this
    # process exits. Forced GUI disposal is handled by the bootstrap job object.
    frontend_writer.thread.join()
    return code


def main(arguments: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if arguments is None else arguments)
    if args[:1] == ['--']:
        args.pop(0)
    path = None
    stage = 'load_manifest'
    try:
        manifest, path = _load_manifest(os.environ)
        executable = Path(manifest.get('real_ssh', ''))
        if not executable.is_absolute() or not executable.is_file():
            raise ShimError('real_ssh_missing', 'The original OpenSSH executable is unavailable.')
        try:
            rewritten, event = route_arguments(args, manifest)
        except ShimError as error:
            if error.code != 'host_binding_required':
                raise
            from manager_core.ssh_auto_prepare import ensure_binding
            stage = 'prepare_host'
            manifest = ensure_binding(args, manifest, path)
            rewritten, event = route_arguments(args, manifest)
        _audit(path, event)
        admission = nullcontext()
        if manifest.get('generation') and not parse_invocation(args).configuration_only:
            from manager_core.ssh_inventory import SshInventory
            admission = SshInventory(manifest['inventory_root']).execution(
                manifest['profile_id'], manifest['generation'], event)
        with admission:
            stage = 'execute'
            return _execute(executable, rewritten, args, manifest, path, event)
    except (RuntimeError, ValueError, KeyError, TypeError, OSError) as error:
        from manager_core.remote import RemoteError
        known = isinstance(error, (ShimError,RemoteError))
        code = error.code if known else 'shim_start_failed'
        if path is not None:
            invocation=parse_invocation(args)
            alias=invocation.destination if isinstance(invocation.destination,str) and ALIAS.fullmatch(invocation.destination) else None
            _audit(path, {'operation': 'blocked', 'code': code, 'alias': alias, 'stage': stage})
        message = str(error) if known else 'The managed SSH adapter could not start.'
        sys.stderr.write('Codex Control Center: ' + message + '\n')
        return 125


def _execute(executable, rewritten, args, manifest, path, event):
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith(('CODEX_MANAGER_', 'CODEX_RECORD_'))
                   and key.upper() != 'CODEX_SQLITE_HOME' and key.casefold() != 'path'}
    environment['PATH'] = manifest['original_path']
    if event['operation'] == 'native-proxy':
        source = os.environ.get('CODEX_MANAGER_SSH_AUTH_SOURCE') or os.environ.get('CODEX_MANAGER_AUTH_SOURCE')
        if not source and os.environ.get('CODEX_MANAGER_EXPECTED_ACCOUNT_FINGERPRINT'):
            raise ShimError('ssh_account_binding_required', 'The selected SSH account binding is unavailable.')
        if source or os.environ.get('CODEX_MANAGER_PRIMARY_MODEL'):
            _, prefix, _ = decode_native(args[-1], manifest.get('native_cli', 'codex'))
            octal = NATIVE_MARKER.match(prefix).group(1)
            marker = bytes(int(octal[index + 1:index + 4], 8) for index in range(0, len(octal), 4))
            code = _proxy_with_auth(executable, rewritten, environment, dict(os.environ), path, event, marker)
            _audit(path, {**event, 'exit_code': code})
            return code
    if os.name != 'nt' and not manifest.get('generation'):
        os.execvpe(str(executable), [str(executable), *rewritten], environment)
    # Native ssh.exe bootstrap owns a kill-on-close job containing this process
    # and real OpenSSH. Inherit byte streams directly; do not add pipe readers.
    process = subprocess.Popen([str(executable), *rewritten], env=environment)
    try:
        code = process.wait()
    except KeyboardInterrupt:
        # The inherited console also delivered Ctrl+C to OpenSSH. Wait for its
        # own exit rather than creating a second signal or breaking half-close.
        code = process.wait()
    _audit(path, {**event, 'exit_code': code})
    return code


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    raise SystemExit(main())
