"""Persistent SSH update queues. Status is local; network work is asynchronous.

Managed updates own one profile's recorded SSH cohort and never own its desktop.
Stock updates are a separate, explicitly confirmed action and are never replayed
by the scheduler. Lost managed lifecycle replies retain their original journal.
A relaunch leaves a reservation pinned to an older generation; the scheduler
retires it only while the transaction's own preserved journal proves no host
was ever asked to stop or start, and keeps the automatic update setting for the
new generation.
"""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import threading
import time
from uuid import uuid4

from .store import Unchanged, identifier, now
from .updates import UpdateError, _lock_file, _unlock_file


ACTIVE = frozenset({'queued', 'waiting', 'applying', 'recovering'})
STOCK_ACTIVE = frozenset({'dispatching', 'pending', 'queued', 'starting', 'applying', 'running', 'unknown'})
# A reservation is terminal once nothing may be dispatched for its transaction.
FINISHED = frozenset({'complete', 'cancelled', 'superseded'})
# Records in these states never crossed a stop/start/drain request boundary.
# An explicit graceful drain is a dispatched lifecycle request, so
# 'drain_requested' is deliberately not unmoved: only read-only observation may
# hand a gate back, and a verified drain leaves its exit proof behind.
UNMOVED = frozenset({'unobserved', 'observed', 'closed'})
# Keys that only exist once a stop, start, drain or resume was requested.
UNTOUCHED = ('exit_proof', 'next_binding', 'started', 'reinspect')
# Refusals that keep the SSH gate until the preserved evidence is reviewed.
REVIEW = frozenset({'lifecycle_pending', 'journal_missing', 'journal_unverified',
                    'journal_generation_changed', 'gate_generation_changed'})


def _journal_entry(lease, profile_id, transaction_id):
    """The journal entry that belongs to exactly this profile transaction.

    The file name alone never proves ownership: a path that was rewritten for
    another transaction, scope or profile is not evidence for this
    reservation, so its records must never be read as an unmoved proof.
    """
    if not isinstance(lease, dict):
        return None
    profiles = lease.get('profiles')
    if (lease.get('transaction_id') != transaction_id or lease.get('ssh_only') is not True
            or lease.get('profile_scope') != [profile_id] or not isinstance(profiles, list)
            or len(profiles) != 1 or not isinstance(profiles[0], dict)
            or profiles[0].get('profile_id') != profile_id
            or not isinstance(profiles[0].get('remotes'), list)):
        return None
    return profiles[0]


def _unmoved(records):
    """True only when no host was ever asked to stop or start.

    An observed host is still unmoved: inspection alone never changes a remote
    runtime, so its recorded process and idle state do not matter. Any other
    record state, or any stop/start evidence, keeps the maintenance gate: a
    lost stop/start reply must never be released without human confirmation.
    """
    return all(record.get('state') in UNMOVED and not any(key in record for key in UNTOUCHED)
               for record in records)


def _version(bundle):
    if not isinstance(bundle, str):
        return None
    return re.sub(r'-[0-9a-f]{16}$', '', bundle)


def _stock_pending(value):
    job = value.get('update_job') or {}
    return job.get('state') in STOCK_ACTIVE or job.get('verification_pending') is True


