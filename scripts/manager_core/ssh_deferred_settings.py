"""Reconnect a verified old SSH cohort without claiming new settings applied.

Unsupported idle evidence never authorizes a stop, a new start, or promotion of
the desired policy. Only a wholly unmodified preflight may restore its original
bindings; partially applied lifecycle journals stay fenced for recovery.
"""
from copy import deepcopy
import json

from .model_settings import render_options
from .ssh_shim import validate_binding
from .store import atomic_json, now
from .updates import UpdateError


UNSUPPORTED_IDLE = frozenset(('remote_idle_binding_missing', 'remote_idle_status_unavailable',
                              'remote_idle_diagnostics_unavailable'))
DEFERRED_MESSAGE = ('SSH는 기존 설정으로 연결했습니다. 새 하위 에이전트·모델 설정은 SSH에 아직 적용되지 않았습니다. '
                    '기존 원격 실행이 안전한 재시작 확인을 지원하지 않아 적용을 보류합니다.')


def _unmodified(lease, profile):
    if (lease.get('ssh_only') is not True or lease.get('force_runtime_update')
            or lease.get('state') != 'held' or lease.get('profile_scope') != [profile['id']]
            or lease.get('target_revision') != profile['policy']['desired_revision']
            or len(lease.get('profiles', [])) != 1):
        return False
    entry = lease['profiles'][0]
    if (entry.get('profile_id') != profile['id'] or entry.get('generation') != profile.get('generation')
            or entry.get('state') != 'held' or not entry.get('remotes')):
        return False
    return all(record.get('state') in ('unobserved', 'observed')
               and not record.get('reinspect') and record.get('exited') is not True
               and not any(key in record for key in ('next_binding', 'exit_proof', 'started',
                                                      'target_policy_revision'))
               for record in entry['remotes'])


def resume_previous(store, maintenance, profile, lease, reason, *, journal_path):
    """Read-only remote identity checks, then guarded local publication.

Returns False when a lifecycle has already crossed its exit boundary. A true
result releases only SSH admission and keeps a visible pending-policy marker.
It never changes effective/launched policy revisions or remote files.
"""
    if reason not in UNSUPPORTED_IDLE or not _unmodified(lease, profile):
        return False
    records = lease['profiles'][0]['remotes']
    saved = {item['alias']: item for item in profile.get('remote_bindings', [])
             if item.get('prepared') is True}
    previous, processes = {}, {}
    path = store.directory / 'profiles' / profile['id'] / 'ssh-bindings.json'
    if path.is_symlink() or path.resolve() != path or path.stat().st_size > 256000:
        raise UpdateError('remote_binding_unknown', 'SSH 연결 설정 파일을 확인해야 합니다.')
    baseline = json.loads(path.read_text(encoding='utf-8'))
    journal_before = journal_path.read_bytes()
    if json.loads(journal_before) != lease:
        raise UpdateError('ssh_generation_changed', 'SSH 설정 적용 기록이 변경되었습니다.')
    if baseline.get('profile_id') != profile['id'] or baseline.get('generation') != profile.get('generation'):
        raise UpdateError('remote_generation_changed', 'SSH 실행이 변경되었습니다.')
    published = [validate_binding(item, profile['id']) for item in baseline.get('bindings', [])]
    if len({item['alias'] for item in published}) != len(published):
        raise UpdateError('remote_binding_changed', 'SSH 연결 설정이 중복되었습니다.')
    for record in records:
        binding = validate_binding(record['binding'], profile['id'])
        alias = binding['alias']
        if (alias in previous or alias != record.get('alias') or alias not in saved
                or validate_binding(saved[alias], profile['id']) != binding
                or record.get('active_binding', binding) != binding):
            return False
        matches = [item for item in published if item['alias'] == alias]
        if matches != [binding] and not (not matches and alias in baseline.get('pending_policy_hosts', [])):
            raise UpdateError('remote_binding_changed', 'SSH 연결 설정이 별도로 변경되었습니다.')
        observed = maintenance.request(binding, 'identity')
        process = observed.get('process')
        if (not process or observed.get('exited') is not False or process.get('revision') != binding['revision']
                or (record.get('process') is not None and record['process'] != process)):
            raise UpdateError('remote_process_changed', '기존 SSH 실행이 변경되어 연결 복구를 보류합니다.')
        previous[alias], processes[alias] = binding, process
    # state.lock serializes enrollment, policy edits and the generation guard.
    # Recheck every local input after network I/O; never reuse an old inspection
    # over a newer launch, binding, policy, or partially advanced journal.
    with store.locked():
        data = store.read()
        current = store.profile(profile['id'], data)
        gate = data.get('ssh_maintenance', {}).get(profile['id'], {})
        if (current.get('generation') != profile.get('generation') or current.get('removed_at')
                or current.get('view_only') or current['policy'] != profile['policy']
                or render_options(current) != render_options(profile)
                or current.get('remote_bindings') != profile.get('remote_bindings')
                or gate.get('transaction_id') != lease['transaction_id']
                or gate.get('generation') != profile.get('generation')
                or gate.get('target_revision') != lease['target_revision']
                or gate.get('state') not in ('held', 'attention')):
            raise UpdateError('ssh_generation_changed', 'SSH 설정 또는 실행이 변경되어 복구를 중지했습니다.')
        for other in (data.get('update_maintenance'), data.get('profile_maintenance', {}).get(profile['id'])):
            if other and other.get('state') != 'released':
                raise UpdateError('profile_maintenance', '다른 설정 적용이 끝난 뒤 SSH 연결을 복구합니다.')
        if (journal_path.read_bytes() != journal_before or path.is_symlink()
                or json.loads(path.read_text(encoding='utf-8')) != baseline):
            raise UpdateError('remote_binding_changed', 'SSH 설정 적용 기록이 변경되었습니다.')
        aliases = sorted(previous)
        updated = deepcopy(baseline)
        updated['bindings'] = [item for item in published if item['alias'] not in previous] + list(previous.values())
        updated['pending_policy_hosts'] = sorted(set(baseline.get('pending_policy_hosts', [])) | set(aliases))
        updated['deferred_policy_hosts'] = aliases
        retired = deepcopy(lease)
        retired.update(state='released', settings_deferred=True, deferred_reason=reason,
                       resumed_processes=processes)
        # Write-ahead retirement prevents any future retry from using this
        # preflight to stop the now-reconnected cohort. A crash keeps admission
        # held until a fresh transaction verifies these same inputs again.
        atomic_json(journal_path, retired)
        atomic_json(path, updated)
        def release(latest):
            latest['ssh_maintenance'][profile['id']].update(state='released', settings_deferred=True,
                code=reason, message=DEFERRED_MESSAGE, deferred_policy_hosts=aliases, updated_at=now())
        store.mutate(release)
    return True
