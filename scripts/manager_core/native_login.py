"""Onboard accounts in their own Windows HOME using the installed app login."""
from pathlib import Path
import tomllib
import time

from .store import now
from .proxy_auth import read_existing_tokens,account_fingerprint,LoginNeededError


class NativeLogin:
    def __init__(self,root,store,instances):
        self.root=Path(root).resolve();self.store=store;self.instances=instances

    def shared_login_ready(self):
        from .runtime_build import resolve
        capabilities=resolve(self.root).get('capabilities',{})
        return all(capabilities.get(key) for key in ('canonical_record_storage','managed_store_binding'))

    def migrate_verified_shared_history(self):
        """Select shared history on the next launch for older verified logins.

        Early profiles were verified before shared history existed. They lack a
        desired channel, so another login verification was previously necessary.
        Do not change an explicit login-in-progress choice or restart any process.
        """
        from .runtime_build import resolve
        from .native_account_guard import NativeAccountGuard
        capabilities = resolve(self.root).get('capabilities', {})
        if self.shared_login_ready():
            # Retire the separate login-only record store on the next cold open.
            # A running login window and its unsent input are left untouched.
            def pending(profile):
                return (profile.get('auth_mode')=='native' and profile.get('runtime_channel')=='packaged'
                        and profile.get('desired_runtime_channel')!='managed'
                        and not profile.get('removed_at') and not profile.get('view_only'))
            if not any(pending(p) for p in self.store.read()['profiles']):return []
            def migrate(data):
                changed=[]
                for item in data['profiles']:
                    if pending(item):
                        item.update(desired_runtime_channel='managed',native_login_pending=True)
                        item.pop('post_login_runtime_channel',None)
                        changed.append(item['id'])
                return changed
            return self.store.mutate(migrate)
        if not (capabilities.get('shared_record_catalog') and capabilities.get('managed_store_binding')):
            return []
        def eligible(profile):
            return (profile.get('auth_mode') == 'native' and profile.get('runtime_channel') == 'packaged'
                    and profile.get('desired_runtime_channel') is None
                    and profile.get('login_state') == 'signed_in' and profile.get('login_verified_at')
                    and profile.get('account_fingerprint') and not profile.get('removed_at')
                    and not profile.get('view_only'))
        if not any(eligible(p) for p in self.store.read()['profiles']):
            return []
        def save(data):
            selected = []
            for profile in data['profiles']:
                if not eligible(profile):
                    continue
                home, _ = self.instances.paths(profile)
                if not NativeAccountGuard({'CODEX_HOME': str(home),
                    'CODEX_MANAGER_EXPECTED_ACCOUNT_FINGERPRINT': profile['account_fingerprint']}).matches():
                    continue
                profile['desired_runtime_channel'] = 'managed'
                selected.append(profile['id'])
            return selected
        return self.store.mutate(save)

    def prepare(self,profile_id):
        profile=self.store.profile(profile_id)
        if profile.get('removed_at'):raise ValueError('목록에서 제거한 계정을 먼저 복원하세요.')
        if profile.get('view_only'):raise ValueError('전체 기록 보기 프로필은 로그인 계정으로 전환할 수 없습니다.')
        running=self.instances.observe(profile)['status']=='running'
        if running and (profile.get('auth_mode')!='native' or
                        (profile.get('runtime_channel')!='packaged' and not profile.get('native_login_pending'))):
            raise RuntimeError('이 프로필의 기존 창을 닫은 뒤 로그인 준비를 다시 실행하세요.')
        channel='managed' if self.shared_login_ready() else 'packaged'
        home,ui=self.instances.paths(profile)
        if not running:
            home.mkdir(parents=True,exist_ok=True);ui.mkdir(parents=True,exist_ok=True)
            config=home/'config.toml'
            if not config.exists():
                config.write_text('cli_auth_credentials_store = "file"\nforced_login_method = "chatgpt"\nmodel = "gpt-6-astra"\n',encoding='utf-8')
            else:
                if config.is_symlink():raise ValueError('로그인 설정 파일이 다른 경로에 연결되어 있습니다.')
                values=tomllib.loads(config.read_text(encoding='utf-8-sig'))
                if values.get('cli_auth_credentials_store')!='file':
                    raise RuntimeError('독립 로그인 저장소 설정을 먼저 확인해야 합니다.')
            self.instances.prepare({**profile,'runtime_channel':channel,'auth_mode':'native'})
        def save(data):
            item=self.store.profile(profile_id,data)
            if item.get('auth_mode')!='native':
                item['previous_account_reference']=dict(usage_account_id=item.get('usage_account_id'),
                                                        source_home=item.get('source_home'))
                # Repair the registered account in its own HOME. Do not turn a
                # source-login repair into a silent reassignment of this alias.
                item['usage']=dict(windows=[],observed_at=None,freshness='unknown',error=None)
                item['login_state']='awaiting_user'
                item.pop('login_verified_at',None)
            if item.get('runtime_channel')=='managed' or item.get('desired_runtime_channel')=='managed':
                item['post_login_runtime_channel']='managed'
            item.update(auth_mode='native',alias_authority='manager',runtime_channel=channel,
                        desired_runtime_channel=channel,native_login_pending=(channel=='managed'),
                        account_missing=False,login_prepared_at=now())
            item.setdefault('login_state','awaiting_user')
            return item
        return self.store.mutate(save)

    def complete_shared_login(self,profile):
        """Observe saved login locally; no probe, window replacement or navigation."""
        if (profile.get('auth_mode')!='native' or profile.get('runtime_channel')!='managed'
                or not profile.get('native_login_pending')):
            return profile
        observer=profile.get('runtime_state',{})
        binding=observer.get('native_account',{})
        if (not profile.get('generation') or observer.get('generation')!=profile['generation']
                or not binding.get('matches') or not binding.get('account_fingerprint')):
            return profile
        status=self.status(profile['id'])
        if (status['state'] in ('credential_saved','signed_in')
                and binding['account_fingerprint']==status.get('account_fingerprint')):
            def complete(data):
                item=self.store.profile(profile['id'],data)
                if item.get('account_fingerprint')==status.get('account_fingerprint'):
                    item.pop('native_login_pending',None)
                    item.pop('post_login_runtime_channel',None)
                    item['desired_runtime_channel']='managed'
                return item
            return self.store.mutate(complete)
        return self.store.profile(profile['id'])

    def open(self,profile_id):
        existing = self.store.profile(profile_id)
        if self.instances.observe(existing)['status'] == 'running':
            status = self.status(profile_id)
            if status['state'] in ('credential_saved', 'signed_in'):
                return {**self.instances.show(profile_id), 'login': status,
                        'message': '이 프로필의 로그인된 Codex 창을 표시했습니다.'}
        profile = self.prepare(profile_id)
        return {**self.instances.show(profile_id),'message':f"{profile['alias']} 전용 Codex 창에서 등록된 계정으로 로그인하세요. 원본 앱의 로그인은 유지됩니다."}

    def _unavailable(self,profile,state,message):
        def save(data):
            item=self.store.profile(profile['id'],data)
            item['login_state']=state
            usage=item.setdefault('usage',{})
            usage.update(freshness='stale' if usage.get('windows') else 'unknown',
                         error=dict(code=state,message=message))
        if profile.get('login_state')!=state or profile.get('usage',{}).get('freshness')=='live':
            self.store.mutate(save)
        return dict(state=state,alias=profile['alias'],server_verified=False,message=message)

    def status(self,profile_id):
        profile=self.store.profile(profile_id)
        if profile.get('auth_mode')!='native':
            if profile.get('source_home') and profile.get('account_fingerprint'):
                from .login_health import inspect
                health = inspect(profile)
                if health['blocks_launch']:
                    return dict(state=health['state'], alias=profile['alias'], server_verified=False,
                                message=health['message'])
                from .current_account import status
                return status(self.store, profile_id)
            return dict(state='unverified',alias=profile['alias'],message='독립 프로필 로그인을 먼저 준비하세요.')
        home,_=self.instances.paths(profile)
        path=home/'auth.json'
        if not path.exists():
            return self._unavailable(profile,'signed_out','아직 이 프로필에 로그인이 저장되지 않았습니다.')
        if path.is_symlink() or path.resolve().parent!=home or path.stat().st_nlink!=1:
            raise RuntimeError('독립 로그인 파일의 경로를 확인하지 못했습니다.')
        try:tokens=read_existing_tokens(home,minimum_validity=0)
        except LoginNeededError:
            return self._unavailable(profile,'unverified','로그인 정보가 만료되었거나 확인이 필요합니다.')
        fingerprint=account_fingerprint(tokens.account_id)
        if profile.get('account_fingerprint') and profile['account_fingerprint']!=fingerprint:
            return self._unavailable(profile,'account_changed','저장된 계정이 변경되었습니다. 별칭과 연결을 다시 확인해야 합니다.')
        def save(data):
            item=self.store.profile(profile_id,data)
            item.update(account_fingerprint=fingerprint,login_observed_at=now())
            if item.get('login_state')!='signed_in':item['login_state']='credential_saved'
        self.store.mutate(save)
        if profile.get('login_state')=='signed_in' and profile.get('login_verified_at'):
            return dict(state='signed_in',alias=profile['alias'],account_fingerprint=fingerprint,
                        server_verified=True,verified_at=profile['login_verified_at'],
                        message='저장된 계정이 이전 서버 인증 확인 때와 같습니다.')
        return dict(state='credential_saved',alias=profile['alias'],account_fingerprint=fingerprint,
                    server_verified=False,message=('로그인 정보가 저장되었으며 이 창에서 공통 대화를 사용할 수 있습니다.'
                        if profile.get('runtime_channel')=='managed' else
                        '이 프로필에 ChatGPT 로그인이 저장되었습니다. 서버 연결 확인은 다음 단계입니다.'))

    def verify(self,profile_id,*,timeout=25):
        status=self.status(profile_id)
        if status['state'] not in ('credential_saved','signed_in'):return status
        profile=self.store.profile(profile_id)
        if profile.get('auth_mode') != 'native':
            from .current_account import status as source_status
            return source_status(self.store, profile_id, verify_server=True, root=self.root, timeout=timeout)
        home,_=self.instances.paths(profile)
        tokens=read_existing_tokens(home)
        fingerprint=account_fingerprint(tokens.account_id)
        if fingerprint!=status['account_fingerprint']:raise RuntimeError('확인 중 로그인 계정이 변경되었습니다.')
        from desktop_launch import cached_app
        from .login_probe import verify,verification_runtime
        app=cached_app();result=verify(self.root,verification_runtime(self.root,app),tokens,timeout=timeout)
        if account_fingerprint(read_existing_tokens(home).account_id)!=fingerprint:
            raise RuntimeError('확인 중 로그인 계정이 변경되었습니다.')
        from .runtime_build import resolve
        shared_ready=resolve(self.root).get('capabilities',{}).get('shared_record_catalog') is True
        def save(data):
            item=self.store.profile(profile_id,data)
            if item.get('account_fingerprint')!=fingerprint:raise RuntimeError('확인 중 계정 연결이 변경되었습니다.')
            proof={key:value for key,value in result.items() if key!='usage'}
            item.update(login_state='signed_in',login_verified_at=now(),native_login_verification=proof)
            if item.pop('post_login_runtime_channel',None)=='managed' or shared_ready:
                item['desired_runtime_channel']='managed'
            if 'usage' in result:item['usage']=result['usage']
        self.store.mutate(save)
        return dict(state='signed_in',alias=profile['alias'],account_fingerprint=fingerprint,**result,
                    message='04 전용 로그인 계정의 서버 인증과 사용량 조회를 확인했습니다.' if profile['alias']=='04' else '로그인 계정의 서버 인증과 사용량 조회를 확인했습니다.')

    def refresh_all(self):
        profiles=[p for p in self.store.read()['profiles'] if p.get('auth_mode') in ('native', 'source') and not p.get('view_only') and not p.get('removed_at')]
        deadline=time.monotonic()+45
        results=[]
        for profile in profiles:
            remaining=deadline-time.monotonic()
            try:
                if remaining<5:raise RuntimeError('refresh budget exhausted')
                if profile.get('auth_mode') == 'source':
                    from .current_account import status
                    value=status(self.store, profile['id'], verify_server=True, root=self.root, timeout=min(20,remaining))
                else:
                    value=self.verify(profile['id'],timeout=min(20,remaining))
                results.append(dict(id=profile['id'],state=value['state'],refreshed=value.get('quota_read') is True))
            except (ValueError,RuntimeError,OSError):
                def stale(data):
                    usage=self.store.profile(profile['id'],data).setdefault('usage',{})
                    usage.update(freshness='stale',error=dict(code='refresh_failed',message='로그인 또는 연결 상태를 확인하세요.'))
                self.store.mutate(stale)
                results.append(dict(id=profile['id'],state='check_required',refreshed=False))
        return dict(accounts=len(profiles),refreshed=sum(r['refreshed'] for r in results),results=results)
