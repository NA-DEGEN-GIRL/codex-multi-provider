"""Local login preflight, without refreshing, copying, or changing credentials."""
from .proxy_auth import LoginNeededError, account_fingerprint, read_existing_tokens


def inspect(profile):
    native = profile.get('auth_mode') == 'native'
    if (profile.get('view_only') or profile.get('runtime_channel') == 'packaged'
            or (native and profile.get('native_login_pending'))):
        return dict(state='not_bound', blocks_launch=False)
    source = profile.get('home') if native else profile.get('source_home')
    if not source and profile.get('auth_mode') != 'source':
        return dict(state='not_bound', blocks_launch=False)
    try:
        if not source:
            raise LoginNeededError('unreadable_auth_source')
        # Check identity before expiry so an account switch is reported clearly.
        tokens = read_existing_tokens(source, now=0, minimum_validity=0)
        expected = profile.get('account_fingerprint')
        if expected and account_fingerprint(tokens.account_id) != expected:
            raise LoginNeededError('account_mismatch')
        # Native OAuth refresh belongs to this isolated runtime. A borrowed
        # access token, however, must still be valid when we start the runtime.
        if not native:
            read_existing_tokens(source)
    except LoginNeededError as error:
        changed = error.reason == 'account_mismatch'
        message = ('원본 앱의 로그인 계정이 이 프로필에 등록된 계정과 다릅니다.'
                   if changed and not native else
                   '저장된 로그인 계정이 이 프로필에 등록된 계정과 다릅니다.' if changed else
                   '이 프로필의 로그인 정보를 다시 확인해야 합니다.')
        return dict(state='account_changed' if changed else 'login_needed',
                    blocks_launch=True, reason=error.reason, action='profile.login',
                    message=message + ' ‘이 프로필에 로그인’에서 등록된 계정으로 로그인하세요.')
    return dict(state='ready', blocks_launch=False)


def require(profile):
    health = inspect(profile)
    if health['blocks_launch']:
        raise RuntimeError(f"{profile['alias']} · {health['message']}")
    return health
