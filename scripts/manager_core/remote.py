"""Explicit, scoped SSH preparation; never replaces a host's installed Codex.

Discovery reads Host aliases only. Inspection does not write remotely. Deployment
requires a verified Linux bundle and a concrete configured alias. A successful
upload is deliberately not reported as native-desktop SSH integration success.
"""
from __future__ import annotations

from .release_code import script_path
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import struct
import subprocess
import tarfile
import tempfile
import time
import uuid


ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
# Upload budget: at least this throughput over SSH, within these bounds.
UPLOAD_MIN_RATE = 256 * 1024
UPLOAD_TIMEOUT = (600, 3600)
# Another profile's upload to the same host may hold the host lock this long.
UPLOAD_LOCK_WAIT = 1800
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
ARCHES = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}
MARKER = "CODEX_MANAGER_INSPECT_V1"
REMOTE_CAPABILITY_MARKERS = {
    # This is read by Config::initialize_execution_presets in production.
    # Rust symbol names such as managed_execution_presets vanish when stripped.
    'execution_presets_present': b'CODEX_MANAGER_EXECUTION_PRESETS',
    'execution_preset_auth_present': b'account/executionPresetAuthTokens/read',
    'claude_account_auth_present': b'CODEX_MANAGER_CLAUDE_AUTH',
}
# Optional: a runtime that reports a lent Claude credential's source (initialize capability).
CLAUDE_CREDENTIAL_SOURCES_MARKER = b'executionPresetCredentialSources'
_CAPABILITY_CACHE = {}


def supports_remote_claude(root):
    """Only verified local Linux artifacts can opt a profile into SSH Claude."""
    root = Path(root).resolve()
    for manifest_path in (root / 'artifacts/remote').glob('*/manifest.json'):
        try:
            raw = manifest_path.read_bytes()
            value = json.loads(raw)
            if not all(value.get(flag) is True for flag in REMOTE_CAPABILITY_MARKERS):
                continue
            directory = manifest_path.parent
            if any(path.is_symlink() for path in (directory, *directory.parents)):
                continue
            identities = []
            for entry in value['files']:
                path = directory.joinpath(*_safe_relative(entry['path']).parts)
                stat = path.stat()
                identities.append((str(path), stat.st_size, stat.st_mtime_ns, stat.st_ino, path.is_symlink()))
            key = (str(manifest_path), hashlib.sha256(raw).hexdigest(), tuple(identities))
            if key not in _CAPABILITY_CACHE:
                artifact = RemoteManager(root)._artifact(value['platform'], value['architecture'], directory=directory)
                if len(_CAPABILITY_CACHE) >= 128:
                    _CAPABILITY_CACHE.clear()
                _CAPABILITY_CACHE[key] = artifact.get('claude_account_auth_present') is True
            if _CAPABILITY_CACHE[key]:
                return True
        except (OSError, ValueError, KeyError, TypeError, RemoteError):
            continue
    return False


INSPECT_SCRIPT = r'''set -u
emit() { printf 'CODEX_MANAGER_INSPECT_V1\t%s\t%s\n' "$1" "$2"; }
emit os "$(uname -s 2>/dev/null || printf unknown)"
emit arch "$(uname -m 2>/dev/null || printf unknown)"
emit home "$HOME"
emit uid "$(id -u 2>/dev/null || printf unknown)"
emit machine "$(hostname 2>/dev/null || printf unknown)"
cli=$(command -v codex 2>/dev/null || true)
emit cli "$cli"
if [ -n "$cli" ]; then
  version=$("$cli" --version 2>/dev/null | head -n 1)
  emit cli_version "$version"
  if "$cli" login status >/dev/null 2>&1; then emit stock_login true; else emit stock_login false; fi
fi
python=$(command -v python3 2>/dev/null || true)
# pyenv's default may be older than another already installed interpreter.
# Resolve sys.executable so future launches do not follow a changed shim.
for candidate in python3 python3.14 python3.13 python3.12 python3.11; do
  found=$(command -v "$candidate" 2>/dev/null || true)
  if [ -n "$found" ]; then
    supported=$("$found" -c 'import sys; assert sys.version_info >= (3,11); import tomllib; print(sys.executable)' 2>/dev/null || true)
    if [ -n "$supported" ]; then python="$supported"; break; fi
  fi
done
emit python "$python"
if [ -n "$python" ]; then
  emit python_version "$("$python" --version 2>/dev/null)"
  emit host_identity "$("$python" -c 'import hashlib,os,pathlib; p=pathlib.Path("/etc/machine-id"); identity=p.read_text().strip(); print(hashlib.sha256((identity+"\\0"+str(os.getuid())+"\\0"+str(pathlib.Path.home())).encode()).hexdigest())' 2>/dev/null || true)"
fi
claude="$HOME/.local/share/codex-control-center/tools/claude/claude"
if [ ! -x "$claude" ]; then claude=$(command -v claude 2>/dev/null || true); fi
if [ -n "$claude" ] && [ -n "$python" ]; then
  claude=$("$python" -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve(strict=True))' "$claude" 2>/dev/null || true)
fi
emit claude_path "$claude"
if [ -n "$claude" ]; then emit claude_version "$("$claude" --version 2>/dev/null | head -n 1)"; fi
'''


