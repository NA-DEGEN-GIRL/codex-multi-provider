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
    delay = .1
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
        if gate.get('state') == 'held' and gate.get('graceful_drain') and gate.get('waiting_hosts'):
            raise ShimError('ssh_remote_drain_pending',
                '이전 요청에 따른 원격 Codex 종료를 기다리고 있습니다. 관리 창의 원격 종료 상태를 확인해 주세요. '
                '네트워크 연결 시간 초과가 아니며, 종료 확인 전에는 이 프로필의 SSH를 다시 연결하지 않습니다.')
        if gate.get('state') != 'held':
            raise ShimError('ssh_settings_pending', 'SSH 설정을 준비하지 못했습니다. 로컬 작업은 계속 사용할 수 있습니다. 프로필을 다시 선택해 연결을 재시도하세요.')
        if clock() >= deadline:
            raise ShimError('ssh_settings_pending', 'SSH 설정을 백그라운드에서 준비 중입니다. 잠시 뒤 연결을 다시 시도하세요.')
        # Several profiles each launch multiple SSH probes at startup. Avoid
        # rereading the complete shared state 10 times/second for every child.
        sleep(min(delay, max(0, deadline - clock())))
        delay = min(1.0, delay * 2)