class RemoteUpdates:
    def __init__(self, root, store, remote, hooks, *, probe=None, stock_update=None,
                 spawn=None, interval=15, check_interval=3600, clock=time.time):
        self.root, self.store, self.remote, self.hooks = Path(root), store, remote, hooks
        self.probe, self.stock_update = probe, stock_update
        # Keep startup SSH discovery bounded even with many profiles/hosts.
        # Submitted checks remain visible as checking while waiting in the queue.
        self.executor = None if spawn else ThreadPoolExecutor(max_workers=3, thread_name_prefix='ssh-update')
        self.spawn = spawn or self.executor.submit
        self.interval, self.check_interval, self.clock = interval, check_interval, clock
        self.mutex = threading.RLock()
        self.busy = set()
        self.stopping, self.wake = threading.Event(), threading.Event()
        self.started = False

    def _key(self, profile_id, alias):
        profile_id = identifier(profile_id)
        if not isinstance(alias, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', alias):
            raise ValueError('등록된 SSH 호스트를 선택해 주세요.')
        profile = self.store.profile(profile_id)
        if profile.get('removed_at') or profile.get('view_only'):
            raise ValueError('사용 가능한 관리 프로필을 선택해 주세요.')
        return profile_id + ':' + alias

    @staticmethod
    def _default(profile_id, alias):
        return dict(profile_id=profile_id, alias=alias, auto_check=True, auto_apply=False,
                    checking=False, checked_at=None, job=None,
                    managed=dict(active_version=None, prepared_version=None, available_version=None,
                                 state='unknown', message='SSH 버전을 확인하면 실행 중인 관리 런타임을 표시합니다.'),
                    stock=dict(cli_version=None, daemon_version=None, daemon_state='unknown',
                               state='unknown', message='기존 Codex 버전을 아직 확인하지 않았습니다.',
                               update_supported=False, safe_auto_update=False))

    def _read(self, profile_id, alias):
        key = profile_id + ':' + alias
        return deepcopy(self.store.read().get('remote_updates', {}).get(key) or self._default(profile_id, alias))

    def _save(self, profile_id, alias, **changes):
        def save(data):
            value = data.setdefault('remote_updates', {}).setdefault(
                profile_id + ':' + alias, self._default(profile_id, alias))
            value.update(deepcopy(changes))
            return deepcopy(value)
        return self.store.mutate(save)

    def status(self, profile_id, alias):
        self._key(profile_id, alias)
        value = self._read(profile_id, alias)
        data = self.store.read()
        value['managed']['can_schedule'] = alias in data.get('ssh_inventory', {}).get(
            profile_id, {}).get('hosts', [])
        shared = next((v for v in data.get('remote_updates', {}).values()
                       if v.get('profile_id') == profile_id and (v.get('job') or {}).get('state') in ACTIVE), None)
        if shared:
            value['job'] = {**deepcopy(shared['job']), 'alias': shared['alias']}
        # Internal generation/target pins never become UI claims of activity.
        return {key: deepcopy(item) for key, item in value.items() if not key.startswith('_')}

    def status_all(self, state=None):
        # state() passes the snapshot it already read for this poll.
        values = (self.store.read() if state is None else state).get('remote_updates', {}).values()
        items = [{key: deepcopy(item) for key, item in value.items() if not key.startswith('_')}
                 for value in values]
        return dict(worker_active=any(v.get('checking') or (v.get('job') or {}).get('state') in ACTIVE
                                      or _stock_pending(v.get('stock', {})) for v in items), items=items)

    def settings(self, profile_id, alias, *, auto_check=None, auto_apply=None):
        self._key(profile_id, alias)
        changes = {}
        for key, value in (('auto_check', auto_check), ('auto_apply', auto_apply)):
            if value is not None:
                if type(value) is not bool:
                    raise ValueError(key + ' 설정은 켜기 또는 끄기로 지정해 주세요.')
                changes[key] = value
        self._save(profile_id, alias, **changes)
        self.wake.set()
        return self.status(profile_id, alias)

    def _launch(self, profile_id, alias, fn, *, checking=False):
        key = self._key(profile_id, alias)
        with self.mutex:
            if key in self.busy or self.stopping.is_set():
                return False
            try:
                claim = _lock_file(self.store.directory / 'remote-updates' / profile_id / (alias + '.lock'))
            except UpdateError:
                return False
            self.busy.add(key)
            if checking:
                self._save(profile_id, alias, checking=True)
        def run():
            try:
                if not self.stopping.is_set():
                    fn()
            except Exception as error:
                self._save(profile_id, alias, error=getattr(error, 'code', 'ssh_observation_unavailable'))
            finally:
                if checking:
                    self._save(profile_id, alias, checking=False)
                with self.mutex:
                    self.busy.discard(key)
                _unlock_file(claim)
        try:
            self.spawn(run)
        except Exception:
            with self.mutex:
                self.busy.discard(key)
            if checking:
                self._save(profile_id, alias, checking=False)
            _unlock_file(claim)
            raise
        return True

    def check(self, profile_id, alias):
        self._launch(profile_id, alias, lambda: self._check(profile_id, alias), checking=True)
        return self.status(profile_id, alias)

    def _available(self, platform, architecture):
        if platform != 'linux' or architecture not in ('x86_64', 'aarch64'):
            raise UpdateError('remote_artifact_missing', '지원되는 Linux 관리 런타임 묶음이 필요합니다.')
        path = self.root / 'artifacts/remote' / (platform + '-' + architecture) / 'manifest.json'
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 256000:
            raise UpdateError('remote_artifact_missing', 'Linux 관리 런타임 묶음을 확인할 수 없습니다.')
        manifest = json.loads(path.read_text(encoding='utf-8-sig'))
        version = manifest.get('version')
        if not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}', version):
            raise UpdateError('invalid_artifact', 'Linux 관리 런타임 버전을 확인할 수 없습니다.')
        digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:16]
        return dict(version=version, bundle_id=version + '-' + digest)

    def _check(self, profile_id, alias):
        self.remote._alias(alias)
        profile = self.store.profile(profile_id)
        generation = profile.get('generation')
        source = next((b for b in profile.get('remote_bindings', []) if b.get('alias') == alias), None)
        stock = self._read(profile_id, alias)['stock']
        error = None
        try:
            if self.probe is None:
                from .ssh_versions import probe
            else:
                probe = self.probe
            stock = probe(self.root, self.remote, alias)
            stock['safe_auto_update'] = False
        except Exception as exception:
            stock = {**stock, 'state': 'unavailable', 'message': '기존 Codex 버전을 확인할 수 없습니다.',
                     'safe_auto_update': False}
            error = getattr(exception, 'code', 'stock_observation_unavailable')
        managed = dict(active_version=None, prepared_version=_version((source or {}).get('runtime_bundle')),
                       available_version=None, state='unknown', message='관리 런타임 실행 상태를 확인할 수 없습니다.')
        target = None
        try:
            observed = stock
            artifact = self._available(observed['platform'], observed['architecture'])
            target = dict(platform=observed['platform'], architecture=observed['architecture'],
                          bundle=artifact['bundle_id'], version=artifact['version'],
                          host_identity=observed['managed_host_identity'])
            if source and source.get('host_identity') != target['host_identity']:
                raise UpdateError('ssh_host_changed', '저장된 SSH 호스트 식별 정보가 변경되었습니다.')
            managed.update(available_version=artifact['version'], available_bundle=artifact['bundle_id'],
                           prepared_bundle=(source or {}).get('runtime_bundle'))
            if source and source.get('prepared') is True:
                actual = self.hooks.remote_maintenance.request(source, 'inspect', discover_active=True, observe_only=True)
                bundle = actual.get('runtime_bundle')
                if actual.get('host_identity') and actual['host_identity'] != target['host_identity']:
                    raise UpdateError('ssh_host_changed', '실행 중인 SSH 호스트 식별 정보가 변경되었습니다.')
                managed.update(active_version=_version(bundle), active_bundle=bundle,
                               active_revision=(actual.get('process') or {}).get('revision'),
                               idle=actual['idle'], running=actual['process'] is not None)
                if actual['process'] is None:
                    state, message = ('stopped', '관리 SSH 런타임이 실행 중이지 않습니다. 준비된 파일은 별도로 표시합니다.')
                elif bundle is None:
                    state, message = ('unknown', '실행 중인 관리 런타임의 버전을 확인할 수 없습니다.')
                elif bundle == artifact['bundle_id']:
                    state, message = ('current', '관리 SSH 런타임이 현재 제공되는 버전으로 실행 중입니다.')
                else:
                    state, message = ('update_available', '이 프로필에 적용할 관리 SSH 업데이트가 있습니다.')
                code = actual.get('observation_code')
                if code:
                    managed['observation_code'] = code
                    managed['version_state'] = state
                    if code == 'remote_listener_unavailable':
                        state = 'attention'
                        message = 'SSH 프로세스는 남아 있지만 연결이 닫혀 있습니다. 종료 결과 확인이 필요합니다.'
                    else:
                        message += ' 기존 작업의 종료 여부는 확인하지 못해 자동으로 중단하지 않습니다.'
            else:
                state, message = ('not_prepared', '이 프로필을 SSH에 연결한 뒤 관리 업데이트를 예약해 주세요.')
            managed.update(state=state, message=message)
        except Exception as exception:
            error = getattr(exception, 'code', 'managed_observation_unavailable')
            managed['observation_code'] = error
            if error == 'ssh_host_changed':
                target = None
                managed.update(state='attention', message='SSH 호스트 식별 정보가 변경되었습니다. 연결 대상을 확인해 주세요.')
        current = self.store.profile(profile_id)
        if current.get('generation') != generation or next((b for b in current.get('remote_bindings', [])
                if b.get('alias') == alias), None) != source:
            managed.update(state='unknown', message='확인 중 프로필 연결 설정이 변경되었습니다. 다시 확인해 주세요.')
            target = None
        self._save(profile_id, alias, stock=stock, managed=managed, checked_at=now(),
                   _checked_epoch=self.clock(), _target=target, error=error)
        value = self._read(profile_id, alias)
        if (value['auto_apply'] and managed['state'] == 'update_available'
                and (value.get('job') or {}).get('state') in (None, 'complete', 'cancelled')):
            self.schedule(profile_id, alias)

    def schedule(self, profile_id, alias):
        self._key(profile_id, alias)
        with self.mutex, self.store.locked():
            data = self.store.read()
            profile = self.store.profile(profile_id, data)
            if alias not in data.get('ssh_inventory', {}).get(profile_id, {}).get('hosts', []):
                raise UpdateError('ssh_not_enrolled', '이 프로필을 SSH 호스트에 연결한 뒤 업데이트를 예약해 주세요.')
            # A profile's aliases share a daemon cohort and one maintenance gate.
            for value in data.get('remote_updates', {}).values():
                if value.get('profile_id') == profile_id and (value.get('job') or {}).get('state') in ACTIVE:
                    return self.status(profile_id, alias)
            gate = data.get('ssh_maintenance', {}).get(profile_id, {})
            if gate.get('remote_update') and gate.get('state') not in (None, 'released'):
                raise UpdateError('ssh_update_attention', '이전 SSH 업데이트 결과를 확인한 뒤 새 업데이트를 예약해 주세요.')
            job = dict(id=str(uuid4()), state='queued', scope='profile',
                       message='이 프로필에 연결된 SSH 호스트의 작업이 끝나면 관리 런타임을 업데이트합니다.')
            self._save(profile_id, alias, job=job, _job=dict(transaction_id=str(uuid4()),
                       generation=profile.get('generation'), revision=profile['policy']['desired_revision'],
                       targets=None, cancel_requested=False))
        self._launch(profile_id, alias, lambda: self.step(profile_id, alias))
        self.wake.set()
        return self.status(profile_id, alias)

    def _job_state(self, profile_id, alias, state, message, **extras):
        value = self._read(profile_id, alias)
        self._save(profile_id, alias, job={**value['job'], 'state': state, 'message': message, **extras})

    def _update_pin(self, profile_id, alias, transaction_id, **changes):
        def update(data):
            value = data['remote_updates'][profile_id + ':' + alias]
            pin = value['_job']
            if pin.get('transaction_id') != transaction_id:
                raise UpdateError('ssh_generation_changed', 'SSH 업데이트 예약이 변경되었습니다.')
            pin.update(deepcopy(changes))
            return deepcopy(pin)
        return self.store.mutate(update)

    def _lease(self, transaction_id):
        path = self.hooks._lease_path(transaction_id)
        return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else None

    @staticmethod
    def _stale(value, profile):
        """A held reservation that pins an older run generation or revision."""
        pin, job = value.get('_job') or {}, value.get('job') or {}
        if (not pin or job.get('state') in FINISHED or not value.get('alias')
                or value.get('profile_id') != profile.get('id')):
            return False
        return (pin.get('generation') != profile.get('generation')
                or pin.get('revision') != profile['policy']['desired_revision'])

    def _cohort_alias(self, profile_id, alias):
        """A profile's aliases share one reservation; find the alias holding it."""
        value = self._read(profile_id, alias)
        if (value.get('_job') or {}) and (value.get('job') or {}).get('state') not in FINISHED:
            return alias
        owned = sorted(v['alias'] for v in self.store.read().get('remote_updates', {}).values()
                       if v.get('profile_id') == profile_id and isinstance(v.get('alias'), str)
                       and (v.get('_job') or {}) and (v.get('job') or {}).get('state') not in FINISHED)
        return owned[0] if owned and owned[0] else alias

    def _journal(self, profile_id, transaction_id):
        """Read this transaction's journal without raising: (present, value).

        ``present`` distinguishes a transaction that never wrote a journal from
        a path that exists but cannot be read as evidence. An unreadable path
        still counts as present, so it is reviewed instead of trusted.
        """
        path = self.hooks._lease_path(transaction_id)
        try:
            if not path.is_file():
                return False, None
            return True, json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError, TypeError):
            return True, None

    def _retire_stale(self, profile_id, alias, *, apply=False, observe=False):
        """Safely retire one reservation whose profile generation already moved on.

        Only the transaction's own preserved journal may prove that no stop or
        start was ever dispatched: unobserved/observed/closed records without
        an exit proof or a start journal. A journal that is missing, belongs to
        another transaction or profile, was adopted by another generation, or
        is already past a lifecycle request keeps its gate for manual review.
        The superseded journal is persisted before the gate is handed back, so
        a crash can never leave a released gate claiming an in-flight
        lifecycle. A released reservation keeps ``auto_apply`` so the next
        observation can schedule the same update for the current generation.
        """
        value = self._read(profile_id, alias)
        pin, job = value.get('_job') or {}, value.get('job') or {}
        transaction = pin.get('transaction_id')
        profile = self.store.profile(profile_id)
        report = dict(profile_id=profile_id, alias=alias, transaction_id=transaction,
                      generation=pin.get('generation'), current_generation=profile.get('generation'),
                      eligible=False, released=False, cancelled=False, gate_released=False,
                      journal_released=False, observed=False, scheduled=False, reason=None)
        lease, gated = None, False
        if value.get('profile_id') != profile_id or value.get('alias') != alias:
            report['reason'] = 'reservation_mismatch'
        elif not transaction:
            report['reason'] = 'no_reservation'
        elif job.get('state') in FINISHED:
            report['reason'] = 'already_finished'
        elif (pin.get('generation') == profile.get('generation')
                and pin.get('revision') == profile['policy']['desired_revision']):
            report['reason'] = 'generation_current'
        elif profile.get('removed_at') or profile.get('view_only'):
            report['reason'] = 'profile_unavailable'
        else:
            present, lease = self._journal(profile_id, transaction)
            entry = _journal_entry(lease, profile_id, transaction)
            gate = self.store.read().get('ssh_maintenance', {}).get(profile_id, {})
            gated = gate.get('transaction_id') == transaction
            if entry is None and gated:
                report['reason'] = 'journal_unverified' if present else 'journal_missing'
            elif entry is None and present:
                report['reason'] = 'journal_unverified'
            elif entry is None:
                # Nothing was ever journaled for this transaction, so no host
                # was asked to move and there is no evidence to hand back.
                report['eligible'] = True
            elif entry.get('generation') != pin.get('generation'):
                report['reason'] = 'journal_generation_changed'
            elif gated and gate.get('generation') not in (None, pin.get('generation')):
                report['reason'] = 'gate_generation_changed'
            elif not _unmoved(entry['remotes']):
                report['reason'] = 'lifecycle_pending'
            else:
                report['eligible'] = True
        if not apply or report['reason'] is not None:
            return report
        if lease is not None:
            # Write-ahead the superseded evidence: the gate is the claim that
            # frees the profile for a fresh lifecycle, so it is handed back only
            # after this transaction can no longer be resumed as in-flight work.
            lease.update(state='released', superseded=True)
            self.hooks._save_lease(lease)
            report['journal_released'] = True
        if gated:
            def release(data):
                current = data['ssh_maintenance'][profile_id]
                if (current.get('transaction_id') != transaction
                        or current.get('generation') not in (None, pin.get('generation'))):
                    raise UpdateError('ssh_generation_changed', 'SSH 유지보수 게이트가 변경되었습니다.')
                current.update(state='released', code='ssh_generation_superseded',
                               message='프로필이 다시 실행되어 이전 SSH 업데이트 예약을 해제했습니다. 자동 업데이트 설정은 유지합니다.',
                               updated_at=now())
            try:
                self.store.mutate(release)
            except UpdateError:
                # A concurrent lifecycle re-owned the gate after the preflight.
                # This reservation is over, but that gate is not ours to release.
                self._job_state(profile_id, alias, 'cancelled',
                                '프로필이 다시 실행되어 이전 SSH 업데이트 예약을 정리했습니다. 자동 업데이트 설정은 유지합니다.',
                                code='ssh_generation_superseded')
                report['cancelled'] = True
                report['reason'] = 'gate_changed'
                return report
            report['gate_released'] = True
        self._job_state(profile_id, alias, 'cancelled',
                        '프로필이 다시 실행되어 이전 SSH 업데이트 예약을 정리했습니다. 자동 업데이트 설정은 유지합니다.',
                        code='ssh_generation_superseded')
        report['cancelled'] = True
        report['released'] = True
        if observe and (value['auto_check'] or value['auto_apply']):
            try:
                self._check(profile_id, alias)
                report['observed'] = True
            except Exception as error:
                report['observation_error'] = getattr(error, 'code', 'ssh_observation_unavailable')
            report['scheduled'] = (self._read(profile_id, alias).get('job') or {}).get('state') == 'queued'
        self.wake.set()
        return report

    def retirement(self, profile_id, alias):
        """Read-only preview of the safe retirement report for this profile."""
        self._key(profile_id, alias)
        return self._retire_stale(profile_id, self._cohort_alias(profile_id, alias))

    def recover(self, profile_id, alias, *, observe=False):
        """Retire a stale-generation reservation without touching the desktop.

        Runs synchronously under the same cross-process worker claim as apply
        and cancel, so a second controller can never retire a reservation
        another one is stepping. Returns the JSON-safe retirement report:
        ``released`` states whether the gate was handed back, ``cancelled``
        states whether the stale reservation was retired, and ``reason``
        explains a refusal (``lifecycle_pending``, ``journal_missing``,
        ``journal_unverified``, ``journal_generation_changed``,
        ``gate_generation_changed``, ``gate_changed``, ``generation_current``,
        ``busy``, ``unavailable``). Never stops, starts, or restarts a remote
        runtime and never closes the local window.
        """
        self._key(profile_id, alias)
        alias = self._cohort_alias(profile_id, alias)
        key = profile_id + ':' + alias
        with self.mutex:
            if key in self.busy:
                return dict(self._retire_stale(profile_id, alias), eligible=False, reason='busy')
            try:
                claim = _lock_file(self.store.directory / 'remote-updates' / profile_id / (alias + '.lock'))
            except UpdateError:
                return dict(self._retire_stale(profile_id, alias), eligible=False, reason='busy')
            self.busy.add(key)
        try:
            return self._retire_stale(profile_id, alias, apply=True, observe=observe)
        except (RuntimeError, ValueError, OSError, KeyError, TypeError) as error:
            # A UI caller receives a refusal, never a traceback: an aborted
            # retirement keeps the gate and only reports that it did not run.
            report = self._retire_stale(profile_id, alias)
            report.update(eligible=False, released=False, cancelled=False, reason='unavailable',
                          error=getattr(error, 'code', 'ssh_retirement_unavailable'))
            return report
        finally:
            with self.mutex:
                self.busy.discard(key)
            _unlock_file(claim)

    def _retire_pass(self, profile_id, alias):
        """Scheduler pass: retire a stale reservation or hand it to review."""
        report = self._retire_stale(profile_id, alias, apply=True, observe=True)
        if not report['released'] and report['reason'] in REVIEW:
            self.hooks.remote_open_failed(profile_id, report['transaction_id'], 'ssh_generation_changed')
            self._job_state(profile_id, alias, 'attention',
                            'SSH 업데이트 결과 확인이 필요합니다. 이전 버전의 연결 정보와 적용 기록을 보존했습니다.',
                            code='ssh_generation_changed')
        return report

    def cancel(self, profile_id, alias):
        self._key(profile_id, alias)
        requested_alias = alias
        with self.mutex:
            shared = next((v for v in self.store.read().get('remote_updates', {}).values()
                           if v.get('profile_id') == profile_id and (v.get('job') or {}).get('state') in ACTIVE), None)
            if shared:
                alias = shared['alias']
            value = self._read(profile_id, alias)
            if (value.get('job') or {}).get('state') not in ACTIVE:
                return self.status(profile_id, requested_alias)
            self._update_pin(profile_id, alias, value['_job']['transaction_id'], cancel_requested=True)
        # Cancellation must own the same cross-process worker claim as apply.
        # The process-local busy set cannot prove another controller is idle.
        self._launch(profile_id, alias, lambda: self.step(profile_id, alias))
        self.wake.set()
        return self.status(profile_id, requested_alias)

    def _cancel(self, profile_id, alias):
        value = self._read(profile_id, alias)
        transaction = value['_job']['transaction_id']
        present, lease = self._journal(profile_id, transaction)
        entry = _journal_entry(lease, profile_id, transaction)
        # A journal that is missing, malformed, or past a lifecycle request
        # cannot prove that no host still holds half-applied work: keep the
        # gate until the preserved evidence is reviewed.
        gate = self.store.read().get('ssh_maintenance', {}).get(profile_id, {})
        if ((not present and gate.get('transaction_id') == transaction)
                or (present and entry is None)
                or (entry is not None and not _unmoved(entry['remotes']))):
            self._update_pin(profile_id, alias, transaction, cancel_requested=False)
            self._job_state(profile_id, alias, 'recovering',
                            'SSH 종료 또는 시작 요청이 이미 진행되었습니다. 결과를 확인한 뒤 연결 제한을 해제합니다.')
            return False
        if lease:
            def release(data):
                gate = data.get('ssh_maintenance', {}).get(profile_id)
                if gate and gate.get('transaction_id') == transaction:
                    gate.update(state='released', updated_at=now())
            self.store.mutate(release)
            lease['state'] = 'released'
            lease['cancelled'] = True
            self.hooks._save_lease(lease)
        self._job_state(profile_id, alias, 'cancelled', '런타임을 변경하기 전에 관리 SSH 업데이트를 취소했습니다.')
        return True

    def step(self, profile_id, alias):
        """One recoverable queue pass; callers serialize it with the worker claim."""
        value = self._read(profile_id, alias)
        if (value.get('job') or {}).get('state') not in ACTIVE:
            return
        pin = value['_job']
        transaction = pin['transaction_id']
        try:
            if pin.get('cancel_requested'):
                self._cancel(profile_id, alias)
                return
            profile = self.store.profile(profile_id)
            if (profile.get('generation') != pin['generation']
                    or profile['policy']['desired_revision'] != pin['revision']):
                # An ordinary relaunch or settings change invalidates the pin.
                # Hand the gate back only while the journal proves that no
                # stop or start was ever dispatched for this transaction.
                if self._retire_stale(profile_id, alias, apply=True, observe=True)['released']:
                    return
                raise UpdateError('ssh_generation_changed', '프로필 실행 세대 또는 설정이 변경되었습니다.')
            gate = self.store.read().get('ssh_maintenance', {}).get(profile_id, {})
            if gate.get('transaction_id') != transaction:
                self.hooks.begin_remote_reconcile(profile_id, ensure_local=False,
                    force_runtime_update=True, transaction_id=transaction)
            lease = self._lease(transaction)
            if lease['state'] == 'released':
                self._job_state(profile_id, alias, 'complete', '관리 SSH 업데이트를 완료했습니다. 로컬 작업은 유지했습니다.')
                return
            if not pin.get('targets'):
                targets = {}
                for record in lease['profiles'][0]['remotes']:
                    self._check(profile_id, record['alias'])
                    target = self._read(profile_id, record['alias']).get('_target')
                    if not target:
                        raise UpdateError('ssh_target_unknown', '아직 SSH 업데이트 대상을 확인할 수 없습니다.')
                    targets[record['alias']] = target
                pin = self._update_pin(profile_id, alias, transaction, targets=targets)
            def guard():
                latest = self._read(profile_id, alias)
                if latest['_job'].get('cancel_requested'):
                    raise UpdateError('ssh_update_cancelled', '업데이트 취소가 요청되었습니다.')
                for host, target in pin['targets'].items():
                    artifact = self._available(target['platform'], target['architecture'])
                    if artifact['bundle_id'] != target['bundle']:
                        raise UpdateError('ssh_target_changed', '제공되는 관리 런타임 묶음이 변경되었습니다.')
                    source = next((b for b in self.store.profile(profile_id).get('remote_bindings', [])
                                   if b.get('alias') == host), None)
                    if not source or source.get('host_identity') != target['host_identity']:
                        raise UpdateError('ssh_host_changed', '저장된 SSH 호스트 식별 정보가 변경되었습니다.')
                    journal = self._lease(transaction)
                    record = next(r for r in journal['profiles'][0]['remotes'] if r['alias'] == host)
                    if record.get('next_binding') and source.get('runtime_bundle') != target['bundle']:
                        raise UpdateError('ssh_target_changed', '준비된 관리 런타임이 예약한 버전과 다릅니다.')
            self._job_state(profile_id, alias, 'applying', 'SSH 작업 종료를 확인하고 관리 런타임을 적용하고 있습니다.')
            complete = self.hooks.reconcile_opened_remotes(profile_id, transaction, target_guard=guard)
            if complete:
                self._job_state(profile_id, alias, 'complete', '관리 SSH 업데이트를 완료했습니다. 로컬 작업은 유지했습니다.')
                for host in pin['targets']:
                    self._check(profile_id, host)
            else:
                self._job_state(profile_id, alias, 'waiting', 'SSH 작업 종료가 확인될 때까지 기다립니다. 로컬 작업은 계속할 수 있습니다.')
        except Exception as error:
            code = getattr(error, 'code', 'ssh_observation_unavailable')
            if code == 'ssh_update_cancelled':
                self._cancel(profile_id, alias)
                return
            fatal = code in {'ssh_generation_changed', 'ssh_target_changed', 'ssh_host_changed',
                            'remote_binding_changed', 'remote_configuration_changed',
                            'remote_runtime_exited', 'remote_start_timeout'}
            if fatal:
                self.hooks.remote_open_failed(profile_id, transaction, code)
            self._job_state(profile_id, alias, 'attention' if fatal else 'waiting',
                ('SSH 업데이트 결과 확인이 필요합니다. 이전 버전의 연결 정보와 적용 기록을 보존했습니다.'
                 if fatal else 'SSH 실행 상태를 확인할 때까지 기다립니다. 결과가 불명확한 종료·시작 요청은 반복하지 않습니다.'), code=code)

    def update_stock(self, profile_id, alias, *, confirmed=False, observation_id=None):
        self._key(profile_id, alias)
        if confirmed is not True:
            raise ValueError('기존 Codex 업데이트로 연결이 중단될 수 있음을 먼저 확인해 주세요.')
        value = self._read(profile_id, alias)
        stock = value['stock']
        # The caller must send the observation displayed when the user confirmed.
        # A newer cached probe must never silently substitute another target.
        token = observation_id
        if not isinstance(token, str) or not token or token != stock.get('observation_id') or not stock.get('update_supported'):
            raise ValueError('SSH 버전을 확인한 뒤 지원되는 기존 Codex 업데이트를 실행해 주세요.')
        if _stock_pending(stock):
            return self.status(profile_id, alias)
        def update():
            self._save(profile_id, alias, stock={**stock, 'update_job': dict(state='dispatching',
                       message='확인한 기존 Codex 업데이트를 시작하고 있습니다.')})
            try:
                if self.stock_update is None:
                    from .ssh_versions import update as perform
                else:
                    perform = self.stock_update
                result = perform(self.root, self.remote, alias, token, confirmed=True)
                self._save(profile_id, alias, stock={**result, 'safe_auto_update': False})
            except Exception as error:
                self._save(profile_id, alias, stock={**stock, 'safe_auto_update': False,
                    'update_job': dict(state='unknown', message='업데이트 결과가 불명확합니다. 요청을 반복하지 않고 상태를 확인합니다.')},
                    error=getattr(error, 'code', 'stock_update_unverified'))
            self.wake.set()
        if not self._launch(profile_id, alias, update, checking=True):
            raise UpdateError('ssh_update_busy', '이 SSH 호스트의 확인 또는 업데이트가 진행 중입니다. 완료 후 다시 시도해 주세요.')
        return self.status(profile_id, alias)

    def start(self):
        with self.mutex:
            if self.started:
                return
            self.started = True
        # A process restart only resumes managed journals and read-only probes.
        # It never resubmits the explicit stock update operation. Clear stale
        # checking claims in one write, and only when one is actually set.
        def clear(data):
            stale = [value for value in data.get('remote_updates', {}).values() if value.get('checking') is not False]
            for value in stale:
                value['checking'] = False
            return len(stale) if stale else Unchanged(0)
        self.store.mutate(clear)
        threading.Thread(target=self._loop, name='ssh-update-scheduler', daemon=True).start()

    def tick(self):
        data = self.store.read()
        pairs = {(p['id'], b['alias']) for p in data['profiles'] if not p.get('removed_at') and not p.get('view_only')
                 for b in p.get('remote_bindings', []) if b.get('alias')}
        pairs.update((v['profile_id'], v['alias']) for v in data.get('remote_updates', {}).values())
        for profile_id, alias in sorted(pairs):
            try:
                value = self._read(profile_id, alias)
                profile = next((p for p in data['profiles'] if p['id'] == profile_id), None)
                if not profile or profile.get('removed_at') or profile.get('view_only'):
                    if (value.get('job') or {}).get('state') in ACTIVE:
                        self._job_state(profile_id, alias, 'attention', '프로필이 변경되어 SSH 업데이트를 중단했습니다.',
                                        code='profile_unavailable')
                    continue
                if (value.get('job') or {}).get('state') in ACTIVE:
                    self._launch(profile_id, alias, lambda p=profile_id, a=alias: self._queued_pass(p, a))
                elif self._stale(value, profile):
                    # A relaunch left a non-active reservation holding the SSH
                    # gate; retire it or hand it to review instead of waiting
                    # for the profile to be blocked forever.
                    self._launch(profile_id, alias, lambda p=profile_id, a=alias: self._retire_pass(p, a))
                elif (_stock_pending(value['stock']) or (value['auto_check'] and
                      self.clock() - value.get('_checked_epoch', 0) >= self.check_interval)):
                    self.check(profile_id, alias)
            except (ValueError, RuntimeError, OSError, KeyError):
                continue

    def _queued_pass(self, profile_id, alias):
        # A busy managed cohort must not starve a separately confirmed stock
        # install's read-only verification, including after service restart.
        if _stock_pending(self._read(profile_id, alias)['stock']):
            self._check(profile_id, alias)
        self.step(profile_id, alias)

    def _loop(self):
        while not self.stopping.is_set():
            try:
                self.tick()
            except (ValueError, RuntimeError, OSError, KeyError):
                pass
            self.wake.wait(self.interval)
            self.wake.clear()

    def shutdown(self):
        self.stopping.set()
        self.wake.set()
        if self.executor:
            self.executor.shutdown(wait=False)
