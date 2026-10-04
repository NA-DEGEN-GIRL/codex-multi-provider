"""Manager UI operations for saved presets; never restart an active profile."""
from .execution_presets import ExecutionPresets
from .runtime_admin import AdminClient, AdminError
from .store import identifier


def _selection_requires_preparation(published, reference):
    if not published.get('runtime_prepare_required'):
        return False
    if 'unprepared_presets' not in published:
        return True  # No trusted union is installed yet.
    if not reference:
        return False
    return any(item['id'] == reference['preset_id'] and item['revision'] == reference['revision']
               for item in published['unprepared_presets'])


def _publish_hosts(center, profile, result, reference=None):
    from .execution_preset_remote_service import publish_connected_hosts
    hosts = publish_connected_hosts(center, profile, reference)
    if not hosts:
        return result
    result['remote_publication'] = hosts
    if any(state != 'published' for state in hosts.values()):
        result['message'] = result.get('message', '프리셋을 저장했습니다.') + ' 일부 SSH 연결은 준비 또는 연결 후 반영이 필요합니다.'
    else:
        result['message'] = result.get('message', '프리셋을 저장했습니다.') + ' 연결된 SSH 호스트에도 반영했습니다.'
    return result


def dispatch(center, command, args):
    presets = ExecutionPresets(center.store, center.providers)
    profile_id = identifier(args['profile_id'])
    profile = center.store.profile(profile_id)
    if command == 'presets.list':
        return presets.list(profile_id)
    if command == 'presets.save':
        result = presets.save(profile_id, args['preset'], args.get('expected_revision'))
        prepared = presets.publish_registry(profile_id)
        prepared['runtime_prepare_required'] = _selection_requires_preparation(
            prepared, {'preset_id': result['id'], 'revision': result['revision']})
        return _publish_hosts(center, profile, {**result, **prepared, 'message': (
            '프리셋을 저장했습니다. 새 실행 조합은 이 프로필을 다음에 열 때 준비됩니다. 현재 작업은 계속됩니다.'
            if prepared.get('runtime_prepare_required') else
            '프리셋을 저장했습니다. 작업 상단에서 선택하면 다음 실행부터 사용합니다.')},
            {'preset_id': result['id'], 'revision': result['revision']})
    if command == 'presets.delete':
        result = presets.delete(profile_id, args['preset_id'], args.get('expected_revision'))
        presets.publish_registry(profile_id)
        return _publish_hosts(center, profile, result)
    if command == 'presets.default':
        result = presets.set_default(profile_id, args.get('preset_id'), args.get('revision'))
        prepared = presets.publish_registry(profile_id)
        prepared['runtime_prepare_required'] = _selection_requires_preparation(prepared, result.get('default'))
        return _publish_hosts(center, profile, {**result, **prepared, 'message': (
            '새 작업 기본값을 저장했습니다. 새 실행 조합은 이 프로필을 다음에 열 때 준비됩니다.'
            if prepared.get('runtime_prepare_required') else
            '새 작업에 사용할 기본 프리셋을 저장했습니다. 기존 작업의 설정은 유지됩니다.')}, result.get('default'))
    if command not in ('presets.select', 'presets.status'):
        raise ValueError('지원하지 않는 프리셋 작업입니다.')
    thread_id = identifier(args['thread_id'])
    host_id = args.get('host_id', 'local')
    if host_id != 'local':
        from .execution_preset_remote_service import dispatch as remote_dispatch
        return remote_dispatch(center, presets, command, profile, {**args, 'thread_id': thread_id})
    if command == 'presets.status':
        result = presets.get_for_task(profile_id, host_id, thread_id) or {}
        observed = center.instances.observe(profile).get('runtime_state', {}).get('execution_presets', {})
        known = thread_id in observed
        applied = observed.get(thread_id)
        # A new task can inherit its default directly in the runtime, before the
        # manager has an explicit per-task binding. Preserve known-null too.
        if not result and known:
            result = {'preset_id': applied['id'] if applied else None,
                      'revision': applied['revision'] if applied else None, 'preset': None}
            if applied:
                try:
                    result['preset'] = presets.get(profile_id, applied['id'], applied['revision'])
                except (ValueError, KeyError):
                    result['preset'] = {'id': applied['id'], 'revision': applied['revision'], 'name': '저장된 조합'}
        result.pop('runtime_prepare_required', None)
        desired = ({'id': result['preset_id'], 'revision': result['revision']}
                   if result.get('preset_id') else None)
        return {**result, 'observed_preset_known': known, 'observed_preset': applied,
                'confirmation_pending': bool(result) and (not known or applied != desired),
                'message': '이 작업에 선택한 다음 실행 설정입니다. 진행 중인 응답은 시작 당시 설정을 사용합니다.'}
    selected = presets.bind(profile_id, host_id, thread_id, args.get('preset_id'), args.get('revision')) or {}
    published = presets.publish_registry(profile_id)
    published['runtime_prepare_required'] = _selection_requires_preparation(published, selected)
    if published.get('runtime_prepare_required'):
        return {**selected, **published, 'state': 'prepare_required',
                'message': '선택을 저장했습니다. 새 실행 조합은 이 프로필을 다음에 열 때 준비됩니다. 현재 작업은 계속됩니다.'}
    result = {**selected, **published, 'state': 'saved',
              'message': '선택을 저장했습니다. 이 작업을 다시 열면 선택한 프리셋으로 실행합니다.'}
    observed = center.instances.observe(profile)
    if observed.get('status') != 'running' or not profile.get('generation'):
        return result
    if observed.get('runtime_state', {}).get('execution_presets_version') != 1:
        return {**result, 'state': 'prepare_required', 'runtime_prepare_required': True,
                'message': '프리셋을 저장했습니다. 이 프로필은 이전 실행기를 사용 중입니다. 작업을 마친 뒤 새 버전으로 다시 열면 적용됩니다.'}
    reference = {'id': selected['preset_id'], 'revision': selected['revision']} if selected.get('preset_id') else None
    try:
        admin = AdminClient(center.root, profile_id, profile['generation'])
        status = admin.request('thread/read', {'threadId': thread_id, 'includeTurns': False})
        if status.get('thread', {}).get('status', {}).get('type') == 'notLoaded':
            return result
        admin.request('thread/settings/update', {'threadId': thread_id, 'executionPreset': reference})
    except AdminError as error:
        return {**result, 'state': 'confirmation_pending' if error.uncertain else 'saved',
                'message': '선택은 저장됐지만 실행기에 반영됐는지 아직 확인하지 못했습니다. 현재 작업은 유지됩니다.',
                'confirmation_pending': True}
    return {**result, 'state': 'queued',
            'message': '프리셋 전환을 요청했습니다. 다음 실행부터 적용하며, 진행 중인 응답과 하위 에이전트는 계속됩니다.'}
