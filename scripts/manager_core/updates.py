"""Official Windows package updates, with durable preparation and restore records.

Official sources (reviewed 2026-09-13):
https://learn.chatgpt.com/docs/enterprise/windows-deployment
https://learn.chatgpt.com/docs/enterprise/manage-app-updates

The stable, Store-signed MSIX is downloaded from OpenAI, never from an arbitrary
URL supplied by the UI. This module does not patch WindowsApps, force-close an
app, downgrade a package, change update policy, or resubmit a task.

Callbacks are supplied by the supervisor, not by a JSON client:
snapshot_instances() -> list of instance dicts
close_instance(instance) -> True only after graceful exit was verified
restore_instance(restore_entry) -> {"verified": True, ...}
verify_compatibility(package) -> {"compatible": True, "message": ...}
remote_snapshot() -> list of host dicts
acquire_maintenance(instances, transaction_id=...) -> {"verified": True, "token": opaque}
release_maintenance(lease) -> None
verify_recovery(transaction, installed) -> {"installer_settled": True,
    "maintenance_released": True, "cancelled_close_profiles": [...]}
The maintenance lease freezes new turns, children, and profile launches until
released. A read-only observation is not sufficient to close a live instance.
Supervisor-owned restoration is allowed under that lease and must be idempotent.
The supervisor associates leases with transaction_id for crash reconciliation.
Recovery proof is required for an installer that may have outlived this process.
An open instance needs process_id and job_state="idle" AND idle_verified=True.
It also needs created_at and executable matching the actual process identity.
The observation must include all its local and remote parents, children,
pending tools, approvals, and queued turns. Missing information blocks closure.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Callable
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
import uuid
import xml.etree.ElementTree as ET
import zipfile

PACKAGE_NAME = "OpenAI.Codex"
PACKAGE_FAMILY = "OpenAI.Codex_2p2nqsd0c76g0"
PACKAGE_PUBLISHER = "CN=50BDFD77-8903-4850-9FFE-6E8522F64D5B"
OFFICIAL_BASE = "https://persistent.oaistatic.com/codex-app-prod/"
OFFICIAL_SOURCES = [
    "https://learn.chatgpt.com/docs/enterprise/windows-deployment",
    "https://learn.chatgpt.com/docs/enterprise/manage-app-updates",
]
MAX_PACKAGE_BYTES = 2 * 1024 * 1024 * 1024
MAX_METADATA_BYTES = 2 * 1024 * 1024
TERMINAL_PHASES = {"complete", "blocked", "failed_before_install", "failed_install", "failed_restore"}
RESTORE_FIELDS = (
    "profile_id", "id", "host_id", "source_store_id", "thread_id",
    "active_thread_id", "window_placement", "policy_revision", "remote_only", "remote_maintenance_required",
)


class UpdateError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _needs_maintenance(instance):
    return bool(instance.get('process_id') or instance.get('remote_maintenance_required'))


def version_tuple(value: str) -> tuple[int, int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", value):
        raise UpdateError("invalid_version", "패키지 버전 형식을 확인할 수 없습니다.")
    parts = tuple(int(p) for p in value.split("."))
    if any(p > 65535 for p in parts):
        raise UpdateError("invalid_version", "패키지 버전 범위를 벗어났습니다.")
    return parts


def _official_url(url: str) -> str:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != "persistent.oaistatic.com"
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or parsed.query or parsed.fragment
            or parsed.path not in ("/codex-app-prod/ChatGPT-x64.msix",
                                   "/codex-app-prod/ChatGPT-arm64.msix")):
        raise UpdateError("untrusted_source", "공식 패키지 다운로드 주소가 아닙니다.")
    return url


class _OfficialRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _official_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _request(request: Request, timeout: int = 20):
    _official_url(request.full_url)
    request.add_header("User-Agent", "Codex-Multi-Profile/0.1")
    return build_opener(_OfficialRedirects).open(request, timeout=timeout)


class HttpRangeReader(io.RawIOBase):
    """Read only ZIP metadata; fail if the server ignores a byte-range request."""
    def __init__(self, url: str):
        self.url = _official_url(url)
        with _request(Request(url, method="HEAD")) as response:
            self.size = int(response.headers.get("Content-Length", "0"))
            self.etag = response.headers.get("ETag")
        if not 0 < self.size <= MAX_PACKAGE_BYTES:
            raise UpdateError("metadata_unavailable", "공식 패키지 크기를 확인할 수 없습니다.")
        self.position = 0
        self.downloaded = 0

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=io.SEEK_SET):
        target = (offset if whence == io.SEEK_SET else
                  self.position + offset if whence == io.SEEK_CUR else
                  self.size + offset if whence == io.SEEK_END else -1)
        if target < 0:
            raise ValueError("Invalid package metadata seek")
        self.position = target
        return target

    def read(self, size=-1):
        size = max(0, min(self.size - self.position,
                          self.size if size is None or size < 0 else size))
        if not size:
            return b""
        if self.downloaded + size > MAX_METADATA_BYTES:
            raise UpdateError("metadata_limit", "패키지 메타데이터가 허용 크기를 초과했습니다.")
        headers = {"Range": f"bytes={self.position}-{self.position + size - 1}"}
        if self.etag:
            headers["If-Match"] = self.etag
        with _request(Request(self.url, headers=headers)) as response:
            expected = f"bytes {self.position}-{self.position + size - 1}/{self.size}"
            if response.status != 206 or response.headers.get("Content-Range") != expected:
                raise UpdateError("range_unavailable", "서버가 안전한 메타데이터 부분 읽기를 지원하지 않습니다.")
            content = response.read(size + 1)
        if len(content) != size:
            raise UpdateError("metadata_changed", "업데이트 확인 중 패키지가 변경되었습니다.")
        self.downloaded += size
        self.position += size
        return content


def package_manifest(source) -> dict:
    """Inspect manifest only, with ZIP bomb and malformed XML size bounds."""
    try:
        with zipfile.ZipFile(source) as package:
            info = package.getinfo("AppxManifest.xml")
            if info.file_size > 512 * 1024:
                raise UpdateError("invalid_manifest", "패키지 매니페스트가 너무 큽니다.")
            xml = package.read(info)
        if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
            raise UpdateError("invalid_manifest", "지원하지 않는 패키지 매니페스트입니다.")
        root = ET.fromstring(xml)
        identity = next((node for node in root if node.tag.rsplit("}", 1)[-1] == "Identity"), None)
        if identity is None:
            raise ValueError("missing identity")
        attrs = identity.attrib
        result = {
            "name": attrs["Name"], "publisher": attrs["Publisher"],
            "version": attrs["Version"], "architecture": attrs["ProcessorArchitecture"].lower(),
        }
        version_tuple(result["version"])
        return result
    except UpdateError:
        raise
    except (KeyError, ValueError, zipfile.BadZipFile, ET.ParseError) as exc:
        raise UpdateError("invalid_manifest", "공식 패키지의 매니페스트를 읽지 못했습니다.") from exc


def validate_identity(package: dict, installed: dict | None = None) -> None:
    if (package.get("name") != PACKAGE_NAME or package.get("publisher") != PACKAGE_PUBLISHER
            or package.get("architecture") not in {"x64", "arm64"}):
        raise UpdateError("identity_mismatch", "설치된 Codex와 패키지 이름·게시자·아키텍처가 일치하지 않습니다.")
    version_tuple(package.get("version"))
    if installed is not None and package["architecture"] != installed["architecture"]:
        raise UpdateError("architecture_mismatch", "현재 Codex와 업데이트 패키지의 아키텍처가 다릅니다.")
    if "family" in package and package["family"] != PACKAGE_FAMILY:
        raise UpdateError("identity_mismatch", "설치된 Codex 패키지 식별자가 다릅니다.")
    if "signature_kind" in package and package["signature_kind"] != "Store":
        raise UpdateError("untrusted_signature", "설치된 Codex가 확인된 Store 서명 패키지가 아닙니다.")
    if "package_status" in package and package["package_status"] != "Ok":
        raise UpdateError("package_unhealthy", "설치된 Codex 패키지 상태를 먼저 복구해야 합니다.")


def _ps_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _powershell(script: str, timeout=30) -> str:
    shell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if not shell:
        raise UpdateError("unsupported_platform", "Windows PowerShell을 찾을 수 없습니다.")
    result = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command",
         "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new(); " + script],
        capture_output=True, encoding="utf-8", errors="replace", timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        # Never expose PowerShell stderr or other process output to the UI.
        raise UpdateError("windows_operation_failed", "Windows 패키지 작업에 실패했습니다.")
    return result.stdout.strip()


def installed_package() -> dict:
    raw = _powershell(
        "$p=Get-AppxPackage -Name OpenAI.Codex | Sort-Object Version -Descending | "
        "Select-Object -First 1; if(-not $p){throw 'Package not found'}; "
        "@{name=$p.Name; version=$p.Version.ToString(); publisher=$p.Publisher; "
        "architecture=$p.Architecture.ToString().ToLower(); family=$p.PackageFamilyName; "
        "full_name=$p.PackageFullName; install_location=$p.InstallLocation; "
        "signature_kind=$p.SignatureKind.ToString(); package_status=$p.Status.ToString()} | "
        "ConvertTo-Json -Compress")
    result = json.loads(raw)
    validate_identity(result)
    return result


def package_processes(installed: dict, managed_root=None) -> list[dict]:
    """Inventory only process identity, never command lines or credentials."""
    raw = _powershell(
        "Get-CimInstance Win32_Process | Where-Object {"
        "$_.Name -in @('ChatGPT.exe','Codex.exe') -or "
        "($_.ExecutablePath -and $_.ExecutablePath.StartsWith("
        + _ps_quote(installed["install_location"].rstrip("\\") + "\\")
        + ",[StringComparison]::OrdinalIgnoreCase))} | "
        "Select-Object @{n='process_id';e={$_.ProcessId}},"
        "@{n='parent_process_id';e={$_.ParentProcessId}},"
        "@{n='executable';e={$_.ExecutablePath}},"
        "@{n='created_at';e={$_.CreationDate.ToUniversalTime().ToString('o')}} | "
        "ConvertTo-Json -Compress")
    values = json.loads(raw) if raw else []
    values = values if isinstance(values, list) else [values]
    prefix = installed["install_location"].replace("/", "\\").rstrip("\\").casefold() + "\\"
    # Unknown executable paths are intentionally not assumed safe.
    def managed(executable):
        if not managed_root or not executable:
            return False
        path = Path(executable).resolve()
        return (path.is_relative_to(Path(managed_root).resolve())
                and path.name.casefold() == 'chatgpt.exe'
                and (path.parent / 'manager-desktop.json').is_file())
    return [p for p in values if not p.get("executable") or
            p["executable"].replace("/", "\\").casefold().startswith(prefix) or managed(p["executable"])]


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _atomic_json(path: Path, value: dict) -> None:
    from .store import atomic_json
    atomic_json(path, value)


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _process_identity(process: dict) -> tuple:
    return (int(process.get("process_id") or 0), process.get("created_at"),
            str(process.get("executable") or "").replace("/", "\\").casefold())


def _needs_recovery(transaction: dict) -> bool:
    closed = set(transaction.get("closed_profiles", []))
    restored = set(transaction.get("restored_profiles", []))
    return bool(transaction.get("recovery_required") or closed - restored
                or transaction.get("maintenance_state") in {"requested", "held", "release_failed"}
                or transaction.get("install_outcome") == "unknown"
                or any(i.get("state") == "requested" for i in transaction.get("close_intents", []))
                or transaction.get("status") in {
                    "recovery_required", "preparing", "closing_instances", "installing",
                    "restoring_instances", "recovering"})


def _lock_file(path: Path):
    """OS-owned byte lock: process crashes release ownership without deleting state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    try:
        if stream.seek(0, io.SEEK_END) == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return stream
    except (OSError, BlockingIOError):
        stream.close()
        raise UpdateError("update_in_progress", "다른 업데이트 작업이 진행 중입니다.") from None


