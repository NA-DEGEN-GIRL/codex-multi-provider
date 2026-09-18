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
import uuid


ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
ARCHES = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}
MARKER = "CODEX_MANAGER_INSPECT_V1"
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
                "os", "arch", "home", "uid", "machine", "cli", "cli_version", "stock_login", "python", "python_version", "host_identity"
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
        if platform != "linux" or not arch:
            result["blockers"].append("platform_not_supported")
        if not observed["cli"]:
            result["blockers"].append("stock_cli_missing")
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

    def prepare(self, alias: str, profile_id: str, profile_home: Path | str,
                model_ids: list[str], *, primary_model_id=None, primary_settings=None, selection_mode='automatic', reuse_host_runtime=False) -> dict:
        alias = self._alias(alias)
        try:
            if str(uuid.UUID(profile_id)) != profile_id:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise RemoteError("invalid_profile", "관리 프로필 ID가 올바르지 않습니다.") from None
        # The argument is intentionally not a source of files, auth, or secrets.
        # A remote profile is generated from the registry, never copied wholesale.
        observed = self.inspect(alias)
        if not observed.get("preparation_supported"):
            return {**observed, "status": "blocked", "prepared": False}
        artifact = self._artifact(observed["platform"], observed["architecture"])
        artifact, reused = self._select_runtime(alias, observed, artifact, reuse_host_runtime and selection_mode != 'external_only')
        if selection_mode == 'external_only':
            from remote_helpers.package_runtime import contains_marker
            if not contains_marker(artifact['directory'] / 'codex', b'External-only subagents:'):
                raise RemoteError('runtime_update_required', '외부 모델 전용 정책을 지원하는 SSH 런타임 업데이트가 필요합니다.')
        base = PurePosixPath(observed["remote_home"]) / ".local/share/codex-control-center"
        remote_profile = base / "profiles" / profile_id
        registry = self._registry_instance()
        rendered = registry.render_for_host(str(remote_profile / "codex"), bool(model_ids), model_ids,
                                            existing_config="[features]\ncode_mode_host = true\n",
                                            primary_model_id=primary_model_id, primary_settings=primary_settings, selection_mode=selection_mode)
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
        revision = hashlib.sha256(json.dumps({"files": rendered["files"], "runtime": artifact["bundle_id"],
                                             "host_identity": observed["host_identity"],
                                             "installer": hashlib.sha256(helper.read_bytes()).hexdigest(),
                                             "launcher": hashlib.sha256(launcher_bytes).hexdigest(),
                                             "native_controller": hashlib.sha256(controller_bytes).hexdigest(),
                                             "ws_client": hashlib.sha256(websocket_bytes).hexdigest(),
                                             "common": hashlib.sha256(common_bytes).hexdigest(),
                                             "managed_sources": hashlib.sha256(managed_bytes).hexdigest(),
                                             "catalog_legacy": hashlib.sha256(legacy_bytes).hexdigest() if legacy_bytes is not None else None},
                                            sort_keys=True, ensure_ascii=False).encode()).hexdigest()
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
        with tempfile.TemporaryFile() as archive:
            with tarfile.open(fileobj=archive, mode="w") as tar:
                self._add(tar, "manifest.json", json.dumps(metadata).encode(), 0o600)
                for name, (source, mode) in entries.items():
                    if reused and name.startswith('runtime/'):continue
                    self._add(tar, name, source, mode)
            archive.seek(0)
            command = shlex.quote(observed["python_path"]) + " -c " + shlex.quote(helper.read_text(encoding="utf-8"))
            installed = self._run(alias, command, stdin=archive, timeout=600)
        payload = self._json_result(installed, "remote_install_failed")
        if payload.get("status") != "prepared" or payload.get("revision") != revision:
            raise RemoteError("remote_install_failed", "원격 설치 완료를 확인하지 못했습니다. 기존 CLI는 유지됩니다.")
        # Deliver only selected provider credentials over SSH stdin after installation.
        credential_models = list(dict.fromkeys(model_ids + ([primary_model_id] if primary_model_id else [])))
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
        return {"id": "ssh:" + alias, "alias": alias, "profile_id": profile_id,
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
                           ('external_bridge_present','managed_sources_present','source_catalog_present','mixed_source_catalog_present')):continue
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
