"""Resume a stalled, untouched SSH preflight using its verified old settings.

No remote start/stop, configuration upload, or credential transfer is allowed.
Useful for an already-running older service that cannot load the fixed helper.
"""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
from uuid import uuid4

from manager_core.instances import process_identity
from manager_core.remote import RemoteManager
from manager_core.remote_maintenance import RemoteMaintenance
from manager_core.ssh_deferred_settings import DEFERRED_MESSAGE, UNSUPPORTED_IDLE, _unmodified, resume_previous
from manager_core.store import Store, atomic_json, identifier, now
from manager_core.updates import UpdateError


def recover(store, maintenance, profile_id):
    profile_id = identifier(profile_id)
    data = store.read()
    profile = store.profile(profile_id, data)
    gate = data.get('ssh_maintenance', {}).get(profile_id, {})
    job = data.get('profile_restarts', {}).get(profile_id, {})
    if (gate.get('state') not in ('held', 'attention') or job.get('remote_background') is not True
            or job.get('phase') not in ('waiting', 'attention')
            or job.get('transaction_id') != gate.get('transaction_id')
            or job.get('generation') != profile.get('generation')
            or gate.get('generation') != profile.get('generation')):
        raise UpdateError('ssh_recovery_not_pending', '복구할 SSH 설정 대기가 없습니다.')
    directory = store.directory / 'updates/maintenance'
    path = directory / (identifier(gate['transaction_id']) + '.json')
    lease = json.loads(path.read_text(encoding='utf-8'))
    if not _unmodified(lease, profile):
        raise UpdateError('ssh_recovery_lifecycle_started', '이미 설정 적용이 진행되어 이전 연결로 복구하지 않았습니다.')
    reason = None
    for record in lease['profiles'][0]['remotes']:
        observed = maintenance.request(record['binding'], 'inspect', discover_active=True, observe_only=True)
        if observed.get('observation_code') in UNSUPPORTED_IDLE:
            reason = observed['observation_code']
            break
    if reason is None:
        raise UpdateError('ssh_recovery_not_unsupported', '실제 작업 대기 또는 다른 원인이므로 기존 설정으로 바꾸지 않았습니다.')
    transaction_id, job_id = str(uuid4()), str(uuid4())
    replacement = deepcopy(lease)
    replacement.update(transaction_id=transaction_id, replaces_transaction=lease['transaction_id'],
                       previous_restart=deepcopy(job))
    new_path = directory / (transaction_id + '.json')
    worker = process_identity(os.getpid()) or {}
    # Revoke the old worker before publication. Its post-inspection generation
    # guard fails, and its id-guarded job writes cannot clobber this recovery.
    with store.locked():
        current = store.read()
        if (store.profile(profile_id, current) != profile
                or current.get('ssh_maintenance', {}).get(profile_id) != gate
                or current.get('profile_restarts', {}).get(profile_id, {}).get('id') != job.get('id')
                or json.loads(path.read_text(encoding='utf-8')) != lease):
            raise UpdateError('ssh_generation_changed', '확인 중 SSH 요청이 변경되었습니다. 다시 확인하세요.')
        for other in (current.get('update_maintenance'), current.get('profile_maintenance', {}).get(profile_id)):
            if other and other.get('state') != 'released':
                raise UpdateError('profile_maintenance', '다른 설정 적용이 진행 중입니다.')
        atomic_json(new_path, replacement)
        def claim(latest):
            latest['ssh_maintenance'][profile_id] = dict(gate, state='held', transaction_id=transaction_id, updated_at=now())
            latest['profile_restarts'][profile_id] = dict(job, id=job_id, phase='waiting',
                transaction_id=transaction_id, worker_pid=os.getpid(), worker_created=worker.get('process_created'),
                message='기존 SSH 연결을 확인하고 있습니다.', updated_at=now())
        store.mutate(claim)
    restored = False
    try:
        restored = resume_previous(store, maintenance, profile, replacement, reason, journal_path=new_path)
        if not restored:
            raise UpdateError('ssh_recovery_unverified', '기존 SSH 연결을 확인하지 못했습니다.')
        return dict(state='deferred', connections_restored=True,
                    aliases=sorted(r['alias'] for r in replacement['profiles'][0]['remotes']))
    finally:
        def finish(latest):
            active = latest.get('profile_restarts', {}).get(profile_id, {})
            if active.get('id') == job_id:
                active.update(phase='attention', connections_restored=restored,
                    code='ssh_settings_deferred' if restored else 'ssh_recovery_unverified',
                    message=DEFERRED_MESSAGE if restored else '기존 SSH 연결을 확인하지 못해 적용을 보류했습니다.',
                    updated_at=now())
            active_gate = latest.get('ssh_maintenance', {}).get(profile_id, {})
            if not restored and active_gate.get('transaction_id') == transaction_id:
                active_gate.update(state='attention', code='ssh_recovery_unverified', updated_at=now())
        store.mutate(finish)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--profile', required=True)
    parser.add_argument('--resume-previous-settings', required=True, action='store_true')
    args = parser.parse_args()
    root = args.root.resolve()
    store = Store(root)
    remote = RemoteManager(root, ssh_executable=Path(os.environ['WINDIR']) / 'System32/OpenSSH/ssh.exe')
    try:
        print(json.dumps(recover(store, RemoteMaintenance(root, store, remote), args.profile)))
    except (RuntimeError, ValueError, OSError, KeyError) as error:
        print(json.dumps(dict(state='attention', code=getattr(error, 'code', 'ssh_recovery_unverified'))))
        raise SystemExit(1)