def _unlock_file(stream):
    try:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        stream.close()


class UpdateManager:
    def __init__(
        self, root, *, snapshot_instances: Callable | None = None,
        close_instance: Callable | None = None, restore_instance: Callable | None = None,
        verify_compatibility: Callable | None = None, remote_snapshot: Callable | None = None,
        acquire_maintenance: Callable | None = None, release_maintenance: Callable | None = None,
        verify_recovery: Callable | None = None, check_restored_connections: Callable | None = None,
        recover_instance: Callable | None = None, recover_maintenance: Callable | None = None,
        installer=None,
        inventory: Callable | None = None, processes: Callable | None = None,
    ):
        self.root = Path(root).resolve()
        self.directory = self.root / "work/control-center/updates"
        self.snapshot_instances = snapshot_instances
        self.close_instance = close_instance
        self.restore_instance = restore_instance
        self.verify_compatibility = verify_compatibility
        self.remote_snapshot = remote_snapshot
        self.acquire_maintenance = acquire_maintenance
        self.release_maintenance = release_maintenance
        self.verify_recovery = verify_recovery
        self.check_restored_connections = check_restored_connections
        self.recover_instance = recover_instance
        self.recover_maintenance = recover_maintenance
        from .package_install import PackageInstaller
        self.installer = installer or PackageInstaller(self.root)
        self._install_transaction = None
        self.inventory = inventory or installed_package
        self.processes = processes or (lambda installed: package_processes(installed, self.root / 'artifacts/managed-desktop'))
        self._check_cache = None
        self._cache_time = 0

    def status(self) -> dict:
        path = self.directory / "transaction.json"
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {"status": "recovery_required", "message": "업데이트 기록을 확인해야 합니다."}
        return copy.deepcopy(self._check_cache) or {"status": "not_checked", "message": "업데이트 확인 전입니다."}

    def check(self) -> dict:
        """Read installed identity and latest remote manifest; no install or close."""
        result = {"checked_at": _utc_now(), "sources": OFFICIAL_SOURCES}
        try:
            installed = self.inventory()
            validate_identity(installed)
            result["installed"] = installed
            url = OFFICIAL_BASE + f"ChatGPT-{installed['architecture']}.msix"
            with HttpRangeReader(url) as source:
                latest = package_manifest(source)
                latest.update({"url": url, "size_bytes": source.size,
                               "signature": "not_yet_checked"})
            validate_identity(latest, installed)
            result["latest"] = latest
            current, target = version_tuple(installed["version"]), version_tuple(latest["version"])
            status = "available" if target > current else "up_to_date" if target == current else "installed_newer"
            result.update(status=status, message={
                "available": f"Codex {latest['version']} 업데이트를 사용할 수 있습니다.",
                "up_to_date": "설치된 Codex가 현재 공식 배포 버전입니다.",
                "installed_newer": "설치된 Codex가 공식 다운로드보다 새 버전입니다. 이전 버전으로 바꾸지 않습니다.",
            }[status])
        except Exception as exc:
            result.update(status="check_failed", code=exc.code if isinstance(exc, UpdateError) else "metadata_unavailable",
                          message=str(exc) if isinstance(exc, UpdateError) else "업데이트 정보를 확인하지 못했습니다.")
        self._check_cache, self._cache_time = copy.deepcopy(result), time.time()
        return result

    def _latest_check(self) -> dict:
        return copy.deepcopy(self._check_cache) if self._check_cache and time.time() - self._cache_time < 60 else self.check()

    def prepare(self) -> dict:
        """Download and verify the official package, without changing installed apps."""
        check = self._latest_check()
        if check["status"] != "available":
            return check
        try:
            path, digest = self._download(check["latest"])
            verified = self._validate_download(path, digest, check["installed"], check["latest"])
            prepared = {
                "status": "prepared", "message": "공식 패키지 다운로드와 서명 확인이 끝났습니다. 아직 설치하지 않았습니다.",
                "installed": check["installed"], "latest": check["latest"],
                "package_path": str(path), "sha256": digest, "verified": verified,
                "prepared_at": _utc_now(),
            }
            _atomic_json(self.directory / "prepared.json", prepared)
            return prepared
        except Exception as exc:
            return {"status": "prepare_failed", "code": getattr(exc, "code", "download_failed"),
                    "message": str(exc) if isinstance(exc, UpdateError) else "공식 패키지를 준비하지 못했습니다."}

    def _download(self, latest: dict) -> tuple[Path, str]:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"Codex-{latest['version']}-{latest['architecture']}.msix"
        if path.is_file():
            return path, _sha256(path)
        temp = path.with_suffix(".partial-" + uuid.uuid4().hex)
        count = 0
        try:
            with _request(Request(_official_url(latest["url"])), timeout=30) as response, temp.open("xb") as output:
                while chunk := response.read(1024 * 1024):
                    count += len(chunk)
                    if count > MAX_PACKAGE_BYTES:
                        raise UpdateError("package_too_large", "공식 패키지가 허용 크기를 초과했습니다.")
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if not count:
                raise UpdateError("empty_package", "공식 패키지가 비어 있습니다.")
            os.replace(temp, path)
            return path, _sha256(path)
        finally:
            if temp.exists():
                temp.unlink()

    def _validate_download(self, path, digest, installed, latest) -> dict:
        if _sha256(path) != digest:
            raise UpdateError("package_changed", "준비한 패키지 파일이 변경되었습니다.")
        identity = package_manifest(path)
        validate_identity(identity, installed)
        if identity["version"] != latest["version"]:
            raise UpdateError("release_changed", "확인 이후 공식 배포 버전이 변경되었습니다. 다시 확인해 주세요.")
        if version_tuple(identity["version"]) <= version_tuple(installed["version"]):
            raise UpdateError("downgrade_rejected", "같거나 이전 버전의 패키지를 설치하지 않습니다.")
        signature = json.loads(_powershell(
            "$s=Get-AuthenticodeSignature -LiteralPath " + _ps_quote(str(path)) + "; "
            "@{status=$s.Status.ToString(); signer=$s.SignerCertificate.Subject} | ConvertTo-Json -Compress"))
        if signature.get("status") != "Valid" or not signature.get("signer"):
            raise UpdateError("untrusted_signature", "다운로드한 패키지의 Windows 서명이 유효하지 않습니다.")
        return {"identity": identity, "signature": signature, "sha256": digest}

    def _blockers(self, instances: list[dict], installed: dict) -> list[dict]:
        blockers = []
        try:
            live = self.processes(installed)
        except Exception:
            return [{"code": "process_inventory_unknown", "message": "실행 중인 Codex 목록을 확인하지 못했습니다."}]
        by_pid = {int(p["process_id"]): p for p in live}
        tracked = {int(i["process_id"]) for i in instances if i.get("process_id")
                   and i.get("created_at") and i.get("executable")
                   and int(i["process_id"]) in by_pid
                   and _process_identity(i) == _process_identity(by_pid[int(i["process_id"])])}
        managed_pids = set(tracked)
        # Electron helper processes descend from managed main processes.
        for _ in range(len(live) + 1):
            before = len(managed_pids)
            managed_pids.update(int(p["process_id"]) for p in live
                                if int(p.get("parent_process_id") or 0) in managed_pids)
            if len(managed_pids) == before:
                break
        if any(int(p["process_id"]) not in managed_pids for p in live):
            blockers.append({"code": "unmanaged_instance", "message": "관리창 밖에서 실행 중인 Codex를 먼저 종료해야 합니다."})
        live_pids = {int(p["process_id"]) for p in live}
        for instance in instances:
            if not instance.get("process_id"):
                if instance.get('remote_maintenance_required') and instance.get('idle_verified') is not True:
                    blockers.append({'code': 'jobs_not_quiescent', 'profile_id': instance.get('profile_id', instance.get('id')),
                                     'message': 'SSH 작업이 끝나고 실행 상태가 확인되면 업데이트할 수 있습니다.'})
                continue
            profile_id = instance.get("profile_id", instance.get("id"))
            if int(instance["process_id"]) not in live_pids:
                blockers.append({"code": "instance_state_changed", "profile_id": profile_id,
                                 "message": "프로필 실행 상태가 바뀌었습니다. 목록을 새로 확인해야 합니다."})
                continue
            if int(instance["process_id"]) not in tracked:
                blockers.append({"code": "process_identity_unverified", "profile_id": profile_id,
                                 "message": "프로필의 실제 프로세스 식별자를 확인하지 못했습니다."})
            if instance.get("job_state") != "idle" or instance.get("idle_verified") is not True:
                blockers.append({"code": "jobs_not_quiescent", "profile_id": profile_id,
                                 "message": "진행 중인 작업 또는 확인되지 않은 자식·도구 작업이 있습니다."})
            if not self.close_instance or not self.restore_instance or not self.snapshot_instances:
                blockers.append({"code": "restart_hooks_unavailable", "profile_id": profile_id,
                                 "message": "프로필 종료·재개 연결이 아직 준비되지 않았습니다."})
            if not self.acquire_maintenance or not self.release_maintenance:
                blockers.append({"code": "maintenance_barrier_unavailable", "profile_id": profile_id,
                                 "message": "업데이트 동안 새 작업 시작을 막는 연결이 필요합니다."})
        return blockers

    def _compatibility(self, latest: dict) -> dict:
        if not self.verify_compatibility:
            return {"compatible": False, "message": "이 앱 버전과 패치 런타임의 호환 검증이 필요합니다."}
        try:
            result = self.verify_compatibility(copy.deepcopy(latest))
            if isinstance(result, bool):
                result = {"compatible": result}
            return result if isinstance(result, dict) else {"compatible": False}
        except Exception:
            return {"compatible": False, "message": "패치 런타임 호환 상태를 확인하지 못했습니다."}

    def plan(self, instances) -> dict:
        """Persist a server-owned plan. UI may later submit only its plan_id."""
        check = self._latest_check()
        plan = {
            "plan_id": str(uuid.uuid4()), "created_at": _utc_now(), "expires_at": time.time() + 600,
            "status": "blocked", "message": "업데이트를 준비하고 있습니다.",
            "installed": check.get("installed"), "latest": check.get("latest"),
            "blockers": [], "restore_manifest": [], "remote_pending": [],
            "phases": ["prepare", "wait_for_idle", "save_restore_manifest", "graceful_close",
                       "official_install", "verify_installed", "restore_instances", "verify_restore"],
        }
        if check["status"] != "available":
            if check['status'] in ('up_to_date', 'installed_newer'):
                plan.update(status=check['status'], message=check['message'])
            else:
                plan["blockers"].append({"code": check["status"], "message": check["message"]})
        else:
            snapshots = copy.deepcopy(list(instances))
            plan["instances"] = snapshots
            plan["blockers"] = self._blockers(snapshots, plan["installed"])
            compatibility = self._compatibility(plan["latest"])
            plan["compatibility"] = compatibility
            if compatibility.get("compatible") is not True:
                plan["blockers"].append({"code": "runtime_compatibility_unverified",
                                        "message": compatibility.get("message", "런타임 호환 검증이 필요합니다.")})
            plan["restore_manifest"] = [
                {key: copy.deepcopy(i[key]) for key in RESTORE_FIELDS if key in i}
                for i in snapshots if _needs_maintenance(i)
            ]
            if self.remote_snapshot:
                try:
                    for host in self.remote_snapshot():
                        if host.get("online") is not True or host.get("compatible") is not True:
                            plan["remote_pending"].append({
                                "host_id": host.get("host_id", host.get("id", host.get("alias"))),
                                "status": "pending_next_connection",
                                "message": "다음 연결 시 원격 런타임 호환성을 확인합니다.",
                            })
                except Exception:
                    plan["remote_pending"].append({"status": "unknown", "message": "원격 상태 확인이 대기 중입니다."})
            if not plan["blockers"]:
                plan.update(status="ready", message="업데이트를 적용할 준비가 되었습니다.")
            else:
                plan["message"] = plan["blockers"][0]["message"]
        # Persist only the fields the updater needs. Never store environment or auth.
        if "instances" in plan:
            plan["instances"] = [{k: copy.deepcopy(i[k]) for k in
                                 (*RESTORE_FIELDS, "process_id", "created_at", "executable", "job_state", "idle_verified") if k in i}
                                 for i in plan["instances"]]
        _atomic_json(self.directory / "plans" / (plan["plan_id"] + ".json"), plan)
        return plan

    def _journal(self, transaction, status, message, **fields):
        transaction.update(status=status, message=message, updated_at=_utc_now(), **fields)
        _atomic_json(self.directory / "transaction.json", transaction)
        return copy.deepcopy(transaction)

    def apply(self, plan) -> dict:
        """Apply a fresh supervisor plan; never trust a client-supplied package path."""
        plan_id = plan if isinstance(plan, str) else plan.get("plan_id", "")
        try:
            parsed_id = str(uuid.UUID(str(plan_id)))
        except ValueError:
            raise UpdateError("invalid_plan", "업데이트 계획을 찾을 수 없습니다.") from None
        path = self.directory / "plans" / (parsed_id + ".json")
        if not path.is_file():
            raise UpdateError("invalid_plan", "업데이트 계획을 찾을 수 없습니다.")
        trusted = json.loads(path.read_text(encoding="utf-8"))
        if trusted['status'] in ('up_to_date', 'installed_newer'):
            return {key: trusted[key] for key in ('status', 'message', 'installed', 'latest')}
        if trusted["status"] != "ready" or trusted["expires_at"] < time.time():
            return {"status": "blocked", "code": "plan_not_ready",
                    "message": "업데이트 조건을 다시 확인해야 합니다.", "blockers": trusted.get("blockers", [])}
        self.directory.mkdir(parents=True, exist_ok=True)
        lock = self.directory / "apply.lock"
        try:
            lock_stream = _lock_file(lock)
        except UpdateError:
            return {"status": "recovery_required", "message": "다른 업데이트 또는 중단된 업데이트를 먼저 확인해야 합니다."}
        transaction = {
            "transaction_id": str(uuid.uuid4()), "plan_id": parsed_id,
            "installed_before": trusted["installed"], "target": trusted["latest"],
            "restore_manifest": trusted["restore_manifest"],
            "remote_pending": trusted["remote_pending"], "closed_profiles": [], "close_intents": [],
        }
        install_started = False
        lease = None
        try:
            previous = self.status()
            if _needs_recovery(previous):
                return {"status": "recovery_required", "message": "중단된 업데이트 기록을 먼저 확인해야 합니다."}
            installed = self.inventory()
            validate_identity(installed)
            if installed["version"] != trusted["installed"]["version"]:
                return self._journal(transaction, "blocked", "설치된 앱 버전이 변경되었습니다. 다시 확인해 주세요.")
            fresh = self.snapshot_instances() if self.snapshot_instances else trusted.get("instances", [])
            blockers = self._blockers(fresh, installed)
            fresh_ids = {i.get("profile_id", i.get("id")) for i in fresh if _needs_maintenance(i)}
            planned_ids = {i.get("profile_id", i.get("id")) for i in trusted["instances"] if _needs_maintenance(i)}
            if fresh_ids != planned_ids:
                blockers.append({"code": "instances_changed", "message": "열려 있는 프로필 목록이 변경되었습니다."})
            compatibility = self._compatibility(trusted["latest"])
            if compatibility.get("compatible") is not True:
                blockers.append({"code": "runtime_compatibility_unverified", "message": "런타임 호환 검증이 필요합니다."})
            if blockers:
                return self._journal(transaction, "blocked", blockers[0]["message"], blockers=blockers)
            self._journal(transaction, "preparing", "공식 패키지를 준비하고 서명을 확인합니다.")
            package_path, digest = self._download(trusted["latest"])
            self._validate_download(package_path, digest, installed, trusted["latest"])
            if any(_needs_maintenance(i) for i in fresh):
                self._journal(transaction, "preparing", "업데이트 동안 새 작업 시작을 잠시 멈춥니다.",
                              maintenance_state="requested")
                lease = self.acquire_maintenance(copy.deepcopy(fresh), transaction_id=transaction["transaction_id"])
                if not isinstance(lease, dict) or lease.get("verified") is not True:
                    raise UpdateError("maintenance_not_acquired", "새 작업 시작을 막지 못해 프로필을 종료하지 않습니다.")
                self._journal(transaction, "preparing", "새 작업 시작이 멈춘 것을 확인했습니다.",
                              maintenance_state="held")
            # Download can take minutes. Re-observe immediately before any closure.
            fresh = self.snapshot_instances() if self.snapshot_instances else fresh
            blockers = self._blockers(fresh, installed)
            if {i.get("profile_id", i.get("id")) for i in fresh if _needs_maintenance(i)} != planned_ids:
                blockers.append({"code": "instances_changed", "message": "다운로드 중 열린 프로필 목록이 변경되었습니다."})
            if blockers:
                return self._journal(transaction, "blocked", blockers[0]["message"], blockers=blockers)
            transaction["restore_manifest"] = [
                {key: copy.deepcopy(i[key]) for key in RESTORE_FIELDS if key in i}
                for i in fresh if _needs_maintenance(i)]
            self._journal(transaction, "closing_instances", "작업이 멈춘 프로필을 정상 종료합니다.")
            for instance in fresh:
                if _needs_maintenance(instance):
                    intent = {"profile_id": instance.get("profile_id", instance.get("id")),
                              "process_id": instance.get("process_id"), "created_at": instance.get("created_at"),
                              "executable": instance.get("executable"), "remote_only": instance.get('remote_only', False),
                              "state": "requested"}
                    transaction["close_intents"].append(intent)
                    self._journal(transaction, "closing_instances", "프로필 정상 종료 요청을 기록했습니다.")
                    if self.close_instance(copy.deepcopy(instance)) is not True:
                        raise UpdateError("graceful_close_failed", "프로필 정상 종료를 확인하지 못했습니다. 강제 종료하지 않습니다.")
                    intent["state"] = "closed"
                    transaction["closed_profiles"].append(instance.get("profile_id", instance.get("id")))
                    self._journal(transaction, "closing_instances", "프로필 정상 종료를 확인했습니다.")
            if self.processes(installed):
                raise UpdateError("package_in_use", "Codex가 아직 실행 중이어서 설치하지 않았습니다.")
            self._journal(transaction, "installing", "Windows가 공식 Codex 패키지를 업데이트하고 있습니다.",
                          install_outcome="unknown")
            install_started = True
            self._install_transaction = transaction
            try:
                self._install(package_path, digest)
            finally:
                self._install_transaction = None
            self._journal(transaction, "installing", "Windows 패키지 설치 명령이 완료됐습니다.",
                          install_outcome="command_completed")
            actual = self.inventory()
            validate_identity(actual, installed)
            if actual["version"] != trusted["latest"]["version"]:
                raise UpdateError("install_not_verified", "설치된 버전이 목표 버전과 일치하지 않습니다.")
            # Installation is already verified even if a reopened connection
            # needs time. This plan must never install the package again.
            trusted["status"] = "consumed"
            _atomic_json(path, trusted)
            self._check_cache = None
            self._journal(transaction, "restoring_instances", "업데이트를 확인하고 이전 프로필을 다시 엽니다.",
                          installed_after=actual)
            failures = self._restore(transaction)
            if failures:
                return self._journal(transaction, "failed_restore", "앱 업데이트는 완료됐지만 일부 프로필 재개를 확인해야 합니다.",
                                     restore_failures=failures)
            if transaction.get('restore_waiting'):
                return self._journal(transaction, 'connecting', self._connection_message(transaction))
            return self._journal(transaction, "complete", "Codex 업데이트와 열려 있던 프로필 재개를 확인했습니다.")
        except Exception as exc:
            code = getattr(exc, "code", "update_failed")
            phase = "failed_install" if install_started else "failed_before_install"
            actual = None
            try:
                actual = self.inventory()
                validate_identity(actual)
            except Exception:
                pass
            if transaction.get('install_outcome') == 'unknown' and self.installer.inspect(transaction).get('installer_settled') is True:
                transaction['install_outcome'] = 'settled_after_error'
            # Restore only on known compatible actual state. Never pretend an MSIX rollback occurred.
            can_restore = bool(transaction.get("install_outcome") != "unknown" and actual
                               and (actual["version"] == trusted["installed"]["version"]
                               or (actual["version"] == trusted["latest"]["version"]
                                   and self._compatibility(actual).get("compatible") is True)))
            failures = self._restore(transaction) if can_restore else []
            return self._journal(transaction, phase,
                                 str(exc) if isinstance(exc, UpdateError) else "업데이트 작업이 완료되지 않았습니다.",
                                 code=code, installed_after=actual, restore_failures=failures,
                                 recovery_required=not can_restore or bool(failures) or
                                 any(i.get("state") == "requested" for i in transaction["close_intents"]))
        finally:
            try:
                if lease is not None and self.release_maintenance:
                    try:
                        self.release_maintenance(lease)
                        self._journal(transaction, transaction["status"], transaction["message"],
                                      maintenance_state="released")
                    except Exception:
                        self._journal(transaction, "recovery_required", "업데이트 작업 잠금 해제를 확인해야 합니다.",
                                      maintenance_state="release_failed", recovery_required=True)
                        raise UpdateError("maintenance_release_failed", "업데이트 작업 잠금 해제를 확인해야 합니다.") from None
            finally:
                _unlock_file(lock_stream)

    def _install(self, path: Path, digest: str) -> None:
        transaction = self._install_transaction
        if not transaction:
            raise UpdateError('installer_context_missing', '검증된 업데이트 요청이 필요합니다.')
        self.installer.run(transaction, path, digest, bind=lambda reference: self._journal(
            transaction, 'installing', '별도 설치 프로세스에서 Windows 업데이트를 진행합니다.', installer=reference))

    def _restore(self, transaction, *, recovering=False) -> list[dict]:
        failures = []
        closed = set(transaction.get("closed_profiles", []))
        restored = set(transaction.get("restored_profiles", []))
        for item in transaction["restore_manifest"]:
            profile_id = item.get("profile_id", item.get("id"))
            if profile_id not in closed or profile_id in restored:
                continue
            try:
                waiting = transaction.get('restore_waiting', {}).get(profile_id)
                if waiting:
                    if not self.check_restored_connections:
                        raise UpdateError('restore_checker_unavailable', 'SSH 계정 연결 확인 처리가 필요합니다.')
                    result = self.check_restored_connections(profile_id, transaction['transaction_id'], waiting['generation'])
                    if isinstance(result, dict) and result.get('verified') is False:
                        waiting.update(message=result.get('message', 'SSH 계정 연결 확인 중'),
                                       remote_connections=result.get('remote_connections'))
                        self._journal(transaction, 'restoring_instances', waiting['message'])
                        continue
                else:
                    result = (self.recover_instance(copy.deepcopy(item), transaction['transaction_id'])
                              if recovering and self.recover_instance else
                              self.restore_instance(copy.deepcopy(item)) if self.restore_instance else None)
                    if (isinstance(result, dict) and result.get('code') == 'remote_account_pending'
                            and result.get('profile_id') == profile_id
                            and result.get('profile_reopened') is True and result.get('runtime_initialized') is True):
                        generation = str(uuid.UUID(result['generation']))
                        transaction.setdefault('restore_waiting', {})[profile_id] = dict(
                            generation=generation, message=result.get('message', 'SSH 계정 연결 확인 중'),
                            remote_connections=result.get('remote_connections'))
                        self._journal(transaction, 'restoring_instances', '다시 열린 프로필의 SSH 연결을 기다립니다.')
                        continue
                if not isinstance(result, dict) or result.get("verified") is not True:
                    raise UpdateError("restore_not_verified", "프로필 재개를 확인하지 못했습니다.")
                transaction.get('restore_waiting', {}).pop(profile_id, None)
                restored.add(profile_id)
                transaction["restored_profiles"] = list(restored)
                self._journal(transaction, "restoring_instances", "프로필 재개 상태를 확인하고 있습니다.")
            except Exception:
                failures.append({"profile_id": profile_id, "code": "restore_not_verified"})
        return failures

    @staticmethod
    def _connection_message(transaction):
        messages = dict.fromkeys(item.get('message', 'SSH 계정 연결 확인 중')
                                 for item in transaction.get('restore_waiting', {}).values())
        return '다시 열린 계정 연결을 확인하고 있습니다. ' + ' / '.join(str(value) for value in messages)

    def poll_restore(self) -> dict:
        """Advance only already reopened connections; never reinstall or reopen."""
        try:
            lock_stream = _lock_file(self.directory / 'apply.lock')
        except UpdateError:
            return {'status': 'connecting', 'message': '다른 업데이트 상태 확인이 끝나기를 기다립니다.'}
        try:
            transaction = self.status()
            if transaction.get('status') != 'connecting':
                return transaction
            remaining = set(transaction.get('closed_profiles', [])) - set(transaction.get('restored_profiles', []))
            waiting = transaction.get('restore_waiting', {})
            if (not remaining or not remaining.issubset(waiting)
                    or transaction.get('install_outcome') == 'unknown'
                    or transaction.get('maintenance_state') in {'requested', 'held', 'release_failed'}):
                return self._journal(transaction, 'recovery_required', '업데이트 복원 기록을 확인해야 합니다.', recovery_required=True)
            failures = self._restore(transaction)
            if failures:
                return self._journal(transaction, 'recovery_required', '다시 열린 프로필의 연결 상태를 확인해야 합니다.',
                                     restore_failures=failures, recovery_required=True)
            if transaction.get('restore_waiting'):
                return self._journal(transaction, 'connecting', self._connection_message(transaction))
            actual = self.inventory()
            validate_identity(actual)
            target = transaction.get('target', {}).get('version')
            before = transaction.get('installed_before', {}).get('version')
            if actual['version'] not in (before, target):
                return self._journal(transaction, 'recovery_required', '기다리는 동안 설치 버전이 바뀌었습니다.',
                                     observed_installed=actual, recovery_required=True)
            return self._journal(transaction, 'complete' if actual['version'] == target else 'failed_install',
                                 '설치 상태와 다시 열린 프로필의 연결을 확인했습니다.',
                                 observed_installed=actual, restore_failures=[], recovery_required=False)
        finally:
            _unlock_file(lock_stream)

    def recovery_status(self) -> dict:
        """Read crash state and installed reality. Never rerun an uncertain install."""
        transaction = self.status()
        try:
            actual = self.inventory()
            validate_identity(actual)
            transaction["observed_installed"] = actual
        except Exception:
            transaction["observed_installed"] = None
        transaction["install_retried"] = False
        transaction["lock_present"] = (self.directory / "apply.lock").exists()
        transaction["recovery_required"] = _needs_recovery(transaction)
        return transaction

    def recover(self) -> dict:
        """Explicitly reconcile an interrupted transaction; never repeat installation.

        The supervisor restore callback must check that no instance already owns
        the profile before starting it. It restores a view, never a submitted turn.
        """
        try:
            lock_stream = _lock_file(self.directory / "apply.lock")
        except UpdateError:
            return {"status": "recovery_required", "message": "업데이트 작업이 아직 진행 중입니다."}
        try:
            transaction = self.status()
            if not _needs_recovery(transaction):
                return transaction
            actual = self.inventory()
            validate_identity(actual)
            proof = self.verify_recovery(copy.deepcopy(transaction), copy.deepcopy(actual)) if self.verify_recovery else {}
            proof = proof if isinstance(proof, dict) else {}
            if transaction.get("install_outcome") == "unknown":
                if proof.get("installer_settled") is not True:
                    return self._journal(transaction, "recovery_required", "이전 Windows 설치 작업이 끝났는지 확인해야 합니다.",
                                         observed_installed=actual, install_retried=False)
                transaction["install_outcome"] = "settled_after_recovery"
            before = transaction.get("installed_before", {}).get("version")
            target = transaction.get("target", {}).get("version")
            compatible = actual["version"] == before or (
                actual["version"] == target and self._compatibility(actual).get("compatible") is True)
            if not compatible:
                return self._journal(transaction, "recovery_required", "현재 설치 버전의 런타임 호환 확인이 필요합니다.",
                                     observed_installed=actual, install_retried=False)
            if transaction.get("maintenance_state") in {"requested", "held", "release_failed"}:
                if proof.get("maintenance_released") is not True and self.recover_maintenance:
                    # Explicit recovery only, after installer outcome and actual
                    # package compatibility are known. Status reads never start.
                    recovered_proof = self.recover_maintenance(copy.deepcopy(transaction), copy.deepcopy(actual))
                    proof = {**proof, **recovered_proof} if isinstance(recovered_proof, dict) else proof
                if not isinstance(proof, dict) or proof.get("maintenance_released") is not True:
                    return self._journal(transaction, "recovery_required", "이전 업데이트 작업 잠금의 해제를 확인해야 합니다.",
                                         observed_installed=actual, install_retried=False)
                transaction["maintenance_state"] = "released"
            live = self.processes(actual)
            live_identities = {_process_identity(p) for p in live}
            closed = set(transaction.get("closed_profiles", []))
            pending_closes = False
            for intent in transaction.get("close_intents", []):
                if intent.get("state") != "requested":
                    continue
                if _process_identity(intent) in live_identities:
                    if intent["profile_id"] in proof.get("cancelled_close_profiles", []):
                        intent["state"] = "cancelled"
                    else:
                        pending_closes = True
                else:
                    intent["state"] = "closed"
                    closed.add(intent["profile_id"])
            transaction["closed_profiles"] = list(closed)
            if pending_closes:
                return self._journal(transaction, "recovery_required", "이전 프로필 종료 요청이 아직 진행 중일 수 있습니다.",
                                     observed_installed=actual, install_retried=False)
            self._journal(transaction, "recovering", "실제 프로세스와 이전 프로필 재개 기록을 확인합니다.",
                          observed_installed=actual, install_retried=False)
            failures = self._restore(transaction, recovering=True)
            if failures:
                return self._journal(transaction, "recovery_required", "일부 프로필의 재개를 확인해야 합니다.",
                                     restore_failures=failures, recovery_required=True)
            if transaction.get('restore_waiting'):
                return self._journal(transaction, 'connecting', self._connection_message(transaction),
                                     recovery_required=False, restore_failures=[])
            return self._journal(transaction, "complete" if actual["version"] == target else "failed_install",
                                 "중단된 업데이트의 실제 설치 상태와 프로필 재개를 확인했습니다.",
                                 restore_failures=[], recovery_required=False, install_retried=False)
        except Exception as exc:
            return {"status": "recovery_required", "message": str(exc) if isinstance(exc, UpdateError)
                    else "중단된 업데이트 상태를 안전하게 확인하지 못했습니다.", "install_retried": False}
        finally:
            _unlock_file(lock_stream)
