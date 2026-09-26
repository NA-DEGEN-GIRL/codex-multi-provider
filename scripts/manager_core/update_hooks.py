"""Supervisor hooks for verified, manager-owned Codex update transactions.

No constructor or read-only method closes a window. Only close_instance(),
called by an explicit UpdateManager.apply(), can request a normal WM_CLOSE.
It first holds the proxy admission lease, obtains strict runtime writer-release
proof for every loaded managed subtree, and rechecks process birth identity.

Wiring:
    hooks = UpdateHooks(root, store, instances)
    updater = UpdateManager(root, **hooks.callbacks())
    # instances.launch_admission = hooks.launch_admission fences preparation
    # and process publication against updates; different profiles share it.
    # Ordinary window waits happen after release.
    # hooks.restore_instance() uses its own scoped restoration permit.

Compatibility evidence is deliberately not inferred from a version string.
artifacts/manager-runtime/update-compatibility.json:
  {"version": 1, "entries": [{
    "app_version": "26.903.9818.0",
    "runtime_sha256": "...", "runtime_proxy_sha256": "...",
    "runtime_admin_sha256": "...", "runtime_proxy_module_sha256": "...",
    "status": "verified", "checks": {
      "initialize": true, "managed_idle_status": true,
      "managed_close_idle": true, "proxy_maintenance": true,
      "auth_binding": true
    }, "evidence_path": "work/.../verified-report.json"
  }]}
The evidence file must be in this workspace and state verified:true. It is
written by actual validation, never by this module. Future app versions need
their own evidence; an old or stock runtime is never silently substituted.
"""
from __future__ import annotations

from .release_code import script_path
import copy
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from uuid import UUID, uuid4

from .store import atomic_json, identifier, now
from .updates import UpdateError, version_tuple, _lock_file, _unlock_file
from .instances import process_identity
from .process_state import process_liveness as _process_liveness
from .runtime_admin import AdminClient, AdminError
from .remote_readiness import RemoteReadiness, pending_message
from .runtime_build import resolve as resolve_runtime


_REQUIRED_CHECKS = (
    "initialize", "managed_idle_status", "managed_close_idle",
    "proxy_maintenance", "auth_binding",
)
_LAUNCH_ADMISSION_WAIT = 10.0
_LAUNCH_ADMISSION_POLL = 0.05


def _hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _read_json(path, limit=2_000_000):
    path = Path(path)
    if path.stat().st_size > limit:
        raise UpdateError("invalid_update_state", "업데이트 상태 파일이 너무 큽니다.")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise UpdateError("invalid_update_state", "업데이트 상태 파일을 확인할 수 없습니다.")
    return value


def _cim_birth(ticks):
    """CIM CreationDate has microsecond precision; FILETIME keeps 100 ns ticks."""
    if type(ticks) is not int or ticks <= 0:
        raise UpdateError("process_identity_unknown", "프로세스 시작 시각을 확인할 수 없습니다.")
    seconds, fraction = divmod(ticks, 10_000_000)
    timestamp = datetime.fromtimestamp(seconds - 11644473600, timezone.utc)
    return timestamp.strftime("%Y-%m-%dT%H:%M:%S") + f".{fraction // 10:06d}0Z"


def _same_process(first, second):
    return bool(first and second
                and first.get("process_id") == second.get("process_id")
                and first.get("process_created") == second.get("process_created")
                and os.path.normcase(first.get("executable_path", ""))
                == os.path.normcase(second.get("executable_path", "")))


def _native_close(instance):
    """App code only. Request normal close of exactly this HWND; never kill."""
    from . import rust_service
    if rust_service.enabled():
        return rust_service.request('process.close', profile_id=instance['id'], generation=instance['generation'],
                                    window_handle=instance.get('window_handle'))
    if os.name != "nt":
        raise UpdateError("unsupported_platform", "Windows 프로필 종료만 지원합니다.")
    current = process_identity(instance["process_id"])
    if not _same_process(current, instance):
        raise UpdateError("process_identity_changed", "프로필 프로세스가 변경되어 종료하지 않았습니다.")
    handle = instance.get("window_handle")
    if not isinstance(handle, int) or handle <= 0:
        raise UpdateError("window_unknown", "프로필의 기본 창을 확인하지 못했습니다.")
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.IsWindow.argtypes = [wintypes.HWND]
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    owner = wintypes.DWORD()
    user.GetWindowThreadProcessId(handle, ctypes.byref(owner))
    if not user.IsWindow(handle) or owner.value != current["process_id"]:
        raise UpdateError("window_identity_changed", "프로필 창의 소유 프로세스가 변경되었습니다.")
    if not user.PostMessageW(handle, 0x0010, 0, 0):  # WM_CLOSE, no forced process termination
        raise UpdateError("graceful_close_failed", "프로필 정상 종료를 요청하지 못했습니다.")