def login_shell_command(command: str) -> str:
    """Match the native application's login-shell convention for PATH discovery."""
    inner = 'exec /bin/sh -c "$CODEX_REMOTE_PAYLOAD"'
    csh = 'set loginsh=1; if ( -r /etc/csh.login ) source /etc/csh.login; if ( -r ~/.login ) source ~/.login; ' + inner
    wrapper = (
        'if [ -z "$SHELL" ] || [ ! -x "$SHELL" ]; then exit 127; fi; '
        'CODEX_REMOTE_PAYLOAD="$1"; export CODEX_REMOTE_PAYLOAD; '
        'case "${SHELL##*/}" in '
        'csh|tcsh) exec "$SHELL" -i -c ' + shlex.quote(csh) + ' ;; '
        'nu) exec "$SHELL" -l -i -c ' + shlex.quote('exec /bin/sh -c $env.CODEX_REMOTE_PAYLOAD') + ' ;; '
        '*) exec "$SHELL" -l -i -c ' + shlex.quote(inner) + ' ;; esac'
    )
    return "sh -c " + shlex.quote(wrapper) + " sh " + shlex.quote(command)


INSPECT_COMMAND = login_shell_command("exec /bin/sh -s")


class RemoteError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _safe_relative(value: str) -> PurePosixPath:
    if not isinstance(value, str) or "\\" in value or "\x00" in value:
        raise RemoteError("invalid_artifact", "원격 묶음의 파일 경로가 올바르지 않습니다.")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(p in (".", "..") for p in path.parts) or ":" in value:
        raise RemoteError("invalid_artifact", "원격 묶음에 허용되지 않은 경로가 있습니다.")
    return path


def _parse_aliases(config: Path) -> list[str]:
    """Do not execute Match exec, ssh -G, or read IdentityFile targets."""
    aliases: set[str] = set()
    visited: set[Path] = set()
    ssh_directory = config.parent.resolve()

    def read(path: Path, depth: int = 0) -> None:
        if depth > 12 or len(visited) >= 128:
            return
        try:
            resolved = path.resolve(strict=True)
            if resolved in visited or not resolved.is_file() or resolved.stat().st_size > 2_000_000:
                return
            visited.add(resolved)
            lines = resolved.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                # SSH configuration uses whitespace or one '=' after the key.
                tokens = shlex.split(re.sub(r"^\s*(Host|Include)\s*=", r"\1 ", line, flags=re.I), comments=True)
            except ValueError:
                continue
            if not tokens:
                continue
            keyword = tokens[0].lower()
            if keyword == "host":
                aliases.update(name for name in tokens[1:] if ALIAS.fullmatch(name))
            elif keyword == "include":
                for pattern in tokens[1:]:
                    # Include expansion is file reading only; no env/command expansion.
                    candidate = Path(pattern).expanduser()
                    if not candidate.is_absolute():
                        candidate = ssh_directory / candidate
                    if "%" in pattern or "$" in pattern:
                        continue
                    import glob
                    for match in sorted(glob.glob(str(candidate)))[:128]:
                        read(Path(match), depth + 1)
    read(config)
    return sorted(aliases, key=str.casefold)


def _hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _verify_elf(path: Path, arch: str) -> None:
    with path.open("rb") as stream:
        header = stream.read(64)
    expected = {"x86_64": 62, "aarch64": 183}[arch]
    if len(header) < 20 or header[:6] != b"\x7fELF\x02\x01" or struct.unpack("<H", header[18:20])[0] != expected:
        raise RemoteError("artifact_architecture_mismatch", "원격 CPU에 맞는 Linux 실행 파일이 아닙니다. Windows 실행 파일은 전송하지 않습니다.")


