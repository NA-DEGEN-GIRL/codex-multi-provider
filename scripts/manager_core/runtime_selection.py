"""Describe the actual running binary separately from the next launch selection."""
import json
from pathlib import Path

from .runtime_build import resolve
from .store import identifier


def describe(root, profile, *, identity, selected=None):
    root = Path(root).resolve()
    selected = selected or resolve(root)
    desired = Path(selected['runtime']).resolve()
    result = dict(state='not_running', selected_release=desired.parent.name,
                  current_release=None, restart_required=False,
                  message='다음 실행에 최신 관리 런타임을 적용합니다.')
    if profile.get('status') != 'running':
        return result
    result.update(state='unknown', message='현재 실행 중인 런타임 버전을 확인하고 있습니다.')
    if profile.get('runtime_channel') == 'packaged':
        result.update(state='packaged', current_release='기본 로그인용',
                      restart_required=profile.get('desired_runtime_channel') == 'managed',
                      message='로그인용 창 실행 중 · 공통 대화는 이 프로필을 종료하고 다시 열면 표시됩니다.'
                              if profile.get('desired_runtime_channel') == 'managed'
                              else '로그인용 창 실행 중 · 로그인 상태 확인 후 공통 대화를 연결합니다.')
        return result
    try:
        pid, generation = identifier(profile['id']), identifier(profile['generation'])
        observer = profile.get('runtime_state', {})
        if observer.get('generation') != generation:
            return result
        descriptor = root / 'work/control-center/instances' / pid / 'runtime-admin' / (generation + '.json')
        if descriptor.is_symlink() or descriptor.resolve() != descriptor or descriptor.stat().st_size > 16384:
            return result
        data = json.loads(descriptor.read_text(encoding='utf-8'))
        expected = data.get('runtime', {})
        if (data.get('profile_id') != pid or data.get('generation') != generation
                or expected.get('pid') != observer.get('runtime_process_id')):
            return result
        actual = identity(expected.get('pid'))
        if not actual or actual.get('process_created') != expected.get('created'):
            return result
        binary = Path(actual['executable_path']).resolve()
        releases = root / 'artifacts/manager-runtime/releases'
        if binary.name != 'codex.exe' or binary.parent.parent != releases:
            return result
        adapter_changed = observer.get('shared_catalog', {}).get('enabled') is True and observer.get('record_edits_version', 0) < 1
        observer_changed = observer.get('runtime_observer_version', 0) < 2
        execution_changed = selected.get('capabilities', {}).get('shared_record_execution') and observer.get('shared_execution_version', 0) < 1
        changed = binary != desired or adapter_changed or observer_changed or execution_changed
        result.update(state='outdated' if changed else 'current', current_release=binary.parent.name,
            restart_required=changed, runtime_pid=expected['pid'],
            message='이전 런타임 실행 중 · 최신 수정 적용을 위해 이 프로필을 재시작하세요.' if changed
                    else '최신 관리 런타임 실행 중')
        if adapter_changed and binary == desired:
            result['message'] = '작업 이름 변경 기능 적용 대기 · 이 관리 프로필을 한 번 재시작하면 적용됩니다.'
        if observer_changed and binary == desired:
            result['message'] = '실행 상태 확인 수정 적용 대기 · 이 관리용 Codex를 다음에 다시 열 때 적용됩니다.'
        if execution_changed:
            result['message'] = '공통 작업 편집 기능 적용 대기 · 왼쪽의 ‘이 프로필 다시 열기’로 새 버전을 적용하세요.'
        return result
    except (OSError, ValueError, KeyError, TypeError):
        return result
