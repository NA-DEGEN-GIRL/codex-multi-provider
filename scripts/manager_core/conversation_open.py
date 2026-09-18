"""Open a shortcut in the account selected by the user.

Shared execution opens the canonical task without transferring ownership or
stopping another profile. Older runtimes retain their original handoff/viewer
fallback until the managed instance is reopened with the new runtime.
"""
from .store import identifier


CONNECT_CAPABILITIES = (
    'managed_store_binding', 'managed_close_idle',
    'managed_idle_status', 'managed_reload_binding',
)


def open_shortcut(center, shortcut_id, capabilities):
    link = next((item for item in center.store.read()['shortcuts']
                 if item['id'] == identifier(shortcut_id)), None)
    if link is None:
        raise ValueError('바로가기를 찾을 수 없습니다.')
    profile = center.store.profile(link['profile_id'])
    if link.get('host_id', 'local') != 'local':
        # Native Codex still owns ordinary SSH connection/project selection.
        # This guard concerns only an automatic exact host+thread shortcut.
        return dict(state='blocked', access_mode='unavailable', profile_id=profile['id'],
                    reason='exact_remote_navigation_unverified',
                    message='SSH 대화 바로가기의 화면 연결은 아직 검증 중입니다. 지정 프로필에서 원본 Codex의 SSH 프로젝트를 열 수 있습니다.')

    foreign = link['source_store_id'] != 'manager:' + profile['id']
    if foreign and not capabilities.get('native_record_catalog'):
        return dict(state='blocked', access_mode='unavailable', profile_id=profile['id'],
                    message='이 대화의 공통 기록 기능이 아직 준비되지 않았습니다.')

    with center.update_hooks.launch_admission(profile['id']):
        # The exact thread link below is already a scoped Electron activation.
        # A separate reopen first can reset an embedded Chromium window's frame.
        shown = center.instances.show(profile['id'], reopen_existing=False)
        profile = shown['profile']
        if capabilities.get('canonical_record_storage'):
            # The first launch may import old stores and update shortcut sources.
            link = next(item for item in center.store.read()['shortcuts'] if item['id'] == link['id'])
        connection = None
        read_only = foreign or profile.get('view_only', False)
        managed = profile.get('runtime_channel') != 'packaged' and not profile.get('view_only')
        if managed and capabilities.get('shared_record_execution'):
            # The user chooses the account directly. This mode does not transfer
            # ownership, wait for another profile, or close its running actors.
            return center.navigations.begin(link, profile, link, False)
        if (managed and link['source_store_id'].startswith('manager:')
                and all(capabilities.get(key) for key in CONNECT_CAPABILITIES)):
            center._wait_runtime(profile['id'])
            # The engine rechecks activity and holds recorder locks before any
            # owner change. It never interrupts a running turn or sends input.
            connection = center.handoffs.continue_conversation(link, profile['id'])
            read_only = connection.get('status') != 'ready'
            profile = {**center.store.profile(profile['id']),
                       **center.instances.observe(center.store.profile(profile['id']))}

        shared = managed and capabilities.get('shared_record_catalog')
        if read_only and not shared:
            if not capabilities.get('native_record_catalog'):
                return dict(state='blocked', access_mode='unavailable', profile_id=profile['id'],
                            message='이 대화의 공통 기록 기능이 아직 준비되지 않았습니다.')
            result = center.dispatch('catalog.show', {'profile_id': profile['id']})
            viewer = result.get('profile')
            if not viewer:
                return result
            return center.navigations.begin(link, viewer, {**link, 'profile_id': viewer['id']}, True,
                annotations=dict(readonly_viewer=True, representative_profile_id=profile['id'],
                                 account_connection=connection))
        else:
            # Viewing a catalog needs the connected reader, not a complete idle
            # proof for stopping writers. AppTransport verifies that reader's
            # fresh generation below. An incomplete activity observer must not
            # put every view request behind a 30-second restart-health wait.
            if read_only and not shared:
                center._wait_runtime(profile['id'])
            return center.navigations.begin(link, profile, link, read_only,
                annotations=dict(account_connection=connection))
