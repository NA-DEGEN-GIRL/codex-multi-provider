"""Open a shortcut in the account selected by the user.

Shared execution opens the canonical task without transferring ownership or
stopping another profile. Older runtimes retain their original handoff/viewer
fallback until the managed instance is reopened with the new runtime.
"""
import time

from .store import identifier


CONNECT_CAPABILITIES = (
    'managed_store_binding', 'managed_close_idle',
    'managed_idle_status', 'managed_reload_binding',
)


def open_shortcut(center, shortcut_id, capabilities, *, expected_profile_id=None):
    link = next((item for item in center.store.read()['shortcuts']
                 if item['id'] == identifier(shortcut_id)), None)
    if link is None:
        raise ValueError('바로가기를 찾을 수 없습니다.')
    if expected_profile_id is not None and identifier(expected_profile_id) != link['profile_id']:
        return dict(state='blocked', access_mode='unavailable', reason='shortcut_profile_changed',
                    profile_id=identifier(expected_profile_id),
                    message='바로가기의 연결 프로필이 변경되었습니다. 목록을 확인한 뒤 다시 열어 주세요.')
    profile = center.store.profile(link['profile_id'])
    # An SSH task opens through the desktop's own thread link with its host id
    # (AppTransport.open_conversation); only shared execution supports it.
    remote = link.get('host_id', 'local') != 'local'

    foreign = link['source_store_id'] != 'manager:' + profile['id']
    if foreign and not capabilities.get('native_record_catalog'):
        return dict(state='blocked', access_mode='unavailable', profile_id=profile['id'],
                    message='이 대화의 공통 기록 기능이 아직 준비되지 않았습니다.')

    requested = time.perf_counter()
    with center.update_hooks.launch_admission(profile['id']):
        admitted = time.perf_counter()
        # The exact thread link below is already a scoped Electron activation.
        # A separate reopen first can reset an embedded Chromium window's frame.
        shown = center.instances.show(profile['id'], reopen_existing=False)
        profile = shown['profile']
        # Phases of this click for the shell's per-click record (milliseconds).
        launch = dict(state=shown.get('state', 'existing'),
                      admission_ms=round((admitted - requested) * 1000),
                      launch_ms=round((time.perf_counter() - admitted) * 1000))
        if capabilities.get('canonical_record_storage'):
            # The first launch may import old stores and update shortcut sources.
            link = next(item for item in center.store.read()['shortcuts'] if item['id'] == link['id'])
        connection = None
        read_only = foreign or profile.get('view_only', False)
        managed = profile.get('runtime_channel') != 'packaged' and not profile.get('view_only')
        if managed and capabilities.get('shared_record_execution'):
            # The user chooses the account directly. This mode does not transfer
            # ownership, wait for another profile, or close its running actors.
            return center.navigations.begin(link, profile, link, False, annotations=dict(launch=launch))
        if remote:
            # Handoff and read-only viewers only know local stores.
            return dict(state='blocked', access_mode='unavailable', profile_id=profile['id'],
                        reason='exact_remote_navigation_unverified', launch=launch,
                        message='SSH 작업은 공유 실행이 적용된 관리용 Codex에서 열 수 있습니다. 이 프로필을 다시 열어 주세요.')
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
                                 account_connection=connection, launch=launch))
        else:
            # Viewing a catalog needs the connected reader, not a complete idle
            # proof for stopping writers. AppTransport verifies that reader's
            # fresh generation below. An incomplete activity observer must not
            # put every view request behind a 30-second restart-health wait.
            if read_only and not shared:
                center._wait_runtime(profile['id'])
            return center.navigations.begin(link, profile, link, read_only,
                annotations=dict(account_connection=connection, launch=launch))
