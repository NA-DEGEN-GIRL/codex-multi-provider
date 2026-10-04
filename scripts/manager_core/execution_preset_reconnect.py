"""Publish offline preset changes before one managed SSH connection starts."""
from types import SimpleNamespace

from .execution_preset_remote_service import running_binding
from .remote import RemoteError, RemoteManager
from .store import Store


def publish_before_connect(source_environment, event, *, ssh_executable=None):
    """Synchronous metadata-only publication; never starts/restarts a runtime.

    Publication failure refuses this new connection instead of launching with a
    stale default. Other established transports and running tasks are untouched.
    """
    root = source_environment.get('CODEX_MANAGER_ROOT')
    generation = source_environment.get('CODEX_MANAGER_GENERATION')
    if not root or not generation or event.get('operation') != 'native-proxy':
        return {'state': 'not_required'}
    try:
        store = Store(root)
        center = SimpleNamespace(store=store)
        profile = store.profile(event['profile_id'])
        candidates = [item for item in profile.get('remote_bindings', [])
                      if item.get('alias') == event['alias']]
        if not any(item.get('execution_presets_version') == 1 for item in candidates):
            return {'state': 'not_required'}
        if profile.get('generation') != generation:
            raise ValueError('profile generation changed')
        binding = running_binding(center, profile, event['alias'])
        if binding is None or binding['revision'] != event['revision']:
            raise ValueError('SSH binding changed')
        if binding.get('execution_presets_version') != 1:
            return {'state': 'not_required'}
        # PATH inside the desktop starts with our SSH adapter. Re-entering it
        # with a metadata command is rejected as an unknown native wrapper.
        # The caller already validated the actual OpenSSH executable.
        published = RemoteManager(root, ssh_executable=ssh_executable).publish_execution_presets(profile, binding)
        if published.get('execution_presets_version') != 1 or published.get('published') is not True:
            raise ValueError('preset authority unavailable')
        current = store.profile(profile['id'])
        if current.get('generation') != generation or running_binding(center, current, event['alias']) != binding:
            raise ValueError('SSH binding changed during publication')
        # A new role can remain unprepared while independent ready tasks connect.
        # Its exact unresolved selector stays in the registry and fails closed.
        return {'state': 'published', 'runtime_prepare_required': bool(published.get('runtime_prepare_required'))}
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        raise RemoteError('ssh_preset_sync_required',
            'SSH 실행 프리셋 동기화를 확인하지 못했습니다. 연결을 다시 시도하세요. 기존 작업은 유지됩니다.') from None
