"""Select a preset on one already prepared SSH runtime without restarting it."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .remote import RemoteError
from .runtime_admin import AdminClient, AdminError
from .ssh_runtime_control import endpoint_id
from .ssh_shim import ALIAS, validate_binding


def canonical_host(value):
    for prefix in ('ssh:', 'remote-ssh-discovered:'):
        if isinstance(value, str) and value.startswith(prefix):
            alias = value[len(prefix):]
            if ALIAS.fullmatch(alias):
                return 'ssh:' + alias, alias
    raise ValueError('등록된 SSH 작업의 호스트를 선택하세요.')


def running_binding(center, profile, alias):
    """Read the current launch manifest, never substitute newly saved settings."""
    path = Path(center.store.directory) / 'profiles' / profile['id'] / 'ssh-bindings.json'
    if not profile.get('generation') or path.is_symlink() or not path.is_file() or path.stat().st_size > 256000:
        return None
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if value.get('profile_id') != profile['id'] or value.get('generation') != profile['generation']:
        return None
    matches = [item for item in value.get('bindings', []) if item.get('alias') == alias]
    if len(matches) != 1:
        return None
    binding = validate_binding(matches[0], profile['id'])
    # Full identity metadata is saved with the prepared definition. Do not merge
    # a different revision that might only be waiting for a future restart.
    saved = [item for item in profile.get('remote_bindings', [])
             if item.get('alias') == alias and item.get('revision') == binding['revision']]
    if len(saved) != 1 or saved[0].get('prepared') is not True:
        return None
    return {**saved[0], **binding}


def publish_connected_hosts(center, profile, reference=None):
    """Publish saved/default changes to connected hosts in this bounded request."""
    from .execution_preset_service import _selection_requires_preparation
    aliases = sorted({item['alias'] for item in profile.get('remote_bindings', [])
                      if isinstance(item, dict) and isinstance(item.get('alias'), str)
                      and ALIAS.fullmatch(item['alias'])})[:32]
    if not aliases:
        return {}

    def publish(alias):
        try:
            binding = running_binding(center, profile, alias)
            if binding is None or binding.get('execution_presets_version') != 1:
                return alias, 'prepare_required'
            admin = AdminClient(center.root, endpoint_id(profile['id'], alias), profile['generation'])
            observed = admin.request('manager/executionPresets/status',
                {'threadId': '00000000-0000-0000-0000-000000000000'}, timeout=1)
            if observed.get('version') != 1 or observed.get('sshBinding') != {
                    'profileId': profile['id'], 'hostAlias': alias, 'revision': binding['revision']}:
                return alias, 'prepare_required'
            current = center.store.profile(profile['id'])
            if current.get('generation') != profile.get('generation') or running_binding(center, current, alias) != binding:
                return alias, 'pending'
            result = center.remote.publish_execution_presets(current, binding)
            current = center.store.profile(profile['id'])
            if current.get('generation') != profile.get('generation') or running_binding(center, current, alias) != binding:
                return alias, 'pending'
            return alias, 'prepare_required' if _selection_requires_preparation(result, reference) else 'published'
        except (AdminError, RemoteError, OSError, ValueError, KeyError, TypeError):
            return alias, 'pending'

    # The request owns these workers and waits for them; no untracked daemon can
    # hold profile shutdown open or keep publishing after the request completes.
    with ThreadPoolExecutor(max_workers=min(4, len(aliases))) as executor:
        states = dict(executor.map(publish, aliases))
    return states


def dispatch(center, presets, command, profile, args):
    from .execution_preset_service import _selection_requires_preparation

    host_id, alias = canonical_host(args['host_id'])
    thread_id = args['thread_id']
    profile_id = profile['id']
    if command == 'presets.select':
        selected = presets.bind(profile_id, host_id, thread_id, args.get('preset_id'), args.get('revision')) or {}
    else:
        selected = presets.get_for_task(profile_id, host_id, thread_id) or {}
    result = {**selected, 'state': 'saved', 'observed_preset_known': False,
              'confirmation_pending': bool(selected),
              'message': 'SSH 작업의 다음 실행 설정입니다. 진행 중인 응답은 시작 당시 설정을 사용합니다.'}
    try:
        binding = running_binding(center, profile, alias)
    except (OSError, ValueError, KeyError, TypeError):
        binding = None
    if binding is None or binding.get('execution_presets_version') != 1:
        return {**result, 'state': 'prepare_required', 'runtime_prepare_required': True,
                'message': 'SSH 프리셋 준비가 필요합니다. 선택은 보관되며 현재 작업은 계속됩니다. 작업 종료 후 이 프로필의 SSH 설정을 적용하세요.'}
    admin = AdminClient(center.root, endpoint_id(profile_id, alias), profile['generation'])
    try:
        observed = admin.request('manager/executionPresets/status', {'threadId': thread_id})
        if observed.get('sshBinding') != {'profileId': profile_id, 'hostAlias': alias, 'revision': binding['revision']}:
            raise AdminError('stale_runtime')
        if observed.get('version') != 1:
            return {**result, 'state': 'prepare_required', 'runtime_prepare_required': True,
                    'message': '현재 SSH 실행기는 새 프리셋 기능을 아직 사용하지 않습니다. 작업 종료 후 SSH 업데이트를 적용하세요.'}
        if command == 'presets.status':
            applied, known = observed['executionPreset'], observed['known']
            if not selected and known:
                selected = {'preset_id': applied['id'] if applied else None,
                            'revision': applied['revision'] if applied else None, 'preset': None}
                if applied:
                    try:
                        selected['preset'] = presets.get(profile_id, applied['id'], applied['revision'])
                    except (ValueError, KeyError):
                        selected['preset'] = {'id': applied['id'], 'revision': applied['revision'], 'name': '저장된 조합'}
            desired = ({'id': selected['preset_id'], 'revision': selected['revision']}
                       if selected.get('preset_id') else None)
            return {**result, **selected, 'observed_preset_known': known, 'observed_preset': applied,
                    'confirmation_pending': bool(selected) and (not known or desired != applied)}
        published = center.remote.publish_execution_presets(profile, binding)
        if _selection_requires_preparation(published, selected):
            return {**result, **published, 'state': 'prepare_required', 'runtime_prepare_required': True,
                    'message': '선택을 저장했습니다. 새 조합은 작업 종료 후 SSH 설정을 적용하면 준비됩니다. 현재 작업은 계속됩니다.'}
        current = center.store.profile(profile_id)
        if current.get('generation') != profile.get('generation') or running_binding(center, current, alias) != binding:
            raise AdminError('stale_runtime')
        status = admin.request('thread/read', {'threadId': thread_id, 'includeTurns': False})
        if status.get('thread', {}).get('status', {}).get('type') == 'notLoaded':
            return {**result, **published, 'message': 'SSH 작업의 선택을 저장했습니다. 작업을 다시 열면 적용됩니다.'}
        reference = ({'id': selected['preset_id'], 'revision': selected['revision']}
                     if selected.get('preset_id') else None)
        admin.request('thread/settings/update', {'threadId': thread_id, 'executionPreset': reference})
        return {**result, **published, 'state': 'queued', 'confirmation_pending': True,
                'message': 'SSH 프리셋 전환을 요청했습니다. 다음 실행부터 적용하며 현재 응답과 하위 에이전트는 계속됩니다.'}
    except (AdminError, RemoteError, OSError, ValueError, KeyError, TypeError):
        return {**result, 'state': 'confirmation_pending', 'confirmation_pending': True,
                'message': '선택은 보관됐지만 SSH 실행기 반영을 확인하지 못했습니다. 연결이 복구되면 다시 선택하세요. 현재 작업은 유지됩니다.'}
