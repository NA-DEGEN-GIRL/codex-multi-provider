"""Remove/restore account entries while retaining their login and record store."""
from .store import label, now


def account_alias(value, profiles, *, excluding=None):
    value = label(value)
    if len(value) > 40:
        raise ValueError('SSH에서도 사용할 계정 별칭은 1~40자로 입력하세요.')
    if any(p['id'] != excluding and not p.get('removed_at') and p['alias'].casefold() == value.casefold()
           for p in profiles if not p.get('view_only')):
        raise ValueError('이미 사용 중인 계정 별칭입니다. 다른 이름을 입력하세요.')
    return value


class ProfileLifecycle:
    def __init__(self, store, instances):
        self.store, self.instances = store, instances

    def remove(self, profile_id):
        profile = self.store.profile(profile_id)
        if profile.get('view_only'):
            raise ValueError('전체 기록 보기 인스턴스는 계정 목록에서 제거할 수 없습니다.')
        if self.instances.observe(profile)['status'] == 'running':
            raise RuntimeError('이 계정의 Codex 창을 닫은 뒤 제거하세요. 실행 중인 작업은 그대로 유지했습니다.')
        def remove(data):
            current = self.store.profile(profile_id, data)
            if any(link['profile_id'] == profile_id for link in data['shortcuts']):
                raise ValueError('연결된 작업을 다른 계정으로 옮기거나 바로가기를 삭제한 뒤 계정을 제거하세요.')
            current['removed_at'] = now()
            if data.get('representative_profile_id') == profile_id:
                data['representative_profile_id'] = next((p['id'] for p in data['profiles']
                    if p['id'] != profile_id and not p.get('removed_at') and not p.get('view_only')), None)
            return dict(id=profile_id, removed=True, records_deleted=False, credentials_deleted=False,
                        message='계정을 목록에서 제거했습니다. 로그인과 대화는 보존되며 계정 복원으로 다시 표시할 수 있습니다.')
        return self.store.mutate(remove)

    def restore(self, profile_id, alias=None):
        def restore(data):
            profile = self.store.profile(profile_id, data)
            if not profile.get('removed_at'):
                raise ValueError('이미 목록에 있는 계정입니다.')
            profile['alias'] = account_alias(alias or profile['alias'], data['profiles'], excluding=profile_id)
            profile.pop('removed_at')
            if not data.get('representative_profile_id'):
                data['representative_profile_id'] = profile_id
            return dict(id=profile_id, restored=True, message='같은 로그인과 대화 기록으로 계정을 복원했습니다.')
        return self.store.mutate(restore)