class RemoteManager:
    def __init__(self, root: Path | str, *, ssh_config: Path | None = None,
                 runner=None, ssh_executable: str | None = None, registry=None):
        self.root = Path(root).resolve()
        self.ssh_config = ssh_config or Path.home() / ".ssh/config"
        self._runner = runner or subprocess.run
        self.ssh = ssh_executable or shutil.which("ssh.exe") or shutil.which("ssh")
        self._registry = registry

    def list_hosts(self) -> list[dict]:
        return [{"id": "ssh:" + alias, "alias": alias, "status": "not_inspected",
                 "message": "원격 연결을 검사한 뒤 준비할 수 있습니다.", "native_gui_verified": False}
                for alias in _parse_aliases(self.ssh_config)]

    def _alias(self, alias: str) -> str:
        if not isinstance(alias, str) or not ALIAS.fullmatch(alias):
            raise RemoteError("invalid_ssh_alias", "SSH 설정에 등록된 정확한 호스트 별칭을 선택하세요.")
        if alias not in _parse_aliases(self.ssh_config):
            raise RemoteError("unknown_ssh_alias", "SSH 설정에서 이 호스트 별칭을 찾지 못했습니다.")
        return alias

    def _command(self, alias: str, command: str) -> list[str]:
        if not self.ssh:
            raise RemoteError("ssh_missing", "Windows OpenSSH 클라이언트를 찾지 못했습니다.")
        return [self.ssh, "-F", str(self.ssh_config), "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                "-o", "ConnectTimeout=10", "-o", "ConnectionAttempts=1", "-o",
                "ClearAllForwardings=yes", alias, command]

    def _run(self, alias: str, command: str, *, input: bytes | None = None,
             stdin=None, timeout: int = 30, stdout=subprocess.PIPE):
        kwargs = dict(stdout=stdout, stderr=subprocess.PIPE, timeout=timeout,
                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if stdin is not None:
            kwargs["stdin"] = stdin
        else:
            kwargs["input"] = input
        try:
            return self._runner(self._command(alias, command), **kwargs)
        except subprocess.TimeoutExpired:
            raise RemoteError("ssh_timeout", "SSH 작업 시간이 초과되었습니다. 원격 적용 상태를 다시 검사하세요.") from None
        except OSError:
            raise RemoteError("ssh_failed", "SSH 클라이언트를 실행하지 못했습니다.") from None

    def inspect(self, alias: str) -> dict:
        alias = self._alias(alias)
        response = self._run(alias, INSPECT_COMMAND, input=INSPECT_SCRIPT.encode())
        result = {"id": "ssh:" + alias, "alias": alias, "status": "connection_failed",
                  "native_gui_verified": False, "remote_writes": False, "blockers": []}
        if response.returncode != 0:
            result.update(message="SSH 연결 또는 읽기 전용 검사가 실패했습니다. 호스트 키와 비대화형 인증을 확인하세요.",
                          blockers=["ssh_connection_failed"])
            return result
        observed = {}
        for line in response.stdout.decode("utf-8", "replace").splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3 and parts[0] == MARKER and parts[1] in {
                "os", "arch", "home", "uid", "machine", "cli", "cli_version", "stock_login", "python", "python_version", "host_identity", "claude_path", "claude_version"
            }:
                if parts[1] in observed or len(parts[2]) > 4096 or "\x00" in parts[2]:
                    raise RemoteError("invalid_remote_response", "원격 검사 응답을 안전하게 해석할 수 없습니다.")
                observed[parts[1]] = parts[2]
        required = ("os", "arch", "home", "uid", "machine", "cli", "python")
        if any(key not in observed for key in required):
            result.update(message="원격 셸 검사 응답이 불완전합니다.", blockers=["remote_probe_incomplete"])
            return result
        arch = ARCHES.get(observed["arch"].lower())
        platform = observed["os"].lower()
        result.update(status="inspected", platform=platform, architecture=arch or observed["arch"],
                      cli_path=observed["cli"], cli_version=observed.get("cli_version"),
                      stock_cli_authenticated=observed.get("stock_login") == "true",
                      remote_home=observed["home"], remote_uid=observed["uid"], remote_machine=observed["machine"],
                      host_identity=observed.get("host_identity"),
                      python_path=observed["python"], python_version=observed.get("python_version"),
                      message="읽기 전용 검사 완료. 관리 프로필의 인증과 앱 연결은 별도로 확인합니다.")
        if observed.get('claude_path') and observed.get('claude_version'):
            match = re.fullmatch(r'(\d+\.\d+\.\d+)(?: \(Claude Code\))?', observed['claude_version'])
            if match:
                result['claude_cli'] = dict(path=observed['claude_path'], version=match.group(1))
        if platform != "linux" or not arch:
            result["blockers"].append("platform_not_supported")
        if not observed["python"] or not observed["python"].startswith("/"):
            result["blockers"].append("python3_missing")
        else:
            python_version = re.fullmatch(r"Python (\d+)\.(\d+)(?:\.\d+)?(?:[A-Za-z0-9.+-]*)", observed.get("python_version", ""))
            if not python_version or tuple(map(int, python_version.group(1, 2))) < (3, 11):
                result["blockers"].append("python311_required")
        if not observed["home"].startswith("/") or "\n" in observed["home"]:
            result["blockers"].append("invalid_remote_home")
        if not SHA256.fullmatch(observed.get("host_identity", "")):
            result["blockers"].append("host_identity_unavailable")
        if not result["blockers"]:
            try:
                artifact = self._artifact(platform, arch)
                result["runtime_bundle"] = {"id": artifact["bundle_id"], "version": artifact["version"]}
            except RemoteError as exc:
                result["blockers"].append(exc.code)
                result["message"] = str(exc)
        result["preparation_supported"] = not result["blockers"]
        # Managed operation runs only the profile runtime's own codex, never the
        # stock CLI (terminal use, stock updates), so its absence is only recorded.
        if not observed["cli"]:
            result["blockers"].append("stock_cli_missing")
        # Upload support and native application routing are independent capabilities.
        result["blockers"].append("native_gui_ssh_binding_unverified")
        return result

    def _artifact(self, platform: str, arch: str, *, directory=None) -> dict:
        directory = Path(directory) if directory is not None else self.root / "artifacts/remote" / f"{platform}-{arch}"
        if directory.is_symlink() or not directory.resolve().is_relative_to(self.root/'artifacts/remote'):
            raise RemoteError('invalid_artifact','원격 런타임 경로가 올바르지 않습니다.')
        manifest_path = directory / "manifest.json"
        if not manifest_path.is_file():
            raise RemoteError("remote_artifact_missing", f"{platform}/{arch}용 검증된 패치 런타임 묶음이 아직 없습니다.")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            if (manifest.get("schema") != 1 or manifest.get("platform") != platform or
                    manifest.get("architecture") != arch or manifest.get("external_bridge_present") is not True or
                    not SAFE_ID.fullmatch(manifest.get("version", ""))):
                raise ValueError()
            if manifest.get('mixed_source_catalog_present') is True and not (
                    manifest.get('managed_sources_present') is True and manifest.get('source_catalog_present') is True):
                raise ValueError()
            files = manifest["files"]
            if not isinstance(files, list) or not 2 <= len(files) <= 64:
                raise ValueError()
            seen = set()
            for item in files:
                relative = _safe_relative(item["path"])
                if str(relative) in seen or not SHA256.fullmatch(item["sha256"]):
                    raise ValueError()
                seen.add(str(relative))
                path = directory.joinpath(*relative.parts)
                if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()) or not path.is_file():
                    raise ValueError()
                if _hash(path) != item["sha256"]:
                    raise RemoteError("artifact_hash_mismatch", "원격 런타임 파일의 해시가 빌드 기록과 다릅니다.")
            if not {"codex", "codex-code-mode-host", "bwrap"} <= seen:
                raise ValueError()
            for executable in ("codex", "codex-code-mode-host", "bwrap"):
                _verify_elf(directory / executable, arch)
            from remote_helpers.package_runtime import contains_marker
            for flag, marker in REMOTE_CAPABILITY_MARKERS.items():
                if manifest.get(flag) is True and not contains_marker(directory / 'codex', marker):
                    raise ValueError()
            bwrap_digest = next(item["sha256"] for item in files if item["path"] == "bwrap")
            if manifest.get("bwrap_sha256") != bwrap_digest:
                raise ValueError()
            digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:16]
            return {**manifest, "directory": directory, "bundle_id": manifest["version"] + "-" + digest}
        except RemoteError:
            raise
        except (KeyError, TypeError, ValueError, OSError):
            raise RemoteError("invalid_artifact", "원격 런타임 빌드 기록이 불완전하거나 유효하지 않습니다.") from None

    def _registry_instance(self):
        if self._registry is None:
            from .providers import ProviderRegistry
            self._registry = ProviderRegistry(self.root)
        return self._registry

    def _definition_revision(self, files, bundle, host_identity, *, legacy, helper_files=None):
        """Only remote execution inputs belong in the SSH definition identity."""
        values = {"files": files, "runtime": bundle, "host_identity": host_identity}
        for key, name in (('installer', 'install'), ('launcher', 'launch'),
                          ('native_controller', 'native_controller'), ('ws_client', 'ws_client'),
                          ('common', 'common'), ('managed_sources', 'managed_sources')):
            values[key] = _hash(script_path(self.root, 'scripts/remote_helpers/' + name + '.py'))
        values['catalog_legacy'] = (_hash(script_path(self.root, 'scripts/remote_helpers/catalog_legacy.py'))
                                    if legacy else None)
        if 'manager-execution-authority.json' in files:
            values['execution_presets'] = _hash(script_path(self.root, 'scripts/remote_helpers/execution_presets.py'))
        if helper_files:
            values['provider_helpers'] = {name: hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()
                                         for name, value in helper_files.items()}
        return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    @staticmethod
    def _execution_presets_supported(artifact):
        from remote_helpers.package_runtime import contains_marker
        binary = artifact['directory'] / 'codex'
        return all(contains_marker(binary, marker) for marker in REMOTE_CAPABILITY_MARKERS.values())

    def _render_presets(self, profile, rendered, *, alias, config_home, remote_python, host_identity, remote_cli=None):
        from .execution_presets import ExecutionPresets
        from .store import Store
        result = ExecutionPresets(Store(self.root), self._registry_instance()).render_for_host(
            profile['id'], rendered['files']['config.toml'], host_id='ssh:' + alias,
            config_home=config_home, remote_python=remote_python, host_identity=host_identity, remote_cli=remote_cli)
        rendered['files'].update(result['files'])
        rendered.setdefault('helper_files', {}).update(result.get('helper_files', {}))
        if rendered.get('main_auth') is not None:
            from .providers import _json
            result['authority']['main_auth'] = rendered['main_auth']
            result['manifest']['main_auth'] = rendered['main_auth']
            rendered['files']['manager-execution-authority.json'] = _json(result['authority'])
            ExecutionPresets._validate_runtime_limits(result['manifest'])
        rendered['execution_presets'] = result
        return rendered

    def _authority_path(self, profile_id, alias, revision):
        from .store import identifier
        profile_id = identifier(profile_id)
        if not ALIAS.fullmatch(alias or '') or not SHA256.fullmatch(revision or ''):
            raise RemoteError('invalid_preset_binding', 'SSH 실행 프리셋 연결 정보를 확인하세요.')
        path = self.root / 'work/control-center/remote-execution-presets' / profile_id / hashlib.sha256(alias.encode()).hexdigest()[:24] / (revision + '.json')
        if any(item.is_symlink() for item in (path, *path.parents)) or not path.resolve().is_relative_to(self.root):
            raise RemoteError('invalid_preset_binding', 'SSH 실행 프리셋 경로를 확인하세요.')
        return path

    def execution_preset_authority(self, profile, binding):
        """Manager-owned authority for the exact prepared host/account runtime."""
        from .execution_presets import ExecutionPresets, MAX_RUNTIME_BYTES
        from .ssh_shim import validate_binding
        valid = validate_binding(binding, profile['id'])
        if (binding.get('prepared') is not True or binding.get('execution_presets_version') != 1
                or not SHA256.fullmatch(binding.get('host_identity', ''))):
            raise RemoteError('runtime_prepare_required', '실행 프리셋을 지원하는 SSH 런타임 준비가 필요합니다.')
        path = self._authority_path(profile['id'], valid['alias'], valid['revision'])
        try:
            if path.stat().st_size > MAX_RUNTIME_BYTES + 65536:
                raise ValueError()
            value = json.loads(path.read_text(encoding='utf-8'))
            if (value.get('schema_version') != 1 or value.get('profile_id') != profile['id']
                    or value.get('host_id') != 'ssh:' + valid['alias'] or value.get('revision') != valid['revision']
                    or value.get('host_identity') != binding['host_identity'] or not isinstance(value.get('roles'), dict)):
                raise ValueError()
            native = {key: value[key] for key in
                      ('schema_version', 'profile_id', 'host_id', 'roles', 'environment_model_ids', 'main_auth') if key in value}
            ExecutionPresets._validate_runtime_limits(dict(native, presets=[], default_preset=None, task_bindings={}))
            return value
        except (OSError, ValueError, KeyError, TypeError):
            raise RemoteError('runtime_prepare_required', '준비된 SSH 프리셋 권한 정보를 확인하세요.') from None

    def publish_execution_presets(self, profile, binding):
        from .execution_presets import ExecutionPresets
        from .ssh_shim import validate_binding
        from .store import Store
        valid = validate_binding(binding, profile['id'])
        try:
            authority = self.execution_preset_authority(profile, binding)
        except RemoteError as error:
            if error.code == 'runtime_prepare_required':
                return dict(runtime_prepare_required=True, execution_presets_version=0)
            raise
        result = ExecutionPresets(Store(self.root), self._registry_instance()).manifest_for_prepared_host(
            profile['id'], authority, config_home=str(PurePosixPath(valid['remote_launcher']).parent / 'codex'),
            remote_python=valid['remote_python'], host_identity=binding['host_identity'], remote_cli=binding.get('claude_cli'))
        selection = {key: result['manifest'][key] for key in ('presets', 'default_preset', 'task_bindings')}
        request = dict(schema_version=1, profile_id=profile['id'], host_id='ssh:' + valid['alias'],
                       host_identity=binding['host_identity'], revision=valid['revision'],
                       source_revision=result['source_revision'], selection=selection)
        command = (shlex.quote(valid['remote_python']) + ' ' + shlex.quote(valid['remote_launcher'])
                   + ' ' + valid['revision'] + ' --publish-execution-presets')
        response = self._json_result(self._run(self._alias(valid['alias']), command,
            input=json.dumps(request, ensure_ascii=False, allow_nan=False).encode('utf-8')), 'remote_presets_failed')
        if (response.get('status') != 'published' or response.get('profile_id') != profile['id']
                or response.get('host_id') != request['host_id'] or response.get('revision') != valid['revision']
                or response.get('source_revision') != result['source_revision']):
            raise RemoteError('remote_presets_failed', 'SSH 실행 프리셋 적용 응답을 확인하지 못했습니다.')
        return {key: value for key, value in result.items() if key != 'manifest'} | dict(execution_presets_version=1, published=True)

    def binding_matches_settings(self, profile, binding):
        """Local comparison only; never stop SSH because the shell bundle changed.

        A reconnect still validates the remote descriptor and exact live revision.
        Missing/old metadata must take the normal preparation path.
        """
        from .model_settings import render_options
        from .ssh_shim import validate_binding
        try:
            valid = validate_binding(binding, profile['id'])
            if binding.get('prepared') is not True or not SHA256.fullmatch(binding.get('host_identity', '')):
                return False
            if 'settings_fingerprint' in binding:
                return binding['settings_fingerprint'] == self.settings_fingerprint(profile, binding)
            artifact = None
            for arch in ('x86_64', 'aarch64'):
                path = self.root / 'artifacts/remote' / ('linux-' + arch) / 'manifest.json'
                if not path.is_file():
                    continue
                value = json.loads(path.read_text(encoding='utf-8-sig'))
                bundle = value['version'] + '-' + hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:16]
                if (value.get('schema') == 1 and value.get('platform') == 'linux'
                        and value.get('architecture') == arch and bundle == binding.get('runtime_bundle')):
                    artifact = value
                    break
            if artifact is None:
                return False
            model_ids = profile['policy']['model_ids'] if profile['policy']['enabled'] else []
            home = str(PurePosixPath(valid['remote_launcher']).parent / 'codex')
            rendered = self._registry_instance().render_for_host(home, bool(model_ids), model_ids,
                existing_config='[features]\ncode_mode_host = true\n', **render_options(profile))
            return valid['revision'] == self._definition_revision(rendered['files'], binding['runtime_bundle'],
                binding['host_identity'], legacy=artifact.get('mixed_source_catalog_present') is True)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            return False

    def settings_files(self, profile, binding):
        """Execution config only; replacing manager helpers is an explicit update."""
        from .model_settings import render_options
        from .ssh_shim import validate_binding
        valid = validate_binding(binding, profile['id'])
        models = profile['policy']['model_ids'] if profile['policy']['enabled'] else []
        home = str(PurePosixPath(valid['remote_launcher']).parent / 'codex')
        if profile.get('auth_mode') == 'claude_code':
            from .claude_profiles import render_for_host
            rendered = render_for_host(self._registry_instance(), PurePosixPath(home), profile,
                '[features]\ncode_mode_host = true\n', host_id='ssh:' + valid['alias'],
                remote_python=valid['remote_python'], host_identity=binding['host_identity'], remote_cli=binding.get('claude_cli'))
        else:
            rendered = self._registry_instance().render_for_host(home, bool(models), models,
                existing_config='[features]\ncode_mode_host = true\n', **render_options(profile))
        if binding.get('execution_presets_version') == 1:
            rendered = self._render_presets(profile, rendered, alias=valid['alias'], config_home=home,
                remote_python=valid['remote_python'], host_identity=binding['host_identity'], remote_cli=binding.get('claude_cli'))
        return {name: hashlib.sha256(content.encode('utf-8')).hexdigest()
                for name, content in rendered['files'].items()}

    @staticmethod
    def _settings_fingerprint(binding, files):
        from .ssh_shim import validate_binding
        inputs = dict(binding=validate_binding(binding, binding['profile_id']), files=files,
                      host_identity=binding['host_identity'], runtime_bundle=binding['runtime_bundle'])
        return hashlib.sha256(json.dumps(inputs, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def settings_fingerprint(self, profile, binding):
        return self._settings_fingerprint(binding, self.settings_files(profile, binding))

    def _validate_claude_profile(self, profile_id, snapshot):
        from .claude_profiles import settings
        from .store import Store
        try:
            profile = Store(self.root).profile(profile_id)
            if (not isinstance(snapshot, dict) or set(snapshot) != {'id', 'settings'}
                    or snapshot['id'] != profile_id or profile.get('auth_mode') != 'claude_code'
                    or settings(snapshot['settings']) != settings(profile.get('claude_settings'))):
                raise ValueError()
        except (OSError, ValueError, KeyError, TypeError):
            raise RemoteError('profile_settings_changed',
                'Claude 계정 설정이 변경되었습니다. 최신 설정으로 SSH 연결을 다시 준비하세요.') from None

    def prepare(self, alias: str, profile_id: str, profile_home: Path | str,
                model_ids: list[str], *, primary_model_id=None, primary_settings=None, selection_mode='automatic', reuse_host_runtime=False,
                claude_profile=None) -> dict:
        alias = self._alias(alias)
        try:
            if str(uuid.UUID(profile_id)) != profile_id:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise RemoteError("invalid_profile", "관리 프로필 ID가 올바르지 않습니다.") from None
        if claude_profile is not None:
            self._validate_claude_profile(profile_id, claude_profile)
        # The argument is intentionally not a source of files, auth, or secrets.
        # A remote profile is generated from the registry, never copied wholesale.
        observed = self.inspect(alias)
        if not observed.get("preparation_supported"):
            return {**observed, "status": "blocked", "prepared": False}
        current = self._artifact(observed["platform"], observed["architecture"])
        reuse = reuse_host_runtime and selection_mode != 'external_only'
        artifact, reused = self._select_runtime(alias, observed, current, reuse)
        upload_lock = None
        if not reused:
            # Profiles preparing together would each stage the whole runtime on
            # the host (disk full) and share its uplink (timeouts). One uploads;
            # the others wait, preflight again and reuse the finished copy.
            upload_lock, waited = self._wait_runtime_upload(observed["host_identity"])
            try:
                if waited:
                    artifact, reused = self._select_runtime(alias, observed, current, reuse)
            except BaseException:
                self._release_runtime_upload(upload_lock)
                raise
            if reused:
                self._release_runtime_upload(upload_lock)
                upload_lock = None
        try:
            return self._install(alias, profile_id, model_ids, observed, artifact, reused,
                                 primary_model_id=primary_model_id, primary_settings=primary_settings,
                                 selection_mode=selection_mode, claude_profile=claude_profile)
        finally:
            if upload_lock is not None:
                self._release_runtime_upload(upload_lock)

    def _wait_runtime_upload(self, host_identity):
        from .updates import UpdateError, _lock_file
        if not SHA256.fullmatch(host_identity or ""):
            raise RemoteError("host_identity_unavailable", "SSH 서버 식별 정보를 확인하지 못했습니다.")
        path = self.root / "work/control-center/remote-uploads" / (host_identity[:32] + ".lock")
        deadline = time.monotonic() + UPLOAD_LOCK_WAIT
        waited = False
        while True:
            try:
                return _lock_file(path), waited
            except UpdateError:
                waited = True
                if time.monotonic() >= deadline:
                    raise RemoteError("remote_upload_busy", "다른 프로필이 같은 SSH 서버에 런타임을 올리고 있습니다. "
                                      "끝난 뒤 다시 시도하세요.") from None
                time.sleep(0.5)

    @staticmethod
    def _release_runtime_upload(lock):
        from .updates import _unlock_file
        _unlock_file(lock)

    @staticmethod
    def _upload_timeout(size):
        low, high = UPLOAD_TIMEOUT
        return max(low, min(high, 120 + size // UPLOAD_MIN_RATE))

    def _install(self, alias, profile_id, model_ids, observed, artifact, reused, *,
                 primary_model_id, primary_settings, selection_mode, claude_profile=None):
        if claude_profile is not None:
            self._validate_claude_profile(profile_id, claude_profile)
        if selection_mode == 'external_only':
            from remote_helpers.package_runtime import contains_marker
            if not contains_marker(artifact['directory'] / 'codex', b'External-only subagents:'):
                raise RemoteError('runtime_update_required', '외부 모델 전용 정책을 지원하는 SSH 런타임 업데이트가 필요합니다.')
        base = PurePosixPath(observed["remote_home"]) / ".local/share/codex-control-center"
        remote_profile = base / "profiles" / profile_id
        registry = self._registry_instance()
        from .store import Store
        profile = next((value for value in Store(self.root).read()['profiles'] if value['id'] == profile_id), None)
        preset_support = self._execution_presets_supported(artifact)
        if profile and profile.get('auth_mode') == 'claude_code':
            if not preset_support:
                raise RemoteError('runtime_update_required', 'Claude 계정을 지원하는 SSH 런타임 업데이트가 필요합니다.')
            from .claude_profiles import render_for_host
            rendered = render_for_host(registry, remote_profile / 'codex', profile,
                '[features]\ncode_mode_host = true\n', host_id='ssh:' + alias,
                remote_python=observed['python_path'], host_identity=observed['host_identity'], remote_cli=observed.get('claude_cli'))
        else:
            rendered = registry.render_for_host(str(remote_profile / "codex"), bool(model_ids), model_ids,
                                                existing_config="[features]\ncode_mode_host = true\n",
                                                primary_model_id=primary_model_id, primary_settings=primary_settings, selection_mode=selection_mode)
        if preset_support and profile:
            rendered = self._render_presets(profile, rendered, alias=alias, config_home=str(remote_profile / 'codex'),
                remote_python=observed['python_path'], host_identity=observed['host_identity'], remote_cli=observed.get('claude_cli'))
        helper = script_path(self.root, "scripts/remote_helpers/install.py")
        launcher = script_path(self.root, "scripts/remote_helpers/launch.py")
        controller = script_path(self.root, "scripts/remote_helpers/native_controller.py")
        common = script_path(self.root, "scripts/remote_helpers/common.py")
        managed = script_path(self.root, "scripts/remote_helpers/managed_sources.py")
        websocket = script_path(self.root, "scripts/remote_helpers/ws_client.py")
        if not all(path.is_file() for path in (helper, launcher, controller, common, managed, websocket)):
            raise RemoteError("remote_helper_missing", "원격 준비 도우미 파일이 없습니다.")
        launcher_bytes = launcher.read_bytes()
        controller_bytes = controller.read_bytes()
        common_bytes = common.read_bytes()
        managed_bytes = managed.read_bytes()
        websocket_bytes = websocket.read_bytes()
        legacy_bytes = None
        if artifact.get('mixed_source_catalog_present') is True:
            legacy_helper = script_path(self.root, 'scripts/remote_helpers/catalog_legacy.py')
            if not legacy_helper.is_file():
                raise RemoteError('remote_helper_missing', '기존 SSH 기록을 찾는 도우미 파일이 없습니다.')
            legacy_bytes = legacy_helper.read_bytes()
        revision = self._definition_revision(rendered['files'], artifact['bundle_id'],
            observed['host_identity'], legacy=legacy_bytes is not None, helper_files=rendered.get('helper_files'))
        entries = {}
        runtime_hashes = {}
        for item in artifact["files"]:
            entries["runtime/" + item["path"]] = (artifact["directory"] / item["path"], 0o700)
            runtime_hashes["runtime/" + item["path"]] = item["sha256"]
        for name, content in rendered["files"].items():
            relative = _safe_relative(name)
            if relative.suffix not in (".toml", ".json") or name in ("auth.json", "credentials.json"):
                raise RemoteError("invalid_remote_config", "원격 프로필 생성기에 허용되지 않은 파일이 있습니다.")
            entries["definition/" + str(relative)] = (content.encode("utf-8"), 0o600)
        entries["helpers/launch.py"] = (launcher_bytes, 0o700)
        entries["helpers/native_controller.py"] = (controller_bytes, 0o700)
        entries["helpers/ws_client.py"] = (websocket_bytes, 0o700)
        entries["helpers/common.py"] = (common_bytes, 0o700)
        entries["helpers/managed_sources.py"] = (managed_bytes, 0o700)
        if 'execution_presets' in rendered:
            entries['helpers/execution_presets.py'] = (script_path(self.root, 'scripts/remote_helpers/execution_presets.py').read_bytes(), 0o700)
        for name, content in rendered.get('helper_files', {}).items():
            relative = _safe_relative(name)
            if relative.suffix != '.py' or 'helpers/' + str(relative) in entries:
                raise RemoteError('invalid_remote_config', 'SSH 제공자 도우미 경로를 확인하세요.')
            entries['helpers/' + str(relative)] = (content.encode() if isinstance(content, str) else content, 0o700)
        if legacy_bytes is not None:
            entries['helpers/catalog_legacy.py'] = (legacy_bytes, 0o700)
        metadata = {"schema": 1, "bundle_id": artifact["bundle_id"], "profile_id": profile_id,
                    "managed_sources": artifact.get('managed_sources_present') is True,
                    "source_catalog": artifact.get('source_catalog_present') is True,
                    "mixed_source_catalog": artifact.get('mixed_source_catalog_present') is True,
                    "revision": revision, "host_identity": observed["host_identity"], "files": []}
        for name, (source, mode) in entries.items():
            digest = runtime_hashes.get(name)
            if digest is None:
                digest = _hash(source) if isinstance(source, Path) else hashlib.sha256(source).hexdigest()
            metadata["files"].append({"path": name, "sha256": digest, "mode": mode,
                                      "size": source.stat().st_size if isinstance(source,Path) else len(source)})
        metadata['reuse_runtime'] = reused
        # Large binaries spool to a temporary file; API keys never enter this archive.
        # Fast gzip roughly thirds a runtime upload; the installer reads "r|*".
        with tempfile.TemporaryFile() as archive:
            with tarfile.open(fileobj=archive, mode="w:gz", compresslevel=1) as tar:
                self._add(tar, "manifest.json", json.dumps(metadata).encode(), 0o600)
                for name, (source, mode) in entries.items():
                    if reused and name.startswith('runtime/'):continue
                    self._add(tar, name, source, mode)
            size = archive.tell()
            archive.seek(0)
            command = shlex.quote(observed["python_path"]) + " -c " + shlex.quote(helper.read_text(encoding="utf-8"))
            installed = self._run(alias, command, stdin=archive, timeout=self._upload_timeout(size))
        payload = self._json_result(installed, "remote_install_failed")
        if payload.get("status") != "prepared" or payload.get("revision") != revision:
            raise RemoteError("remote_install_failed", "원격 설치 완료를 확인하지 못했습니다. 기존 CLI는 유지됩니다.")
        # Deliver only selected provider credentials over SSH stdin after installation.
        credential_models = list(dict.fromkeys(model_ids + ([primary_model_id] if primary_model_id else [])
            + rendered.get('execution_presets', {}).get('environment_model_ids', [])))
        env = registry.environment(credential_models) if credential_models else {}
        command = (shlex.quote(observed["python_path"]) + " " +
                   shlex.quote(str(remote_profile / "launch.py")) + " " + revision + " --configure")
        credential_payload = json.dumps({"environment": env, "revision": revision}).encode()
        try:
            configured = self._run(alias, command, input=credential_payload, timeout=30)
        finally:
            env.clear()
            credential_payload = b""
        config_result = self._json_result(configured, "remote_credentials_failed")
        if config_result.get("status") != "configured":
            raise RemoteError("remote_credentials_failed", "런타임은 준비됐지만 원격 키 설정을 확인하지 못했습니다.")
        result = {"id": "ssh:" + alias, "alias": alias, "profile_id": profile_id,
                "status": "prepared_not_connected", "prepared": True, "native_gui_verified": False,
                "runtime_bundle": artifact["bundle_id"], "revision": revision,
                "runtime_reused": reused,
                "host_identity": observed["host_identity"],
                "remote_profile_home": str(remote_profile / "codex"),
                "remote_launcher": str(remote_profile / "launch.py"),
                "remote_python": observed["python_path"],
                "model_options": {**({'primary_model_id':primary_model_id,'primary_settings':primary_settings} if primary_model_id else {}),
                                  **({'selection_mode':selection_mode} if selection_mode=='external_only' else {})},
                "model_ids": list(model_ids), "authentication_required": not config_result.get("authenticated", False),
                "blockers": ["native_gui_ssh_binding_unverified", "selected_account_verification_required"],
                "message": "원격 파일 준비 완료. 원본 앱 연결과 선택한 GPT 계정 확인 전에는 사용 가능으로 표시하지 않습니다."}
        result['settings_fingerprint'] = self._settings_fingerprint(result,
            {name: hashlib.sha256(content.encode('utf-8')).hexdigest() for name, content in rendered['files'].items()})
        if profile and profile.get('auth_mode') == 'claude_code':
            from .model_settings import render_options
            result['model_options'] = render_options(profile)
        if observed.get('claude_cli'):
            result['claude_cli'] = observed['claude_cli']
        if 'execution_presets' in rendered:
            from .store import atomic_json
            authority = dict(rendered['execution_presets']['authority'], revision=revision,
                             host_identity=observed['host_identity'])
            cache = self._authority_path(profile_id, alias, revision)
            if cache.exists() and json.loads(cache.read_text(encoding='utf-8')) != authority:
                raise RemoteError('invalid_preset_binding', '기존 SSH 프리셋 권한 정보와 충돌합니다.')
            atomic_json(cache, authority)
            result['execution_presets_version'] = 1
            if self._claude_credential_sources(artifact, authority):
                # Display only (the Claude settings panel): the broker checks the runtime's
                # initialize capability and the authority's flag itself on every read.
                from .claude_long_lived_auth import SSH_SUPPORT_KEY
                result[SSH_SUPPORT_KEY] = 1
            publication = self.publish_execution_presets(profile, result)
            result['execution_presets'] = publication
        return result

    @staticmethod
    def _claude_credential_sources(artifact, authority):
        """Whether this prepared runtime and authority can carry a long-lived Claude token."""
        from remote_helpers.package_runtime import contains_marker
        accounts = [authority.get('main_auth')] + list((authority.get('roles') or {}).values())
        if not any(isinstance(item, dict) and item.get('credential_sources') == 1 for item in accounts):
            return False
        try:
            return contains_marker(artifact['directory'] / 'codex', CLAUDE_CREDENTIAL_SOURCES_MARKER)
        except OSError:
            return False

    def _select_runtime(self, alias, observed, artifact, reuse_host_runtime):
        candidates = [artifact]
        if reuse_host_runtime:
            # Adding an account does not require replacing a host's working CLI.
            # Only consider pinned local releases already registered on this host.
            from .store import Store
            known = {b.get('runtime_bundle') for p in Store(self.root).read()['profiles']
                     for b in p.get('remote_bindings',[]) if b.get('alias')==alias
                     and b.get('host_identity')==observed.get('host_identity') and b.get('prepared')}
            for path in sorted((self.root/'artifacts/remote').glob('*/manifest.json'))[:64]:
                if path.parent == artifact['directory']:continue
                try:
                    info=json.loads(path.read_text(encoding='utf-8-sig'))
                    bundle=info['version']+'-'+hashlib.sha256(json.dumps(info,sort_keys=True).encode()).hexdigest()[:16]
                    if bundle not in known:continue
                    if any(artifact.get(k) is True and info.get(k) is not True for k in
                           ('external_bridge_present','managed_sources_present','source_catalog_present','mixed_source_catalog_present',
                            *REMOTE_CAPABILITY_MARKERS)):continue
                    candidates.append(self._artifact(observed['platform'],observed['architecture'],directory=path.parent))
                except (OSError,ValueError,KeyError,RemoteError):continue
                if len(candidates)>=16:break
        helper=script_path(self.root,'scripts/remote_helpers/install.py')
        request={'candidates':[{'bundle_id':a['bundle_id'],'files':[
            {**f,'size':(a['directory']/f['path']).stat().st_size} for f in a['files']]} for a in candidates]}
        command=shlex.join([observed['python_path'],'-c',helper.read_text(encoding='utf-8'),'--preflight'])
        response=self._run(alias,command,input=json.dumps(request).encode(),timeout=45)
        result=self._json_result(response,'remote_preflight_failed')
        if result.get('status')=='cached':
            selected=next((a for a in candidates if a['bundle_id']==result.get('runtime_bundle')),None)
            if selected:return selected,True
        if result.get('status')=='upload':return artifact,False
        if result.get('status')=='insufficient_space':
            raise RemoteError('remote_disk_full','SSH 서버의 디스크 공간이 부족해 런타임을 준비하지 못했습니다. 기존 프로필과 대화는 유지됩니다.')
        raise RemoteError('remote_preflight_failed','SSH 런타임 저장 공간과 캐시를 확인하지 못했습니다.')

    @staticmethod
    def _add(tar, name: str, source: bytes | Path, mode: int) -> None:
        info = tarfile.TarInfo(name)
        info.mode = mode
        info.mtime = 0
        if isinstance(source, Path):
            info.size = source.stat().st_size
            with source.open("rb") as stream:
                tar.addfile(info, stream)
        else:
            info.size = len(source)
            tar.addfile(info, io.BytesIO(source))

    @staticmethod
    def _json_result(response, code: str) -> dict:
        if response.returncode != 0:
            try:
                if json.loads(response.stdout).get('code')=='remote_disk_full':
                    raise RemoteError('remote_disk_full','SSH 서버의 디스크 공간이 부족해 런타임을 준비하지 못했습니다. 기존 프로필과 대화는 유지됩니다.')
            except (ValueError,AttributeError,TypeError):pass
            raise RemoteError(code, "원격 준비 단계가 실패했습니다. 비밀정보가 포함될 수 있는 원격 출력은 표시하지 않습니다.")
        try:
            data = json.loads(response.stdout)
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except (ValueError, TypeError):
            raise RemoteError(code, "원격 작업의 완료 응답이 올바르지 않습니다.") from None
