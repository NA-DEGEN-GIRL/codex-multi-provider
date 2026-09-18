"""Register the user's existing local login without moving its app or credentials."""
from pathlib import Path
from uuid import uuid4

from .profile_lifecycle import account_alias
from .proxy_auth import read_existing_tokens, account_fingerprint
from .store import Store, now


def register(store, alias, source):
    source = Path(source).resolve()
    fingerprint = account_fingerprint(read_existing_tokens(source).account_id)

    def save(data):
        existing = next((p for p in data['profiles'] if not p.get('removed_at')
                         and p.get('account_fingerprint') == fingerprint), None)
        name = account_alias(alias, data['profiles'], excluding=existing['id'] if existing else None)
        if existing:
            if existing['alias'] != name:
                raise ValueError('이 계정은 이미 등록되어 있습니다. 기존 프로필의 별칭 변경을 사용하세요.')
            return existing
        pid = str(uuid4())
        folder = store.directory / 'profiles' / pid
        profile = dict(id=pid, alias=name, alias_authority='manager', auth_mode='source',
                       source_home=str(source), account_fingerprint=fingerprint,
                       home=str(folder / 'codex'), ui_home=str(folder / 'ui'),
                       runtime_channel='managed', desired_runtime_channel='managed',
                       status='not_started', process_id=None, account_missing=False,
                       login_state='credential_saved', created_at=now(),
                       policy=dict(enabled=False, model_ids=[], desired_revision=0,
                                   effective_revision=None))
        data['profiles'].append(profile)
        Store._source(data, folder / 'codex', 'manager:' + pid, name)
        Store._source(data, source, 'current-local', name + ' · 원본 기록')
        if not data.get('representative_profile_id'):
            data['representative_profile_id'] = pid
        return profile

    return store.mutate(save)


def status(store, profile_id, *, verify_server=False, root=None, timeout=25):
    profile = store.profile(profile_id)
    tokens = read_existing_tokens(profile['source_home'])
    if account_fingerprint(tokens.account_id) != profile['account_fingerprint']:
        raise RuntimeError('원본 앱의 로그인 계정이 바뀌었습니다. 등록된 계정으로 자동 변경하지 않았습니다.')
    result = dict(state='credential_saved', server_verified=False,
                  account_fingerprint=profile['account_fingerprint'], alias=profile['alias'],
                  message='원본 앱의 현재 로그인에 연결되어 있습니다. 원본 로그인 파일은 유지됩니다.')
    if verify_server:
        from desktop_launch import find_app
        from .login_probe import verify, verification_runtime
        result.update(verify(root, verification_runtime(root, find_app()), tokens, timeout=timeout))
        if account_fingerprint(read_existing_tokens(profile['source_home']).account_id) != profile['account_fingerprint']:
            raise RuntimeError('확인 중 원본 앱의 계정이 변경되었습니다.')
        result.update(state='signed_in', server_verified=True, message='현재 계정의 서버 인증과 사용량 조회를 확인했습니다.')
        def save(data):
            current = store.profile(profile_id, data)
            if current.get('account_fingerprint') != profile['account_fingerprint']:
                raise RuntimeError('확인 중 프로필의 계정 연결이 변경되었습니다.')
            current.update(login_state='signed_in', login_verified_at=now())
            if 'usage' in result:
                current['usage'] = result['usage']
        store.mutate(save)
    return result
