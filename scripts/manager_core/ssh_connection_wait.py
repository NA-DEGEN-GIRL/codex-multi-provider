"""Keep SSH initialization off the local window launch path."""
import time

from .ssh_shim import ShimError, parse_invocation
from .store import Store


def wait_for_settings(arguments, manifest, *, store=None, clock=time.monotonic,
                      sleep=time.sleep, timeout=90):
    """Wait in the SSH child, before routing or enrolling a remote operation.

    The caller reloads the manifest afterward: a command must never use the
    pre-update revision it read before the remote settings were published.
    """
    invocation = parse_invocation(arguments)
    if invocation.configuration_only or not manifest.get('generation'):
        return
    store = store or Store(manifest['inventory_root'])
    profile_id, generation = manifest['profile_id'], manifest['generation']
    deadline = clock() + timeout
    while True:
        data = store.read()
        gate = data.get('ssh_maintenance', {}).get(profile_id, {})
        if not gate or gate.get('state') == 'released':
            return
        profile = store.profile(profile_id, data)
        if profile.get('removed_at') or profile.get('generation') != generation:
            raise ShimError('ssh_generation_changed', '프로필 실행이 바뀌었습니다. SSH 연결을 다시 열어 주세요.')
        if gate.get('generation') != generation:
            raise ShimError('ssh_generation_changed', 'SSH 설정을 적용할 프로필 실행이 바뀌었습니다.')
        if gate.get('state') != 'held':
            raise ShimError('ssh_settings_pending', 'SSH 설정을 준비하지 못했습니다. 로컬 작업은 계속 사용할 수 있습니다. 프로필을 다시 선택해 연결을 재시도하세요.')
        if clock() >= deadline:
            raise ShimError('ssh_settings_pending', 'SSH 설정을 백그라운드에서 준비 중입니다. 잠시 뒤 연결을 다시 시도하세요.')
        sleep(.1)