class UpdateHooks:
    def __init__(self, root, store, instances, *, admin_factory=None,
                 identity=None, native_close=None, runtime_resolver=None, idle_stop=None,
                 navigate=None, verify_selection=None, remote_snapshot=None,
                 host_inventory=None,
                 remote_maintenance=None,
                 remote_readiness=None,
                 liveness=None,
                 clock=None, sleep=None, timeout=30):
        self.root = Path(root).resolve()
        self.remote_readiness = remote_readiness or RemoteReadiness(self.root)
        self.store, self.instances = store, instances
        self.directory = self.store.directory / "updates" / "maintenance"
        self.admin_factory = admin_factory or (
            lambda profile: AdminClient(self.root, profile["id"], profile["generation"]))
        self.identity = identity or process_identity
        self.liveness = liveness or _process_liveness
        self.native_close = native_close or _native_close
        self.idle_stop = idle_stop or self._finish_idle_exit
        self.runtime_resolver = runtime_resolver or resolve_runtime
        self.navigate = navigate or self._navigate
        self.verify_selection = verify_selection
        self._remote_snapshot = remote_snapshot
        self.host_inventory = host_inventory
        self.remote_maintenance = remote_maintenance
        self.clock, self.sleep, self.timeout = clock or time.monotonic, sleep or time.sleep, timeout
        self._mutex = threading.RLock()
        from .launch_queue import LaunchQueue
        self._launch_queue = LaunchQueue()
        # Launches admitted together share one hold of the cross-process lock.
        self._external_lock = threading.Lock()
        self._external = None
        self._external_users = 0
        self._restoration = threading.local()
        self._admission = threading.local()

    def callbacks(self):
        return dict(
            snapshot_instances=self.snapshot_instances,
            close_instance=self.close_instance,
            restore_instance=self.restore_instance,
            verify_compatibility=self.verify_compatibility,
            acquire_maintenance=self.acquire_maintenance,
            release_maintenance=self.release_maintenance,
            verify_recovery=self.verify_recovery,
            check_restored_connections=self.check_restored_connections,
            recover_instance=self.recover_update_instance,
            recover_maintenance=self.recover_update_maintenance,
            remote_snapshot=self.remote_snapshot,
        )

    def guard_launch(self, profile_id):
        """Call before every ordinary Instances.show/profile or conversation launch."""
        profile_id = identifier(profile_id)
        data = self.store.read()
        owner = getattr(self._restoration, "transaction_id", None)
        for maintenance in (data.get("update_maintenance"),
                            data.get("profile_maintenance", {}).get(profile_id)):
            if maintenance and maintenance.get("state") != "released":
                if owner != maintenance.get("transaction_id"):
                    raise UpdateError("update_maintenance", "이 프로필의 설정 적용 또는 업데이트가 끝나면 열 수 있습니다.")
        return profile_id

    def authorize_restoration_generation(self, profile):
        ssh_owner = getattr(self._restoration, 'ssh_transaction_id', None)
        if ssh_owner is not None:
            def authorize_ssh(data):
                gate = data.get('ssh_maintenance', {}).get(profile['id'])
                if not gate or gate.get('transaction_id') != ssh_owner or gate.get('state') != 'held':
                    raise UpdateError('restoration_changed', 'SSH 준비 중 프로필 실행 권한이 변경되었습니다.')
                gate['generation'] = profile['generation']
            self.store.mutate(authorize_ssh)
        owner = getattr(self._restoration, 'transaction_id', None)
        if owner is None:
            return
        def authorize(data):
            gate = data.get('profile_maintenance', {}).get(profile['id'])
            if not gate or gate.get('state') == 'released':
                gate = data.get('update_maintenance')
            if not gate or gate.get('transaction_id') != owner or gate.get('state') != 'held':
                raise UpdateError('restoration_changed', '설정 적용 후 다시 열기 권한이 변경되었습니다.')
            gate.setdefault('restoring_generations', {})[profile['id']] = identifier(profile['generation'])
        self.store.mutate(authorize)

    def _maintenance_for(self, profile_id):
        data = self.store.read()
        scoped = data.get("profile_maintenance", {}).get(profile_id)
        return (scoped if scoped and scoped.get("state") != "released"
                else data.get("update_maintenance"))

    def prioritize_launch(self, profile_id):
        self._launch_queue.prefer(identifier(profile_id))

    def _launch_exclusive(self):
        required = getattr(self.instances, 'launch_requires_exclusive', None)
        return bool(required is not None and required())

    def _acquire_launch_admission_lock(self, profile_id=None):
        # Different profiles prepare, spawn and publish together; the same
        # profile, maintenance (profile_id None) and a pending one-time
        # migration still run alone. Queue this service's requests before
        # starting the OS-lock timeout, so a slow launch cannot turn other
        # warmup workers into spurious launch failures.
        if self._launch_queue.holding():
            # Nested requests use launch_admission(); a second hold here would
            # wait for this worker's own launch, as the separate OS handle did.
            raise UpdateError('profile_launch_busy',
                              '다른 프로필을 여는 작업이 아직 진행 중입니다. 잠시 후 다시 시도해 주세요.')
        profile_id = None if profile_id is None else identifier(profile_id)
        exclusive = profile_id is None or self._launch_exclusive()
        self._launch_queue.acquire(profile_id, exclusive=exclusive)
        try:
            with self._external_lock:
                if not self._external_users:
                    self._external = self._acquire_external_launch_lock()
                self._external_users += 1
                return self._external
        except BaseException:
            self._launch_queue.release()
            raise

    def _release_launch_admission_lock(self, lock):
        try:
            with self._external_lock:
                self._external_users -= 1
                if not self._external_users:
                    self._external = None
                    # Released before the queue: maintenance admitted next
                    # takes the cross-process lock itself.
                    _unlock_file(lock)
        finally:
            self._launch_queue.release()

    def _acquire_external_launch_lock(self):
        """Let an ordinary launch drain before checking maintenance ownership."""
        deadline = self.clock() + _LAUNCH_ADMISSION_WAIT
        while True:
            try:
                return _lock_file(self.directory / 'launch-admission.lock')
            except UpdateError as error:
                if error.code != 'update_in_progress':
                    raise
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise UpdateError(
                        'profile_launch_busy',
                        '다른 프로필을 여는 작업이 아직 진행 중입니다. 잠시 후 다시 시도해 주세요.',
                    ) from None
                self.sleep(min(_LAUNCH_ADMISSION_POLL, remaining))

    def open_local_for_remote_reconcile(self, profile_id):
        """Open the desktop without network I/O, fencing only its SSH cohort."""
        return self.begin_remote_reconcile(profile_id, ensure_local=True)

    def begin_remote_reconcile(self, profile_id, *, ensure_local=False, force_runtime_update=False,
                               transaction_id=None, graceful_drain=False, stop_only=False):
        """Freeze SSH enrollment atomically; an update never opens/closes local work."""
        requested_transaction_id = identifier(transaction_id) if transaction_id else None
        profile_id = identifier(profile_id)
        from .launch_metrics import LaunchMetrics
        with LaunchMetrics(self.store.directory).phase(profile_id, 'ssh_launch_admission_wait'):
            lock = self._acquire_launch_admission_lock(profile_id)
        self._admission.depth = 1
        try:
            observed_profile = self._profile(profile_id)
            observed_data = self.store.read()
            global_gate = observed_data.get('update_maintenance')
            if global_gate and global_gate.get('state') != 'released':
                raise UpdateError('update_maintenance', '전체 업데이트가 끝나면 프로필을 열 수 있습니다.')
            prior = observed_data.get('profile_maintenance', {}).get(profile_id)
            observed_ssh_gate = observed_data.get('ssh_maintenance', {}).get(profile_id)
            old_lease = None
            if prior and prior.get('state') != 'released':
                if not ensure_local:
                    raise UpdateError('profile_maintenance', 'Wait for the profile settings operation to finish.')
                old_lease = _read_json(self._lease_path(prior['transaction_id']))
                entries = old_lease.get('profiles', [])
                if (old_lease.get('profile_scope') != [profile_id] or len(entries) != 1
                        or entries[0].get('profile_id') != profile_id):
                    raise UpdateError('restart_scope_changed', '이전 설정 적용의 계정 범위를 확인해야 합니다.')
                previous = entries[0]
                identities = list(previous.get('identities', {}).values())
                if previous.get('process'):
                    identities.append({'pid': previous['process']['process_id'],
                                       'created': previous['process']['process_created']})
                # Service-backed process identity may wait for an RPC. Keep
                # that preflight outside state.lock so other profiles can read
                # state or enroll SSH, then validate its evidence below.
                if (self._live(observed_profile) is not None
                        or (not previous.get('remote_only') and not identities)
                        or any(self._endpoint_alive(item) for item in identities
                               if isinstance(item, dict) and 'pid' in item)):
                    raise UpdateError('previous_runtime_alive', '이전 로컬 실행의 종료를 먼저 확인해야 합니다.')
            # SSH enrollment no longer waits for the desktop launch lock.
            # Snapshot its hosts and publish the gate in one state transaction:
            # an operation that wins first is recorded; a later one is rejected.
            # Release this short lock before preparing/launching the desktop.
            with self.store.locked():
                profile = self._profile(profile_id)
                data = self.store.read()
                global_gate = data.get('update_maintenance')
                if global_gate and global_gate.get('state') != 'released':
                    raise UpdateError('update_maintenance', '전체 업데이트가 끝나면 프로필을 열 수 있습니다.')
                if (data.get('profile_maintenance', {}).get(profile_id) != prior
                        or data.get('ssh_maintenance', {}).get(profile_id) != observed_ssh_gate):
                    raise UpdateError('restoration_changed', '이전 설정 적용 기록이 변경되었습니다.')
                if old_lease:
                    lifetime = ('generation', 'process_id', 'process_created', 'executable_path',
                                'removed_at', 'home', 'ui_home')
                    if (any(profile.get(key) != observed_profile.get(key) for key in lifetime)
                            or _read_json(self._lease_path(prior['transaction_id'])) != old_lease):
                        raise UpdateError('restoration_changed', '이전 설정 적용 기록이 변경되었습니다.')
                transaction_id = requested_transaction_id or str(uuid4())
                lease = None
                existing = data.get('ssh_maintenance', {}).get(profile_id)
                if existing and existing.get('state') != 'released':
                    if existing.get('remote_update'):
                        raise UpdateError('ssh_update_in_progress', 'SSH 업데이트가 진행 중입니다. 로컬 작업은 계속할 수 있습니다.')
                    if not ensure_local and not graceful_drain:
                        raise UpdateError('ssh_maintenance', 'Another SSH operation owns this profile.')
                    transaction_id = identifier(existing['transaction_id'])
                    saved = _read_json(self._lease_path(transaction_id))
                    if not saved.get('ssh_only') or saved.get('profile_scope') != [profile_id]:
                        raise UpdateError('restart_scope_changed', 'SSH 준비 기록의 계정 범위가 다릅니다.')
                    # A journal another lifecycle path retired stays retired: a
                    # reopen rebuilds this generation's cohort from the saved
                    # bindings instead of trusting revoked records as evidence.
                    if saved.get('state') == 'held':
                        lease = saved
                if lease is None:
                    records = copy.deepcopy(old_lease['profiles'][0].get('remotes', [])) if old_lease else []
                    for record in records:
                        record['reinspect'] = True
                    if not records:
                        from .ssh_shim import validate_binding
                        bindings = {b['alias']: validate_binding(b, profile_id)
                                    for b in profile.get('remote_bindings', []) if b.get('prepared') is True}
                        path = self.store.directory / 'profiles' / profile_id / 'ssh-bindings.json'
                        if path.is_file():
                            manifest = _read_json(path, 256000)
                            if manifest.get('profile_id') == profile_id:
                                bindings.update({b['alias']: validate_binding(b, profile_id)
                                                 for b in manifest.get('bindings', [])})
                        hosts = data.get('ssh_inventory', {}).get(profile_id, {}).get('hosts', [])
                        if graceful_drain:
                            hosts = sorted(set(hosts) | set(bindings))
                        records = [dict(binding=bindings.get(alias), alias=alias, state='unobserved')
                                   for alias in sorted(set(hosts))]
                    lease = dict(transaction_id=transaction_id, ssh_only=True, state='held',
                                 profile_scope=[profile_id], target_revision=profile['policy']['desired_revision'],
                                 profiles=[dict(profile_id=profile_id, generation=profile.get('generation'),
                                                remote_only=True, state='held', remotes=records)])
                    if force_runtime_update:
                        lease['force_runtime_update'] = True
                        # Keep the full previous sources even after preparation
                        # saves a new binding. A failed start is never published.
                        lease['previous_bindings'] = copy.deepcopy(profile.get('remote_bindings', []))
                if graceful_drain:
                    # Explicit user action only; ordinary opens never signal.
                    lease['graceful_drain'] = True
                    lease['stop_only'] = bool(stop_only)
                # Retry remote evidence with the current helper, never an old saved
                # next_binding start. A fresh inspect proves exit or the actual PID.
                for record in lease['profiles'][0].get('remotes', []):
                    if record.get('state') in ('prepared', 'start_requested', 'stop_requested', 'started'):
                        record['reinspect'] = True
                lease['target_revision'] = profile['policy']['desired_revision']
                self._save_lease(lease)
                def hold(current):
                    current.setdefault('ssh_maintenance', {})[profile_id] = dict(
                        state='held', transaction_id=transaction_id, generation=profile.get('generation'),
                        remote_update=lease.get('force_runtime_update', False),
                        target_revision=profile['policy']['desired_revision'], updated_at=now())
                    if old_lease:
                        gate = current.get('profile_maintenance', {}).get(profile_id)
                        if not gate or gate.get('transaction_id') != prior['transaction_id']:
                            raise UpdateError('restoration_changed', '이전 설정 적용 기록이 변경되었습니다.')
                        gate.update(state='released', adopted_by=transaction_id, updated_at=now())
                self.store.mutate(hold)
            self._restoration.ssh_transaction_id = transaction_id
            shown = (self.instances.show(profile_id, reopen_existing=False, wait_for_window=False)
                     if ensure_local else None)
            current = self._profile(profile_id)
            def launched(state):
                gate = state['ssh_maintenance'][profile_id]
                if gate['transaction_id'] != transaction_id:
                    raise UpdateError('restoration_changed', 'SSH 준비 요청이 변경되었습니다.')
                gate['generation'] = current.get('generation')
            self.store.mutate(launched)
            lease['profiles'][0]['generation'] = current.get('generation')
            if lease['profiles'][0].get('remotes'):
                from .ssh_shim import validate_binding
                manifest = _read_json(self.store.directory / 'profiles' / profile_id / 'ssh-bindings.json', 256000)
                if (manifest.get('profile_id') != profile_id
                        or manifest.get('generation') != current.get('generation')):
                    raise UpdateError('remote_generation_changed', '로컬 창의 SSH 연결 설정을 확인해야 합니다.')
                published = {b['alias']: validate_binding(b, profile_id) for b in manifest.get('bindings', [])}
                # Environment creation may have installed a previously prepared
                # binding. Pin that publication baseline independently of the
                # original runtime identity retained in binding/active_binding.
                for record in lease['profiles'][0]['remotes']:
                    record.pop('publication_binding', None)
                    alias = (record.get('binding') or {}).get('alias', record.get('alias'))
                    if alias in published:
                        record['publication_binding'] = published[alias]
            self._save_lease(lease)
        except (RuntimeError, ValueError, OSError, KeyError):
            if getattr(self._restoration, 'ssh_transaction_id', None):
                self.remote_open_failed(profile_id, transaction_id, 'local_launch_failed')
            raise
        finally:
            self._restoration.ssh_transaction_id = None
            self._admission.depth = 0
            self._release_launch_admission_lock(lock)
        # The SSH lease now refers to the published process generation. Waiting
        # for Chromium's first window is read-only and must not hold the global
        # launch/update fence, or every background profile starts in sequence.
        try:
            return (self.instances.finish_show(shown), transaction_id) if ensure_local else transaction_id
        except (RuntimeError, ValueError, OSError, KeyError):
            self.remote_open_failed(profile_id, transaction_id, 'local_launch_failed')
            raise

    def reconcile_opened_remotes(self, profile_id, transaction_id, *, target_guard=None):
        try:
            return self._reconcile_opened_remotes(profile_id, transaction_id, target_guard=target_guard)
        except UpdateError as error:
            from .ssh_deferred_settings import DEFERRED_MESSAGE, UNSUPPORTED_IDLE, resume_previous
            if error.code not in UNSUPPORTED_IDLE or self.remote_maintenance is None:
                raise
            lease = _read_json(self._lease_path(transaction_id))
            try:
                resumed = resume_previous(self.store, self.remote_maintenance, self._profile(profile_id), lease,
                                          error.code, journal_path=self._lease_path(transaction_id))
            except (RuntimeError, ValueError, OSError, KeyError):
                # An unavailable/changed old listener cannot be restored. Keep
                # the original typed failure and fence instead of reclassifying
                # it as busy or claiming the desired settings were applied.
                raise error from None
            if resumed:
                raise UpdateError('ssh_settings_deferred', DEFERRED_MESSAGE) from None
            raise

    def _reconcile_opened_remotes(self, profile_id, transaction_id, *, target_guard=None):
        """Advance only SSH; the newly opened local process is never closed."""
        lease = _read_json(self._lease_path(transaction_id))
        if not lease.get('ssh_only') or lease.get('profile_scope') != [profile_id]:
            raise UpdateError('restart_scope_changed', 'SSH 준비 범위가 다릅니다.')
        entry = lease['profiles'][0]
        if lease.get('state') != 'held' or entry.get('state') != 'held':
            # A journal another lifecycle path retired is never evidence for a
            # new adoption: a fresh hold must rebuild this generation's cohort
            # from the saved bindings before anything is released as reused.
            raise UpdateError('ssh_generation_changed', 'SSH 적용 기록이 이미 종료되어 다시 준비해야 합니다.')
        def current():
            profile = self._profile(profile_id)
            gate = self.store.read().get('ssh_maintenance', {}).get(profile_id, {})
            if (gate.get('transaction_id') != transaction_id or gate.get('state') != 'held'
                    or profile.get('removed_at') or profile.get('view_only')
                    or profile.get('generation') != entry['generation']
                    or gate.get('generation') != entry['generation']
                    or profile['policy']['desired_revision'] != lease['target_revision']):
                raise UpdateError('ssh_generation_changed', '프로필 실행 또는 설정이 변경되어 SSH 적용을 중지했습니다.')
            self.guard_launch(profile_id)
            if target_guard is not None:
                target_guard()
            return profile
        profile = current()
        if self.remote_maintenance is None:
            raise UpdateError('remote_binding_unknown', 'SSH 준비 구성요소를 확인해야 합니다.')
        records = entry.get('remotes', [])
        if lease.get('force_runtime_update'):
            coverage = self.host_inventory(profile) if self.host_inventory else {}
            if (coverage.get('maintenance_complete', coverage.get('complete')) is not True
                    or coverage.get('generation') != entry['generation']
                    or set(coverage.get('hosts', [])[1:]) != {r.get('alias') for r in records}):
                return False
        reusable = getattr(self.remote_maintenance, 'reuse_unchanged', None)
        reused = (not lease.get('graceful_drain') and not lease.get('force_runtime_update') and callable(reusable)
                  and reusable(current(), records) is True)
        adopted = None
        if not reused and not lease.get('graceful_drain') and not lease.get('force_runtime_update'):
            # An unchanged reconnect keeps a live listener even when that
            # listener publishes no idle inventory. Only a verified settings
            # change may enter the exit-verified lifecycle below.
            equivalent = getattr(self.remote_maintenance, 'reuse_equivalent', None)
            if callable(equivalent):
                adopted = equivalent(current(), records)
                reused = adopted is not None
        for record in ([] if reused else records):
            current()
            if record.get('state') == 'unobserved' or record.get('reinspect'):
                binding = record.get('next_binding') or record.get('active_binding') or record.get('binding')
                if not binding:
                    raise UpdateError('remote_binding_unknown', '저장된 SSH 연결 설정을 확인해야 합니다.')
                inspect_options = dict(discover_active=True)
                if lease.get('graceful_drain'):
                    inspect_options['observe_only'] = True
                observed = self.remote_maintenance.request(binding, 'inspect', **inspect_options)
                record.update(process=observed['process'], idle=observed['idle'], exited=observed['exited'],
                              active_binding=observed.get('active_binding', binding),
                              state='observed' if observed['process'] is not None else 'closed')
                record.pop('reinspect', None)
                record.pop('next_binding', None)
                record.pop('target_policy_revision', None)
                self._save_lease(lease)
                current()
            if lease.get('graceful_drain') and record.get('state') in ('observed', 'drain_requested'):
                # Write ahead; recovery always targets the same process birth.
                record['state'] = 'drain_requested'
                self._save_lease(lease)
                current()
                proof = self.remote_maintenance.request(record.get('active_binding', record['binding']),
                    'drain', expected_process=record['process'])
                current()
                if proof['exited'] is not True or proof['idle'] is not True:
                    return False
                record.update(state='closed', exit_proof=proof)
                self._save_lease(lease)
            elif record.get('state') == 'observed':
                observed = self.remote_maintenance.request(record.get('active_binding', record['binding']), 'inspect')
                if observed['process'] != record['process']:
                    raise UpdateError('remote_process_changed', 'SSH 실행 식별자가 변경되어 종료하지 않았습니다.')
                if not observed['idle']:
                    return False
            elif record.get('state') in ('stop_requested', 'start_requested'):
                self.remote_maintenance.reconcile(record)
                self._save_lease(lease)
                if record['state'] in ('stop_requested', 'start_requested'):
                    return False
        current()
        if not reused:
            # A resumed start has already crossed the exit boundary. Never
            # replay stop or discard its write-ahead start journal.
            pending_close = {**entry, 'remotes': [r for r in records if r.get('state') in ('observed', 'closed')]}
            self._close_remotes(lease, pending_close, lifecycle_guard=current)
            profile = current()
            if not lease.get('stop_only'):
                self.remote_maintenance.prepare_and_start(profile, records, lambda: self._save_lease(lease),
                                                          lifecycle_guard=current)
                profile = current()
                self.remote_maintenance.publish_started(profile, records)
        def release(data):
            gate = data['ssh_maintenance'][profile_id]
            profile = self.store.profile(profile_id, data)
            if (gate.get('transaction_id') != transaction_id or gate.get('generation') != entry['generation']
                    or profile.get('generation') != entry['generation']
                    or profile['policy']['desired_revision'] != lease['target_revision']):
                raise UpdateError('ssh_generation_changed', 'SSH 준비 중 프로필 설정이 변경되었습니다.')
            gate.update(state='released', updated_at=now())
        self.store.mutate(release)
        if reused:
            lease['reused_unchanged'] = True
            if adopted:
                lease['reused_revisions'] = adopted
        lease['state'] = 'released'
        self._save_lease(lease)
        return True

    def remote_open_failed(self, profile_id, transaction_id, code):
        def attention(data):
            gate = data.get('ssh_maintenance', {}).get(profile_id)
            if gate and gate.get('transaction_id') == transaction_id:
                gate.update(state='attention', code=code,
                            message='로컬 창은 사용할 수 있습니다. SSH 연결 준비 상태를 확인해 주세요.', updated_at=now())
        self.store.mutate(attention)

    @contextmanager
    def launch_admission(self, profile_id):
        """Fence launch preparation, process creation and identity publication.

        Other profiles' launches share this fence; updates and maintenance
        wait for all of them and then exclude new launches (guard_launch).
        """
        if getattr(self._admission, "depth", 0):
            # The outer admission (restore, SSH, conversation) holds the update
            # fence. Still wait for another worker opening this profile.
            self._launch_queue.acquire(identifier(profile_id), exclusive=False)
            try:
                self.guard_launch(profile_id)
                yield
            finally:
                self._launch_queue.release()
            return
        lock = self._acquire_launch_admission_lock(profile_id)
        try:
            self.guard_launch(profile_id)
            self._admission.depth = 1
            yield
        finally:
            self._admission.depth = 0
            self._release_launch_admission_lock(lock)

    def _host_coverage(self, profile):
        if not self.host_inventory:
            return False
        coverage = self.host_inventory(copy.deepcopy(profile))
        if self.remote_maintenance is not None:
            try:
                self.remote_maintenance.bindings(profile, coverage)
                return True
            except (RuntimeError, ValueError, OSError, KeyError, TypeError):
                return False
        return bool(isinstance(coverage, dict) and coverage.get("complete") is True
                    and coverage.get("generation") == profile.get("generation")
                    and coverage.get("hosts") == ["local"])

    def _profile(self, profile_id):
        profile = self.store.profile(identifier(profile_id))
        self.instances.paths(profile)  # Enforce manager-owned home/UI directories.
        return profile

    def _live(self, profile):
        observed = self.instances.observe(profile)
        if observed.get("status") != "running":
            return None
        current = self.identity(observed.get("process_id"))
        if not _same_process(current, observed) or not _same_process(current, profile):
            raise UpdateError("process_identity_changed", "프로필의 실행 프로세스를 확인하지 못했습니다.")
        return {**profile, **observed, "profile_id": profile["id"],
                "created_at": _cim_birth(current["process_created"]),
                "executable": current["executable_path"]}

    def _observer_ready(self, profile, observed):
        value = observed.get("runtime_state", {})
        try:
            age = time.time() - datetime.fromisoformat(value["observed_at"]).timestamp()
        except (KeyError, ValueError, TypeError):
            return False
        if (not -5 <= age <= 15 or value.get("profile_id") != profile["id"]
                or value.get("generation") != profile.get("generation")
                or value.get("connected") is not True or value.get("initialized") is not True
                or value.get("stream_complete") is not True):
            return False
        binding = value.get("auth_binding", {})
        if profile.get("auth_mode") == "native":
            # A directly signed-in profile uses its own Codex authentication.
            # Its credential proxy is deliberately disabled even if llm-usage
            # has registered a source/account entry for displaying usage.
            return binding.get("bound") is False and binding.get("state") == "disabled"
        if profile.get("source_home") or profile.get("usage_account_id"):
            expected = profile.get("account_fingerprint")
            if (not expected or binding.get("bound") is not True or binding.get("state") != "ready"
                    or binding.get("account_fingerprint") != expected):
                return False
        return True

    @staticmethod
    def _health_busy(value, generation):
        return bool(isinstance(value, dict) and value.get('generation') == generation
                    and all(value.get(k) is True for k in
                            ('connected', 'initialized', 'streamComplete', 'accountReady'))
                    and any(type(value.get(k)) is int and value[k] > 0 for k in
                            ('pendingMutationCount', 'pendingApprovalCount', 'activeProcessCount',
                             'activeToolCount', 'activeTurnCount', 'activeChildCount')))

    @staticmethod
    def _health(value, generation, *, require_held=False):
        return bool(isinstance(value, dict) and value.get("generation") == generation
                    and all(value.get(k) is True for k in
                            ("connected", "initialized", "streamComplete", "accountReady"))
                    and value.get("pendingMutationCount") == 0
                    and value.get("pendingApprovalCount") == 0
                    and value.get("activeProcessCount") == 0
                    and value.get("activeToolCount") == 0
                    and value.get("activeTurnCount") == 0
                    and value.get("activeChildCount") == 0
                    and (not require_held or
                         (value.get("held") is True and value.get("frontendMutationBlocked") is True)))

    def _loaded(self, admin):
        ids, cursor, seen = [], None, set()
        for _ in range(128):
            result = admin.request("thread/loaded/list", {"limit": 256, "cursor": cursor})
            data = result.get("data")
            if not isinstance(data, list):
                raise UpdateError("runtime_inventory_unknown", "로드된 전체 작업 목록을 확인하지 못했습니다.")
            for item in data:
                item = identifier(item)
                if item in ids:
                    raise UpdateError("runtime_inventory_changed", "전체 작업 목록이 확인 중 변경되었습니다.")
                ids.append(item)
            cursor = result.get("nextCursor")
            if cursor is None:
                return ids
            if not isinstance(cursor, str) or cursor in seen:
                raise UpdateError("runtime_inventory_unknown", "전체 작업 목록의 페이지를 확인할 수 없습니다.")
            seen.add(cursor)
        raise UpdateError("runtime_inventory_limit", "로드된 작업이 너무 많아 업데이트를 대기합니다.")

    def _roots(self, admin, ids):
        parents = {}
        for thread_id in ids:
            response = admin.request("thread/read", {"threadId": thread_id, "includeTurns": False})
            thread = response.get("thread", {})
            if thread.get("id") != thread_id or "parentThreadId" not in thread:
                raise UpdateError("runtime_parentage_unknown", "작업과 자식의 연결 관계를 확인하지 못했습니다.")
            parent = thread["parentThreadId"]
            if parent is not None:
                parent = identifier(parent)
            parents[thread_id] = parent
        for thread_id in ids:
            visited, node = set(), thread_id
            while node in parents:
                if node in visited:
                    raise UpdateError("runtime_parentage_invalid", "작업과 자식의 연결 관계가 올바르지 않습니다.")
                visited.add(node)
                node = parents[node]
        return [thread_id for thread_id in ids if parents[thread_id] not in parents]

    def _idle_scope(self, admin):
        """Strict runtime read-only snapshot; never claims writer release."""
        loaded = self._loaded(admin)
        roots = self._roots(admin, loaded)
        covered = set()
        for thread_id in roots:
            result = admin.request("thread/managedIdleStatus", {"threadId": thread_id})
            # Exact adapter schema is intentionally strict; absent/older RPC fails closed.
            scope = result.get("observedThreadIds")
            if (result.get("threadId") != thread_id or result.get("idle") is not True
                    or result.get("proofScope") != "advisory" or result.get("hostId") != "local"
                    or not isinstance(result.get("sourceStoreId"), str) or not result["sourceStoreId"]
                    or not isinstance(scope, list)
                    or thread_id not in scope or result.get("blockers") != []):
                return None
            covered.update(identifier(item) for item in scope)
        if not set(loaded).issubset(covered) or set(self._loaded(admin)) != set(loaded):
            return None
        return {"thread_ids": loaded, "roots": roots, "scope_verified": True,
                "writer_release_verified": False}

    def snapshot_instances(self, profile_ids=None):
        result = []
        selected = None if profile_ids is None else {identifier(value) for value in profile_ids}
        for profile in self.store.read()["profiles"]:
            if selected is not None and profile["id"] not in selected:
                continue
            try:
                live = self._live(profile)
                if live is None:
                    item = {**profile, "profile_id": profile["id"], "process_id": None,
                            "job_state": "not_running", "idle_verified": False}
                    coverage = self.host_inventory(profile) if self.host_inventory else {}
                    if len(coverage.get('hosts', [])) > 1:
                        item['remote_maintenance_required'] = True
                        item['remote_only'] = True
                        if self.remote_maintenance is not None:
                            remotes = self.remote_maintenance.snapshot(profile, coverage)
                            item.update(remote_states=remotes, idle_verified=all(r['idle'] for r in remotes),
                                        job_state='idle' if all(r['idle'] for r in remotes) else 'active')
                    result.append(item)
                    continue
                live.update(job_state="unknown", idle_verified=False)
                if not self._host_coverage(profile):
                    live["update_blocker"] = "remote_runtime_coverage_unverified"
                elif not self._observer_ready(profile, live):
                    live["update_blocker"] = "runtime_identity_not_ready"
                else:
                    admin = self.admin_factory(profile)
                    health = admin.request("manager/maintenance/status", {})
                    if not self._health(health, profile["generation"]):
                        live["update_blocker"] = ("runtime_not_idle" if self._health_busy(health, profile["generation"])
                                                  else "runtime_admin_not_ready")
                    else:
                        scope = self._idle_scope(admin)
                        if scope is not None:
                            live.update(job_state="idle", idle_verified=True,
                                        idle_evidence="runtime_managed_idle_advisory",
                                        writer_release_verified=False, loaded_thread_ids=scope["thread_ids"])
                            if self.remote_maintenance is not None:
                                remotes = self.remote_maintenance.snapshot(profile, self.host_inventory(profile))
                                live['remote_states'] = remotes
                                if any(not remote['idle'] for remote in remotes):
                                    live.update(job_state='active', idle_verified=False, update_blocker='runtime_not_idle')
                        else:
                            live.update(job_state="active", update_blocker="runtime_not_idle")
                result.append(live)
            except (RuntimeError, ValueError, KeyError, OSError):
                result.append({**profile, "profile_id": profile["id"],
                               "job_state": "unknown", "idle_verified": False,
                               "remote_maintenance_required": True,
                               "remote_only": not profile.get('process_id'),
                               "update_blocker": "runtime_proof_unavailable"})
        return result

    def compatibility_fingerprint(self):
        runtime = self.runtime_resolver(self.root)
        caps = runtime.get("capabilities", {})
        if any(caps.get(k) is not True for k in ("managed_close_idle", "managed_idle_status")):
            raise UpdateError("runtime_capability_unverified", "검증된 작업 종료 런타임 배포가 필요합니다.")
        release = _read_json(self.root / "artifacts/manager/current.json")
        proxy = Path(release["runtime_proxy"]).resolve(strict=True)
        if not proxy.is_relative_to(self.root / "artifacts/manager/releases"):
            raise UpdateError("runtime_proxy_unverified", "배포된 관리 실행기를 확인하지 못했습니다.")
        runtime_hash = _hash(runtime["runtime"])
        if runtime_hash != runtime.get("sha256"):
            raise UpdateError("runtime_hash_changed", "런타임 배포 파일이 변경되었습니다.")
        return {
            "runtime_sha256": runtime_hash, "runtime_proxy_sha256": _hash(proxy),
            "runtime_admin_sha256": _hash(script_path(self.root, 'scripts/manager_core/runtime_admin.py')),
            "runtime_proxy_module_sha256": _hash(script_path(self.root, 'scripts/manager_core/runtime_proxy.py')),
        }

    def verify_compatibility(self, package):
        try:
            version_tuple(package["version"])
            fingerprint = self.compatibility_fingerprint()
            document = _read_json(self.root / "artifacts/manager-runtime/update-compatibility.json")
            if document.get("version") != 1:
                raise ValueError()
            for entry in document.get("entries", []):
                if (entry.get("app_version") != package["version"] or entry.get("status") != "verified"
                        or any(entry.get(k) != v for k, v in fingerprint.items())
                        or any(entry.get("checks", {}).get(k) is not True for k in _REQUIRED_CHECKS)):
                    continue
                evidence = (self.root / entry["evidence_path"]).resolve(strict=True)
                if not evidence.is_relative_to(self.root / "work"):
                    continue
                report = _read_json(evidence)
                if (report.get("verified") is not True or report.get("app_version") != package["version"]
                        or any(report.get(k) != v for k, v in fingerprint.items())):
                    continue
                return {"compatible": True, "app_version": package["version"], **fingerprint,
                        "evidence_path": str(evidence), "message": "검증된 앱과 관리 런타임 조합입니다."}
        except (RuntimeError, ValueError, OSError, KeyError, TypeError):
            pass
        return {"compatible": False, "message": "이 앱 버전과 현재 패치 런타임의 검증된 조합 기록이 필요합니다."}

    def _lease_path(self, transaction_id):
        return self.directory / (identifier(transaction_id) + ".json")

    def _save_lease(self, lease):
        atomic_json(self._lease_path(lease["transaction_id"]), lease)

    def _begin_global(self, transaction_id, profile_scope=None):
        def begin(data):
            ssh_gates = data.get('ssh_maintenance', {})
            candidates = ssh_gates.values() if profile_scope is None else (ssh_gates.get(key) for key in profile_scope)
            if any(gate and gate.get('state') != 'released' for gate in candidates):
                raise UpdateError('profile_maintenance', 'SSH 연결 준비가 끝난 뒤 전체 설정을 적용할 수 있습니다.')
            previous = data.get("update_maintenance")
            if previous and previous.get("state") != "released" and previous.get("transaction_id") != transaction_id:
                raise UpdateError("update_in_progress", "다른 업데이트가 프로필 시작을 관리하고 있습니다.")
            scoped = data.setdefault("profile_maintenance", {})
            conflicts = scoped.values() if profile_scope is None else (scoped.get(key) for key in profile_scope)
            if any(item and item.get("state") != "released" and item.get("transaction_id") != transaction_id
                   for item in conflicts):
                raise UpdateError("profile_maintenance", "설정을 적용 중인 프로필이 있습니다.")
            value = {"transaction_id": transaction_id, "state": "held", "updated_at": now()}
            if profile_scope is None:
                data["update_maintenance"] = value
            else:
                for profile_id in profile_scope:
                    self.store.profile(profile_id, data)
                    scoped[profile_id] = value.copy()
        lock = self._acquire_launch_admission_lock()
        try:
            self.store.mutate(begin)
        finally:
            self._release_launch_admission_lock(lock)

    def acquire_maintenance(self, instances, *, transaction_id, profile_scope=None):
        transaction_id = identifier(transaction_id)
        if profile_scope is not None:
            profile_scope = sorted({identifier(value) for value in profile_scope})
            if not profile_scope or {item.get("profile_id", item.get("id")) for item in instances} != set(profile_scope):
                raise UpdateError("invalid_maintenance_scope", "재시작할 프로필 범위가 일치하지 않습니다.")
        with self._mutex:
            self._begin_global(transaction_id, profile_scope)
            lease = {"transaction_id": transaction_id, "verified": False, "profiles": [],
                     "state": "acquiring", "created_at": now(), "profile_scope": profile_scope}
            self._save_lease(lease)
            try:
                expected_ids = {i.get("profile_id", i.get("id")) for i in instances if i.get("process_id")}
                actual_ids = {p["id"] for p in self.store.read()["profiles"]
                              if (profile_scope is None or p["id"] in profile_scope) and self._live(p)}
                if expected_ids != actual_ids:
                    raise UpdateError("instances_changed", "프로필 실행 목록이 변경되어 업데이트를 대기합니다.")
                for expected in instances:
                    if not expected.get("process_id"):
                        if expected.get('remote_maintenance_required'):
                            profile = self._profile(expected.get('profile_id', expected.get('id')))
                            if self.remote_maintenance is None or not self._host_coverage(profile):
                                raise UpdateError('remote_coverage_unknown', 'SSH 실행 상태 확인을 기다리고 있습니다.')
                            remotes = self.remote_maintenance.snapshot(profile, self.host_inventory(profile))
                            if any(not remote['idle'] for remote in remotes):
                                raise UpdateError('runtime_not_idle', 'SSH 작업이 끝나면 설정을 적용합니다.')
                            lease['profiles'].append(dict(profile_id=profile['id'], generation=profile['generation'],
                                process=None, identities={}, state='held', remote_only=True,
                                remotes=[dict(remote, state='observed') for remote in remotes]))
                            self._save_lease(lease)
                        continue
                    profile = self._profile(expected.get("profile_id", expected.get("id")))
                    current = self._live(profile)
                    if (not _same_process(current, expected) or profile.get("generation") != expected.get("generation")
                            or not self._host_coverage(profile) or not self._observer_ready(profile, current)):
                        raise UpdateError("instance_changed", "프로필 상태가 변경되어 업데이트를 대기합니다.")
                    admin = self.admin_factory(profile)
                    identities = admin.identities()
                    if (not isinstance(identities, dict) or set(identities) != {"generation", "proxy", "runtime"}
                            or identities.get("generation") != profile["generation"]
                            or any(not isinstance(identities.get(name), dict)
                                   or not {"pid", "created"}.issubset(identities[name])
                                   or type(identities[name].get("pid")) is not int or identities[name]["pid"] <= 0
                                   or type(identities[name].get("created")) is not int or identities[name]["created"] <= 0
                                   for name in ("proxy", "runtime"))
                            or identities["runtime"]["pid"] != current["runtime_state"].get("runtime_process_id")):
                        raise UpdateError("runtime_identity_changed", "관리 런타임 실행 정보가 변경되었습니다.")
                    # AdminClient adds the verified executable path for diagnostics.
                    # Pin only the lifetime fields used by maintenance; optional
                    # metadata must not turn a valid process into a mismatch.
                    identities = {"generation": identities["generation"], **{
                        name: {key: identities[name][key] for key in ("pid", "created")}
                        for name in ("proxy", "runtime")}}
                    entry = {"profile_id": profile["id"], "generation": profile["generation"],
                             "process": {k: current[k] for k in ("process_id", "process_created", "executable_path")},
                             "identities": identities, "state": "acquire_requested"}
                    lease["profiles"].append(entry)
                    self._save_lease(lease)
                    try:
                        response = admin.request("manager/maintenance/acquire", {"transactionId": transaction_id})
                    except AdminError as error:
                        if not error.uncertain:
                            entry["state"] = "rejected"
                            self._save_lease(lease)
                        raise
                    if (response.get("held") is not True or response.get("generation") != profile["generation"]
                            or response.get("transactionId") != transaction_id):
                        raise UpdateError("maintenance_unverified", "새 작업 시작을 멈춘 상태를 확인하지 못했습니다.")
                    entry["lease_token"] = identifier(response["leaseToken"])
                    entry["state"] = "held"
                    self._save_lease(lease)
                    if not self._health(response, profile["generation"], require_held=True):
                        raise UpdateError("work_started", "확인 중 시작된 작업이 끝나면 업데이트할 수 있습니다.")
                    scope = self._idle_scope(admin)
                    if scope is None:
                        raise UpdateError("work_started", "확인 중 시작된 작업이 끝나면 업데이트할 수 있습니다.")
                    if self.remote_maintenance is not None:
                        coverage = self.host_inventory(profile)
                        remotes = self.remote_maintenance.snapshot(profile, coverage)
                        if any(not remote['idle'] for remote in remotes):
                            raise UpdateError('runtime_not_idle', 'SSH 작업이 끝나면 설정을 적용합니다.')
                        connected = {op.get('alias') for op in coverage.get('operations', [])
                                     if op.get('operation') == 'native-proxy'}
                        entry['remotes'] = [dict(remote, state='observed',
                            reconnect_required=remote['binding']['alias'] in connected) for remote in remotes]
                        self._save_lease(lease)
                lease.update(verified=True, state="held")
                self._save_lease(lease)
                return copy.deepcopy(lease)
            except Exception:
                lease["state"] = "acquire_failed"
                self._save_lease(lease)
                # Only known acquired leases are released. An uncertain acquire
                # stays recorded for explicit reconciliation, never guessed away.
                try:
                    self.release_maintenance(lease)
                except Exception:
                    pass
                raise

    def _lease_for_profile(self, profile_id):
        maintenance = self._maintenance_for(profile_id) or {}
        if maintenance.get("state") != "held":
            raise UpdateError("maintenance_missing", "업데이트 작업 잠금을 찾을 수 없습니다.")
        lease = _read_json(self._lease_path(maintenance["transaction_id"]))
        entry = next((p for p in lease.get("profiles", []) if p["profile_id"] == profile_id), None)
        if lease.get("verified") is not True or entry is None or entry.get("state") != "held":
            raise UpdateError("maintenance_missing", "프로필의 업데이트 작업 잠금을 확인하지 못했습니다.")
        return lease, entry

    def _lease_status(self, admin, lease, entry):
        value = admin.request("manager/maintenance/status", {
            "transactionId": lease["transaction_id"], "leaseToken": entry["lease_token"]})
        if (not self._health(value, entry["generation"], require_held=True)
                or value.get("transactionId") != lease["transaction_id"]):
            raise UpdateError("maintenance_lost", "업데이트 중 프로필 작업 잠금이 변경되었습니다.")

    def _finish_idle_exit(self, profile, verify_quiescence):
        from .profile_recovery import stop_profile
        return stop_profile(self.store, self.instances, profile['id'],
                            expected_generation=profile['generation'], quiescence_check=verify_quiescence)

    def close_instance(self, expected, *, finish_idle_exit=False):
        """Only called by explicit update apply, after verified maintenance."""
        profile_id = expected.get("profile_id", expected.get("id"))
        profile = self._profile(profile_id)
        lease, entry = self._lease_for_profile(profile_id)
        current = self._live(profile)
        if entry.get('remote_only'):
            if current is not None or profile.get('generation') != entry['generation'] or not self._host_coverage(profile):
                raise UpdateError('instance_changed', '설정 적용 중 프로필 실행 상태가 변경되었습니다.')
            self._close_remotes(lease, entry)
            entry.update(state='closed', closed_verified=True)
            self._save_lease(lease)
            return True
        if (not _same_process(current, expected) or not _same_process(current, entry["process"])
                or profile.get("generation") != entry["generation"] or not self._host_coverage(profile)):
            raise UpdateError("instance_changed", "종료할 프로필의 실행 상태가 변경되었습니다.")
        admin = self.admin_factory(profile)
        self._lease_status(admin, lease, entry)
        scope = self._idle_scope(admin)
        if scope is None:
            raise UpdateError("runtime_not_idle", "진행 중인 작업을 중단하지 않고 업데이트를 대기합니다.")
        closed = set()
        for root in scope["roots"]:
            if root in closed:
                continue
            proof = admin.request("thread/managedCloseIdle", {"threadId": root}, timeout=60)
            ids = proof.get("closedThreadIds")
            if (proof.get("threadId") != root or proof.get("writerReleaseVerified") is not True
                    or not isinstance(ids, list) or root not in ids):
                raise UpdateError("writer_release_unverified", "작업 기록 저장과 종료를 확인하지 못했습니다.")
            closed.update(identifier(item) for item in ids)
        if not set(scope["thread_ids"]).issubset(closed):
            raise UpdateError("writer_release_unverified", "일부 작업의 기록 쓰기 종료를 확인하지 못했습니다.")
        if self._loaded(admin):
            raise UpdateError("runtime_inventory_changed", "새로 로드된 작업이 있어 앱을 종료하지 않았습니다.")
        self._lease_status(admin, lease, entry)
        current = self._live(self._profile(profile_id))
        if not _same_process(current, entry["process"]):
            raise UpdateError("instance_changed", "앱 종료 직전에 프로필 실행 상태가 변경되었습니다.")
        self._close_remotes(lease, entry)
        entry.update(state="native_close_requested", writer_release_verified=True)
        self._save_lease(lease)
        self.native_close(current)
        deadline = self.clock() + (min(2, self.timeout) if finish_idle_exit else self.timeout)
        while self.clock() < deadline:
            gui_alive = self._endpoint_alive({"pid": current["process_id"], "created": current["process_created"]})
            endpoints_alive = any(self._endpoint_alive(v) for v in entry["identities"].values()
                                  if isinstance(v, dict) and "pid" in v)
            if not gui_alive and not endpoints_alive:
                entry.update(state="closed", closed_verified=True)
                self._save_lease(lease)
                return True
            self.sleep(.1)
        if finish_idle_exit:
            return self._finish_requested_idle_exit(profile, lease, entry)
        # Ordinary manual update callers retain their existing close semantics.
        return False

    def _finish_requested_idle_exit(self, profile, lease, entry):
        """Finish one already requested close only while its saved proof still holds."""
        if (entry.get('state') != 'native_close_requested' or entry.get('writer_release_verified') is not True
                or entry.get('profile_id') != profile['id']
                or any(remote.get('state') != 'closed' for remote in entry.get('remotes', []))):
            raise UpdateError('writer_release_unverified', '이전 프로필의 작업 저장과 종료 확인이 필요합니다.')
        admin = self.admin_factory(profile)

        def verify_quiescence():
            selected = self._profile(profile['id'])
            gate = self._maintenance_for(profile['id'])
            if (not gate or gate.get('state') != 'held'
                    or gate.get('transaction_id') != lease['transaction_id']):
                raise UpdateError('maintenance_lost', '프로필 작업 잠금의 소유자가 변경되었습니다.')
            current = self._live(selected)
            if (selected.get('generation') != entry['generation']
                    or not _same_process(current, entry['process'])
                    or not self._observer_ready(selected, current) or not self._host_coverage(selected)):
                raise UpdateError('instance_changed', '설정 적용 중 실행 프로필이 변경되었습니다.')
            identities = admin.identities()
            if (identities.get('generation') != entry['generation'] or any(
                    {key: identities.get(kind, {}).get(key) for key in ('pid', 'created')}
                    != entry['identities'].get(kind) for kind in ('proxy', 'runtime'))):
                raise UpdateError('runtime_identity_changed', '종료를 요청한 런타임의 실행 정보가 변경되었습니다.')
            self._lease_status(admin, lease, entry)
            if self._loaded(admin):
                raise UpdateError('runtime_inventory_changed', '새 작업이 있어 프로필 종료를 중단했습니다.')
            return True

        # WM_CLOSE can leave Electron resident. Recheck the held gate, empty
        # runtime and exact process lifetimes again inside the stop operation.
        verify_quiescence()
        stopped = self.idle_stop(profile, verify_quiescence)
        if (stopped.get('state') in ('stopped', 'already_stopped')
                and not self._endpoint_alive({'pid': entry['process']['process_id'],
                                             'created': entry['process']['process_created']})
                and not any(self._endpoint_alive(value) for value in entry['identities'].values()
                            if isinstance(value, dict) and 'pid' in value)):
            entry.update(state='closed', closed_verified=True, idle_exit_finished=True)
            self._save_lease(lease)
            return True
        return False

    def _close_remotes(self, lease, entry, *, lifecycle_guard=None):
        for remote in entry.get('remotes', []):
            if lifecycle_guard is not None:
                lifecycle_guard()
            if remote.get('state') == 'closed':
                continue
            if remote.get('state') != 'observed':
                raise UpdateError('remote_shutdown_pending', '이전 SSH 종료 요청의 결과 확인이 필요합니다.')
            if remote['process'] is None:
                remote['state'] = 'closed'
            else:
                remote['state'] = 'stop_requested'
                self._save_lease(lease)
                proof = self.remote_maintenance.stop(remote)
                if proof.get('exited') is not True or proof.get('idle') is not True:
                    raise UpdateError('remote_shutdown_pending', 'SSH 서버의 종료를 확인하지 못했습니다.')
                remote.update(state='closed', exit_proof=proof)
            self._save_lease(lease)
            if lifecycle_guard is not None:
                lifecycle_guard()

    def _endpoint_alive(self, value):
        return self.liveness(value) not in ("exited", "reused")

    def release_maintenance(self, lease, *, profile_ids=None):
        transaction_id = identifier(lease["transaction_id"])
        saved = _read_json(self._lease_path(transaction_id))
        selected = None if profile_ids is None else {identifier(value) for value in profile_ids}
        if selected is not None and (not selected or not selected.issubset({p['profile_id'] for p in saved.get('profiles', [])})):
            raise UpdateError('restart_scope_changed', '복원할 프로필 범위를 확인해야 합니다.')
        unresolved = []
        for entry in saved.get("profiles", []):
            if selected is not None and entry['profile_id'] not in selected:
                continue
            for remote in entry.get('remotes', []):
                if remote.get('state') in ('stop_requested', 'start_requested', 'drain_requested') and self.remote_maintenance is not None:
                    try:
                        self.remote_maintenance.reconcile(remote)
                        self._save_lease(saved)
                    except (RuntimeError, ValueError, OSError, KeyError, TypeError):
                        pass
            if any(remote.get('state') in ('stop_requested', 'start_requested', 'drain_requested') for remote in entry.get('remotes', [])):
                unresolved.append(entry['profile_id'])
                continue
            if self.remote_maintenance is not None:
                try:
                    self.remote_maintenance.publish_started(self._profile(entry['profile_id']), entry.get('remotes', []))
                except (RuntimeError, ValueError, OSError, KeyError, TypeError):
                    unresolved.append(entry['profile_id'])
                    continue
            if entry.get("state") in ("released", "rejected"):
                entry["state"] = "released"
                continue
            if entry.get('remote_only'):
                entry['state'] = 'released'
                continue
            endpoints = entry.get("identities")
            if endpoints and not any(self._endpoint_alive(v) for v in endpoints.values()
                                     if isinstance(v, dict) and "pid" in v):
                entry["state"] = "released"
                continue
            if not entry.get("lease_token"):
                unresolved.append(entry["profile_id"])
                continue
            if entry.get("state") == "native_close_requested" and self._endpoint_alive({
                    "pid": entry["process"]["process_id"], "created": entry["process"]["process_created"]}):
                unresolved.append(entry["profile_id"])
                continue
            try:
                profile = self._profile(entry["profile_id"])
                if profile.get("generation") != entry["generation"]:
                    raise UpdateError("generation_changed", "이전 런타임의 종료 확인이 필요합니다.")
                admin = self.admin_factory(profile)
                result = admin.request("manager/maintenance/release", {
                    "transactionId": transaction_id, "leaseToken": entry["lease_token"]})
                if (result.get("held") is not False or result.get("frontendMutationBlocked") is not False
                        or result.get("transactionId") is not None or result.get("generation") != entry["generation"]):
                    raise UpdateError("maintenance_release_unverified", "프로필 작업 잠금 해제를 확인하지 못했습니다.")
                entry["state"] = "released"
            except (RuntimeError, ValueError, OSError):
                unresolved.append(entry["profile_id"])
        saved["state"] = "release_pending" if unresolved else "released"
        self._save_lease(saved)
        if unresolved:
            raise UpdateError("maintenance_release_pending", "일부 프로필의 작업 잠금 해제 확인이 필요합니다.")
        def release(data):
            active = ([data.get('profile_maintenance', {}).get(key) for key in selected] if selected is not None else
                      [data.get("update_maintenance")] if saved.get("profile_scope") is None else
                      [data.get("profile_maintenance", {}).get(key) for key in saved["profile_scope"]])
            for item in active:
                if item and item.get("transaction_id") == transaction_id:
                    item.update(state="released", updated_at=now())
        self.store.mutate(release)

    def _navigate(self, profile, entry):
        from .app_transport import AppTransport
        shortcut = {"profile_id": profile["id"], "thread_id": entry["thread_id"],
                    "host_id": entry.get("host_id", "local"),
                    "source_store_id": entry.get("source_store_id", "manager:" + profile["id"])}
        environment = self.instances.environment(profile)
        try:
            return AppTransport(self.root).open_conversation(
                profile, shortcut, profile["executable_path"], environment)
        finally:
            environment.clear()

    def recover_profile_restart(self, profile_id, transaction_id, *, revision):
        """Resume a user-retried scoped restart without closing it again."""
        profile_id, transaction_id = identifier(profile_id), identifier(transaction_id)
        path = self._lease_path(transaction_id)
        if not path.is_file():
            self.guard_launch(profile_id)
            return {'status': 'restart'}
        lease = _read_json(path)
        if lease.get('ssh_only'):
            # An SSH cohort journal is released by its own gate, never by a
            # restart recovery: a fresh attempt re-checks the SSH gate instead.
            return {'status': 'restart'}
        entries = lease.get('profiles', [])
        if (lease.get('transaction_id') != transaction_id or lease.get('profile_scope') != [profile_id]
                or len(entries) != 1 or entries[0].get('profile_id') != profile_id):
            raise UpdateError('restart_scope_changed', '이전 재시작 기록의 계정 범위가 일치하지 않습니다.')
        entry = entries[0]
        profile = self._profile(profile_id)
        if (entry.get('state') == 'native_close_requested' and entry.get('process')
                and self._endpoint_alive({'pid': entry['process']['process_id'],
                                          'created': entry['process']['process_created']})):
            # A prior WM_CLOSE can leave the same drained app resident. Reuse its
            # strict writer-release proof; never replay WM_CLOSE or close tasks.
            self._begin_global(transaction_id, profile_scope=[profile_id])
            if not self._finish_requested_idle_exit(profile, lease, entry):
                raise UpdateError('normal_exit_pending', '이전 프로필의 종료 확인을 기다리고 있습니다.')
        remotes = entry.get('remotes', [])
        resume = (entry.get('closed_verified') is True
                  and profile['policy']['desired_revision'] == revision
                  and all(r.get('state') in ('closed', 'prepared', 'start_requested', 'started')
                          and (r.get('state') == 'closed' or r.get('target_policy_revision') == revision)
                          for r in remotes))
        if not resume:
            # No close request is replayed here. A new attempt must take a fresh
            # idle snapshot after the previous gate is verifiably released.
            self.release_maintenance(lease)
            return {'status': 'restart'}

        # Re-establish only this profile's gate before reading current identity.
        # A different update/restart transaction cannot be overwritten.
        self._begin_global(transaction_id, profile_scope=[profile_id])
        try:
            profile = self._profile(profile_id)
            if profile['policy']['desired_revision'] != revision:
                raise UpdateError('policy_changed', '재시도 중 설정이 변경되었습니다.')
            identities = list(entry.get('identities', {}).values())
            if entry.get('process'):
                identities.append({'pid': entry['process']['process_id'], 'created': entry['process']['process_created']})
            if any(self._endpoint_alive(value) for value in identities if isinstance(value, dict) and 'pid' in value):
                raise UpdateError('previous_runtime_alive', '이전 실행의 종료를 확인해야 합니다.')
            live = self._live(profile)
            if live and (profile.get('generation') == entry['generation']
                         or profile['policy'].get('launched_revision') != revision):
                raise UpdateError('restored_instance_changed', '다른 설정으로 실행 중인 프로필을 재시작하지 않았습니다.')
            if self.remote_maintenance is not None:
                self.remote_maintenance.retry_pending_starts(profile, remotes, lambda: self._save_lease(lease))
            restored = self.restore_instance({'profile_id': profile_id})
            if restored.get('verified') is not True and restored.get('code') != 'remote_account_pending':
                raise UpdateError('restart_verification', '재시도한 프로필의 런타임 연결 확인이 필요합니다.')
            return {'status': 'restored', **restored}
        finally:
            self.release_maintenance(lease)

    def _update_recovery_entry(self, lease, profile_id):
        if lease.get('profile_scope') is not None or lease.get('verified') is not True:
            raise UpdateError('update_recovery_scope', '원래 업데이트의 프로필 기록이 필요합니다.')
        entry = next((item for item in lease.get('profiles', []) if item.get('profile_id') == profile_id), None)
        if entry is None or entry.get('closed_verified') is not True:
            raise UpdateError('previous_runtime_alive', '원래 프로필의 종료를 확인해야 합니다.')
        identities = list(entry.get('identities', {}).values())
        if entry.get('process'):
            identities.append({'pid': entry['process']['process_id'], 'created': entry['process']['process_created']})
        if any(self._endpoint_alive(value) for value in identities if isinstance(value, dict) and 'pid' in value):
            raise UpdateError('previous_runtime_alive', '이전 실행이 남아 있어 다시 시작하지 않았습니다.')
        profile = self._profile(profile_id)
        revision = profile['policy']['desired_revision']
        for remote in entry.get('remotes', []):
            if (remote.get('state') not in ('closed', 'prepared', 'start_requested', 'started')
                    or (remote['state'] != 'closed' and remote.get('target_policy_revision') != revision)):
                raise UpdateError('restore_policy_changed', '기존 SSH 복원 기록과 현재 설정을 확인해야 합니다.')
        live = self._live(profile)
        if live and (profile.get('generation') == entry['generation']
                     or profile['policy'].get('launched_revision') != revision):
            raise UpdateError('restored_instance_changed', '이미 실행 중인 다른 프로필 설정을 유지했습니다.')
        return profile, entry

    def recover_update_instance(self, restore_entry, transaction_id):
        """Reuse the original update journal, fencing only the profile restored."""
        profile_id, transaction_id = identifier(restore_entry['profile_id']), identifier(transaction_id)
        lease = _read_json(self._lease_path(transaction_id))
        if lease.get('transaction_id') != transaction_id:
            raise UpdateError('update_recovery_scope', '업데이트 기록이 일치하지 않습니다.')
        self._begin_global(transaction_id, profile_scope=[profile_id])
        try:
            profile, entry = self._update_recovery_entry(lease, profile_id)
            if bool(restore_entry.get('remote_only')) != bool(entry.get('remote_only')):
                raise UpdateError('update_recovery_scope', '복원할 창과 SSH 실행 범위가 일치하지 않습니다.')
            if self.remote_maintenance is not None:
                self.remote_maintenance.retry_pending_starts(profile, entry.get('remotes', []), lambda: self._save_lease(lease))
            return self.restore_instance(restore_entry)
        finally:
            self.release_maintenance(lease, profile_ids=[profile_id])

    def recover_update_maintenance(self, transaction, installed):
        """Explicitly resolve missing immutable starts after installation settles."""
        if transaction.get('install_outcome') == 'unknown':
            return {'maintenance_released': False}
        before = transaction.get('installed_before', {}).get('version')
        target = transaction.get('target', {}).get('version')
        if installed.get('version') not in (before, target):
            return {'maintenance_released': False}
        transaction_id = identifier(transaction['transaction_id'])
        lease = _read_json(self._lease_path(transaction_id))
        if lease.get('transaction_id') != transaction_id:
            raise UpdateError('update_recovery_scope', '업데이트 기록이 일치하지 않습니다.')
        selected = []
        for entry in lease.get('profiles', []):
            if any(remote.get('state') == 'start_requested' for remote in entry.get('remotes', [])):
                selected.append(self._update_recovery_entry(lease, entry['profile_id']))
        # Validate the entire pending cohort before any explicit same-revision retry.
        for profile, entry in selected:
            if self.remote_maintenance is None:
                return {'maintenance_released': False}
            self.remote_maintenance.retry_pending_starts(profile, entry.get('remotes', []), lambda: self._save_lease(lease))
        self.release_maintenance(lease)
        return {'maintenance_released': True}

    def restore_instance(self, entry):
        profile_id = entry.get("profile_id", entry.get("id"))
        profile = self._profile(profile_id)
        maintenance = self._maintenance_for(profile_id)
        self._restoration.transaction_id = maintenance.get("transaction_id") if maintenance else None
        remotes = []
        try:
            if maintenance and self.remote_maintenance is not None:
                lease = _read_json(self._lease_path(maintenance['transaction_id']))
                saved = next((p for p in lease['profiles'] if p['profile_id'] == profile_id), None)
                remotes = saved.get('remotes', []) if saved else []
                if remotes:
                    if any(remote.get('state') not in ('closed', 'prepared', 'start_requested', 'started') for remote in remotes):
                        raise UpdateError('remote_exit_unverified', 'SSH 서버 종료 확인 뒤 새 설정을 적용할 수 있습니다.')
                    self.remote_maintenance.prepare_and_start(profile, remotes, lambda: self._save_lease(lease))
                    self.remote_maintenance.publish_started(profile, remotes)
                    if self._profile(profile_id)['policy']['desired_revision'] != profile['policy']['desired_revision']:
                        raise UpdateError('policy_changed', 'SSH 시작 중 설정이 변경되었습니다. 최신 설정 적용이 필요합니다.')
            if entry.get('remote_only'):
                if not maintenance or not remotes:
                    raise UpdateError('remote_restore_unknown', '원격 서버의 설정 복원 기록이 필요합니다.')
                return {'verified': True, 'profile_id': profile_id, 'profile_reopened': False,
                        'remote_runtime_restored': True}
            with self.launch_admission(profile_id):
                shown = self.instances.show(profile_id)
        finally:
            self._restoration.transaction_id = None
        generation = shown.get("profile", {}).get("generation")
        deadline = self.clock() + self.timeout
        ready = None
        while self.clock() < deadline:
            current = self._profile(profile_id)
            live = self._live(current)
            if current.get("generation") != generation:
                return {"verified": False, "code": "restore_generation_changed"}
            if live and self._observer_ready(current, live):
                health = self.admin_factory(current).request("manager/maintenance/status", {})
                if (self._health(health, generation) and health.get("held") is False
                        and health.get("frontendMutationBlocked") is False):
                    ready = live
                    break
            self.sleep(.1)
        if ready is None:
            return {"verified": False, "profile_id": profile_id, "code": "runtime_restore_not_ready"}
        thread_id = entry.get("thread_id", entry.get("active_thread_id"))
        if thread_id:
            navigation = self.navigate(ready, {**entry, "thread_id": identifier(thread_id)})
            selected = bool(self.verify_selection and self.verify_selection(ready, entry, navigation) is True)
            restored = {"verified": True, "profile_id": profile_id, "profile_reopened": True,
                    "runtime_initialized": True, "selection_verified": selected,
                    "conversation_restore": "selected" if selected else
                        "requested" if navigation.get("state") == "request_sent" else "blocked",
                    "navigation_state": navigation.get("state", "unknown")}
        else:
            restored = {"verified": True, "profile_id": profile_id, "profile_reopened": True,
                        "runtime_initialized": True, "selection_verified": None}
        connections = self.remote_readiness.inspect(current, remotes)
        if not connections['ready']:
            restored.update(verified=False, code='remote_account_pending', generation=generation,
                            remote_connections=connections, message=pending_message(connections))
        return restored

    def check_restored_connections(self, profile_id, transaction_id, generation):
        """Poll existing connections only; never starts, logs in or closes anything."""
        profile = self._profile(profile_id)
        if profile.get('generation') != generation:
            raise UpdateError('restore_generation_changed', '계정 연결 확인 중 프로필 실행이 변경되었습니다.')
        lease = _read_json(self._lease_path(transaction_id))
        entry = next((p for p in lease.get('profiles', []) if p.get('profile_id') == profile_id), None)
        if entry is None or entry.get('closed_verified') is not True:
            raise UpdateError('restore_record_missing', 'SSH 연결을 확인할 재시작 기록이 필요합니다.')
        live = self._live(profile)
        if live is None or not self._observer_ready(profile, live):
            return {'verified': False, 'message': '다시 연 Codex의 계정 연결을 확인하고 있습니다.'}
        connections = self.remote_readiness.inspect(profile, entry.get('remotes', []))
        return {'verified': connections['ready'], 'remote_connections': connections,
                'message': pending_message(connections) if not connections['ready'] else 'SSH 계정 연결을 확인했습니다.'}

    def verify_recovery(self, transaction, installed):
        """Reconcile only owned proxy leases. Never guess Windows installer outcome."""
        transaction_id = transaction.get("transaction_id")
        if not transaction_id:
            return {}
        from .package_install import PackageInstaller
        proof = PackageInstaller(self.root).inspect(transaction)
        path = self._lease_path(transaction_id)
        if path.is_file():
            lease = _read_json(path)
            try:
                # Explicit recovery may reconcile a lost response to the
                # transaction-idempotent acquire; automatic release never retries it.
                for entry in lease.get("profiles", []):
                    if entry.get("lease_token") or entry.get("state") != "acquire_requested":
                        continue
                    if not any(self._endpoint_alive(v) for v in entry.get("identities", {}).values()
                               if isinstance(v, dict) and "pid" in v):
                        continue
                    profile = self._profile(entry["profile_id"])
                    if profile.get("generation") != entry["generation"]:
                        continue
                    result = self.admin_factory(profile).request("manager/maintenance/acquire", {
                        "transactionId": transaction_id})
                    if (result.get("held") is True and result.get("transactionId") == transaction_id
                            and result.get("generation") == entry["generation"]):
                        entry["lease_token"] = identifier(result["leaseToken"])
                        entry["state"] = "held"
                        self._save_lease(lease)
                self.release_maintenance(lease)
                proof["maintenance_released"] = True
            except (RuntimeError, OSError, ValueError):
                proof["maintenance_released"] = False
        else:
            # No lease request was recorded by hooks, so no proxy was acquired.
            proof["maintenance_released"] = transaction.get("maintenance_state") not in ("held", "requested")
        # An installer may outlive its Python caller. Observing an installed
        # version alone cannot mark installer_settled or cancel an in-flight close.
        return proof

    def remote_snapshot(self):
        return self._remote_snapshot() if self._remote_snapshot else [
            {"host_id": 'ssh:' + binding['alias'], "online": None, "compatible": False}
            for p in self.store.read()["profiles"] for binding in p.get("remote_bindings", [])
        ]
