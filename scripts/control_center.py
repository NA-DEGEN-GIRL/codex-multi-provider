"""Codex Control Center backend. JSON RPC plus an opt-in loopback skill worker."""
import argparse
import json
import os
from pathlib import Path
import sys
import threading
import time

from manager_core.store import Store, identifier, label, now
from manager_core.accounts import Accounts
from manager_core.catalog import list_catalog, sort_conversations
from manager_core.catalog_pages import page as catalog_page, legacy_view as catalog_legacy_view
from manager_core.remote_catalog import RemoteCatalog
from manager_core.providers import ProviderRegistry
from manager_core.instances import Instances
from manager_core.remote import RemoteManager
from manager_core.updates import UpdateManager
from manager_core.update_jobs import UpdateJobs
from manager_core.update_hooks import UpdateHooks
from manager_core.runtime_build import resolve as runtime_build
from manager_core.handoff import HandoffManager
from manager_core.native_login import NativeLogin
from manager_core.profile_lifecycle import ProfileLifecycle, account_alias
from manager_core.profile_restart import ProfileRestarts
from manager_core.ssh_inventory import SshInventory
from manager_core.remote_maintenance import RemoteMaintenance
from manager_core.remote_updates import RemoteUpdates

ROOT=Path(__file__).resolve().parents[1]


class ControlCenter:
    def __init__(self,root=ROOT,*,supervisor_protocol=None):
        self.root=Path(root).resolve(); self.store=Store(self.root)
        self.supervisor_protocol=supervisor_protocol
        self.providers=ProviderRegistry(self.root)
        self.accounts=Accounts(self.root)
        # The standalone login helper has no window host. Only the desktop
        # supervisor may request hidden startup for later in-manager attachment.
        self.instances=Instances(self.root,self.store,self.providers,
                                 embed_windows=bool(supervisor_protocol and supervisor_protocol > 0))
        self.remote=RemoteManager(self.root)
        self.remote_catalog=RemoteCatalog(self.root,self.store,self.remote)
        self.ssh_inventory=SshInventory(self.root)
        self.remote_maintenance=RemoteMaintenance(self.root,self.store,self.remote)
        self.update_hooks=UpdateHooks(self.root,self.store,self.instances,host_inventory=self.ssh_inventory.coverage,
                                      remote_maintenance=self.remote_maintenance)
        self.instances.launch_admission=self.update_hooks.launch_admission
        from manager_core.pending_navigation import PendingNavigations
        self.navigations=PendingNavigations(self)
        self.instances.authorize_restoration_generation=self.update_hooks.authorize_restoration_generation
        self.restarts=ProfileRestarts(self.store,self.instances,self.update_hooks)
        from manager_core.startup_updates import StartupUpdates
        self.startup_updates=StartupUpdates(self.root,self.store,self.instances,self.restarts)
        self.updates=UpdateManager(self.root,**self.update_hooks.callbacks())
        self.update_jobs=UpdateJobs(self.updates)
        self.remote_updates=RemoteUpdates(self.root,self.store,self.remote,self.update_hooks)
        self.catalog_refresh=self.instances.catalog_refresh
        self.source_catalog_refresh=self.instances.source_catalog_refresh
        self.handoffs=HandoffManager(self.root,self.store,self.instances)
        self.native_login=NativeLogin(self.root,self.store,self.instances)
        self.native_login.migrate_verified_shared_history()
        self._remote_reconcile_started = False
        from manager_core.usage_refresh import UsageRefresh
        self.usage_refresh=UsageRefresh(self.root,self.store)
        self.profile_lifecycle=ProfileLifecycle(self.store,self.instances)
        self._sync_at=0
        self._mutex=threading.RLock()
        self._request_gates={}
        self._request_gate_lock=threading.Lock()
        self.personal_skills=self.instances.personal_skills
        from manager_core.skill_bridge import SkillBridge
        self.skill_bridge=SkillBridge(self.root,self.store,self.personal_skills,self.remote)
        self.shared_plugins=self.instances.shared_plugins
        from manager_core.profile_warmup import ProfileWarmup
        self.profile_warmup=ProfileWarmup(self.store,self.instances,self._open_profile_locally)

    def _open_profile_locally(self, profile_id):
        profile=self.store.profile(profile_id)
        observed=self.instances.observe(profile)
        ssh_gate=self.store.read().get('ssh_maintenance',{}).get(profile_id,{})
        if ssh_gate.get('remote_update') and ssh_gate.get('state') != 'released':
            if observed.get('status')=='running':
                return dict(profile_id=profile_id,profile={**profile,**observed},state='existing')
            return self.instances.show(profile_id,reopen_existing=False)
        if ssh_gate.get('state') not in (None,'released') or (
                observed.get('status')!='running' and self.remote_maintenance.pending_on_open(profile)):
            return self.restarts.open_local(profile_id)
        if observed.get('status')=='running':
            # A warmed window needs only native attachment. Re-running Electron
            # here adds a second-instance round trip and can steal foreground.
            return dict(profile_id=profile_id,profile={**profile,**observed},state='existing')
        return self.instances.show(profile_id,reopen_existing=False)

    def catalog_sources(self):
        capabilities=runtime_build(self.root).get('capabilities',{})
        if not capabilities.get('native_record_catalog'):return None
        if (capabilities.get('mixed_source_catalog')
                and not (self.root/'work/control-center/catalog/local-records.json').exists()):
            return None
        return self.store.read()['sources'],capabilities.get('paginated_record_catalog',False)

    def native_catalog_sources(self):
        capabilities=runtime_build(self.root).get('capabilities',{})
        if not capabilities.get('mixed_source_catalog'):return None
        return self.store.read()['sources'],True

    def current_catalog_refresh(self, capabilities):
        return self.source_catalog_refresh if capabilities.get('mixed_source_catalog') else self.catalog_refresh

    def instance_snapshot(self):
        return [{**p,**self.instances.observe(p)} for p in self.store.read()['profiles']]

    def _handoff_for(self, host_id):
        if host_id == 'local': return self.handoffs
        if not isinstance(host_id,str) or not host_id.startswith('ssh:'):
            raise ValueError('지원하는 작업 호스트가 아닙니다.')
        from manager_core.remote_handoff import RemoteHandoffManager
        return RemoteHandoffManager(self.root,self.store,self.remote,host_id[4:])

    def _wait_runtime(self,profile_id,timeout=30):
        from manager_core.runtime_admin import AdminClient,AdminError
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            profile=self.store.profile(profile_id)
            try:
                health=AdminClient(self.root,profile_id,profile['generation']).request('manager/maintenance/status',{},timeout=2)
                if all(health.get(key) is True for key in ('initialized','connected','streamComplete','accountReady')):
                    return
            except (AdminError,OSError,ValueError,KeyError):pass
            if self.instances.observe(profile)['status']!='running':break
            time.sleep(.25)

    def _prepare_remote(self, profile, alias, models, **options):
        """Enroll preparation so an SSH update's atomic gate can wait for it."""
        generation = profile.get('generation')
        with self.store.locked():
            state = self.store.read()
            gate = state.get('ssh_maintenance', {}).get(profile['id'], {})
            if gate.get('state') not in (None, 'released'):
                from manager_core.updates import UpdateError
                raise UpdateError('ssh_settings_pending', 'SSH 업데이트가 진행 중입니다. 완료 후 연결 설정을 준비해 주세요.')
            if generation and not state.get('ssh_inventory', {}).get(profile['id']):
                # Use this Store's reentrant transaction; the inventory owns a
                # different Store object and cannot reacquire this file lock.
                self.store.mutate(lambda data: data.setdefault('ssh_inventory', {}).update({profile['id']:
                    dict(generation=generation, adapter='tracked-ssh-v1', hosts=[], operations={},
                         unclassified=False, updated_at=now())}))
        if generation:
            with self.ssh_inventory.execution(profile['id'], generation, dict(operation='prepare', alias=alias)):
                return self.remote.prepare(alias, profile['id'], profile['home'], models, **options)
        return self.remote.prepare(alias, profile['id'], profile['home'], models, **options)

    def _reconcile_remote_hosts(self):
        """Observe old bindings without installing a new runtime/helper release.

        Startup may backfill exact settings evidence for legacy metadata. Runtime
        updates belong to the managed update queue; configuration changes use the
        existing explicit profile apply/SSH preparation path.
        """
        try:
            state = self.store.read()
        except (ValueError, RuntimeError, OSError):
            return
        for profile in state.get('profiles', []):
            if profile.get('removed_at') or profile.get('view_only'):
                continue
            if state.get('ssh_maintenance', {}).get(profile['id'], {}).get('state') not in (None, 'released'):
                continue
            for binding in profile.get('remote_bindings') or []:
                if not binding.get('alias') or binding.get('prepared') is not True:
                    continue
                try:
                    self.remote_maintenance.verify_settings(profile, binding)
                except (ValueError, RuntimeError, OSError, KeyError):
                    continue

    def state(self):
        notices=[]
        if time.monotonic()-self._sync_at>30:
            try:self.accounts.sync(self.store)
            except (ValueError,RuntimeError,OSError):notices.append('llm-usage 연결을 확인하지 못했습니다. 등록된 프로필은 유지합니다.')
            self._sync_at=time.monotonic()
        state=self.store.read()
        if not self._remote_reconcile_started:
            self._remote_reconcile_started = True
            threading.Thread(target=self._reconcile_remote_hosts, daemon=True,
                             name='codex-remote-reconcile').start()
        if not any(Path(s['home']).resolve()==(Path.home()/'.codex').resolve() for s in state['sources']):
            def register(data):
                Store._source(data,Path.home()/'.codex','original:local','기존 Codex')
            self.store.mutate(register);state=self.store.read()
        # Forked tasks copy their source memo into an independent document; the
        # note service reads this mapping instead of opening state databases.
        try:
            from manager_core.note_forks import refresh as refresh_note_forks
            refresh_note_forks(self.root,state)
        except (ValueError,RuntimeError,OSError):
            pass
        state['profile_restarts']=self.restarts.status()
        state['startup_updates']=self.startup_updates.status()
        state['remote_updates']=self.remote_updates.status_all()
        state['profile_warmup']=self.profile_warmup.status()
        state['local_launches']=self.instances.launch_status()
        registry=self.providers.list()
        from manager_core.runtime_selection import describe as describe_runtime
        from manager_core.instances import process_identity
        selected_runtime=runtime_build(self.root)
        state['providers']=registry.get('providers',[]);state['models']=registry.get('models',[])
        state['removed_profiles']=[dict(id=p['id'],alias=p['alias'],removed_at=p['removed_at']) for p in state['profiles'] if p.get('removed_at')]
        state['profiles']=[p for p in state['profiles'] if not p.get('removed_at')]
        for p in state['profiles']:
            p['restart']=state['profile_restarts'].get(p['id'],{})
            p.update(self.instances.observe(p))
            completed=self.native_login.complete_shared_login(p)
            # Keep the fresh process/observer fields while applying saved login
            # metadata. This never reopens or navigates a running desktop.
            for key in ('account_fingerprint','login_state','login_observed_at','desired_runtime_channel'):
                if key in completed:p[key]=completed[key]
            if not completed.get('native_login_pending'):p.pop('native_login_pending',None)
            p['runtime_selection']=describe_runtime(self.root,p,identity=process_identity,selected=selected_runtime)
            p.setdefault('usage',dict(windows=[],observed_at=None,error=None))
            p['account_verification']='not_verified'
            observer=p.get('runtime_state',{})
            binding=observer.get('auth_binding',{})
            if binding.get('state')=='ready' and binding.get('bound'):
                p['account_verification']='verified'
                if binding.get('account_fingerprint') and not p.get('account_fingerprint'):
                    fingerprint=binding['account_fingerprint']
                    self.store.mutate(lambda data:self.store.profile(p['id'],data).update(account_fingerprint=fingerprint))
            if observer.get('initialized') and observer.get('stream_complete'):
                p['policy']['effective_revision']=p['policy'].get('launched_revision')
            if p.get('auth_mode')=='external':
                model=next((m for m in registry['models'] if m['id']==p.get('external_model_id')), {})
                p['external_model_name']=model.get('display_name', '모델 확인 필요')
                p['status_message']='외부 API · '+p['external_model_name']
                p['account_verification']='api_key'
            elif p.get('auth_mode')=='native':
                p['status_message']='직접 로그인 · 기본 런타임' if p.get('runtime_channel')=='packaged' else '직접 로그인'
                p['account_verification']=p.get('login_state','awaiting_user')
            elif p.get('auth_mode') == 'source':
                p['status_message'] = '현재 앱 로그인 연결 · 별도 작업 창'
                p['account_verification'] = p.get('login_state', 'credential_saved')
            elif p.get('usage_account_id') and p['account_verification']!='verified':
                p['status_message']='연결 계정 확인 대기' if p['status']=='running' else 'llm-usage 계정 연결'
        state['updates']=self.update_jobs.status()
        self.usage_refresh.schedule()
        from manager_core.native_usage import presentation
        for p in state['profiles']:
            p['usage'] = presentation(self.usage_refresh.value(p), refreshing=self.usage_refresh.active(p['id']))
        state['hosts']=self.remote.list_hosts()
        state['view_instances']=[p for p in state['profiles'] if p.get('view_only')]
        state['profiles']=[p for p in state['profiles'] if not p.get('view_only')]
        built=runtime_build(self.root).get('capabilities',{})
        shared_running=built.get('shared_record_catalog') and any(p['status']=='running' and p.get('runtime_channel')!='packaged' for p in state['profiles'])
        catalog_refresh=self.current_catalog_refresh(built)
        if (state['view_instances'] and built.get('native_record_catalog')) or shared_running:
            catalog_refresh.refresh(state['sources'],include_paginated=built.get('paginated_record_catalog',False))
        state['catalog_refresh']=catalog_refresh.status()
        state['capabilities']=dict(original_gui_hosting=True,exact_navigation='request_then_verify',
                                   native_catalog=built.get('native_record_catalog',False),
                                   shared_native_catalog=built.get('shared_record_catalog',False),
                                   paginated_catalog=built.get('paginated_record_catalog',False),
                                   cross_account_continue=all(built.get(name,False) for name in (
                                       'managed_store_binding','managed_close_idle','managed_idle_status','managed_reload_binding')),
                                   cross_account_hosts=['local'],automatic_restart='verified_local_scope',
                                   ssh_external='native_transport_requires_host_preparation',provider_protocols=['responses'])
        state['notices']=notices
        return state

    def dispatch(self,command,args):
        if not isinstance(args,dict):raise ValueError('명령 인수가 올바르지 않습니다.')
        if command=='notes.refresh_forks':
            if args['task'].get('host_id', 'local').startswith(('ssh:', 'remote-ssh-discovered:')):
                from manager_core.note_aliases import refresh
                refresh(self.root, args['task'])
                return dict(refreshed=True)
            from manager_core.note_forks import refresh
            refresh(self.root, self.store.read(), thread_id=args['task']['thread_id'])
            return dict(refreshed=True)
        if command=='state':return self.state()
        if command=='skills.personal.list':return self.skill_bridge.decorate(self.personal_skills.list())
        if command=='skills.bridge.set':return self.skill_bridge.set(args['id'],args['enabled'])
        if command in ('skills.personal.set','skills.personal.delete','skills.personal.restore'):
            if command=='skills.personal.set':result=self.personal_skills.set(args['skill_id'],args['enabled'])
            elif command=='skills.personal.delete':result=self.personal_skills.delete(args['skill_id'])
            else:result=self.personal_skills.restore(args['deleted_id'])
            self.skill_bridge.refresh()
            return self.skill_bridge.decorate(result)
        if command=='manager.startup':
            result=self.startup_updates.start(retry_failed=args.get('retry_failed') is True)
            if getattr(self.instances,'embed_windows',False):
                self.profile_warmup.start()
            return result
        if command=='manager.stop_warmup':
            self.instances.stop_launches()
            self.profile_warmup.shutdown()
            return self.profile_warmup.status()
        if command=='manager.resume_launches':
            self.instances.resume_launches()
            return self.instances.launch_status()
        if command=='profile.cleanup':
            from manager_core import rust_service
            profile=self.store.profile(identifier(args['profile_id']))
            if profile.get('generation')!=identifier(args['generation']):
                raise ValueError('프로필 실행이 변경되어 이전 종료 요청을 취소했습니다.')
            if not rust_service.enabled():
                raise RuntimeError('프로세스 정리를 위한 관리 서비스가 연결되지 않았습니다.')
            return rust_service.request('process.stop',profile_id=profile['id'],generation=profile['generation'])
        if command=='manager.recover_legacy':return self.startup_updates.recover_legacy(
            args.get('profiles'), interrupt_running_work=args.get('interrupt_running_work') is True)
        if command=='accounts.refresh':
            result=self.accounts.refresh_live(self.store)
            if any(p.get('auth_mode') in ('native', 'source') for p in self.store.read()['profiles']):
                native=self.native_login.refresh_all()
                result['native_accounts']=native
                result['message']=f"Windows 로그인 계정 {native['accounts']}개 중 {native['refreshed']}개의 최신 사용량을 확인했습니다."
            self._sync_at=time.monotonic();return result
        if command=='accounts.list':return dict(accounts=self.accounts.list())
        if command=='profile.add':
            kind=args.get('kind','codex')
            if kind not in ('codex','external'):raise ValueError('프로필 종류를 선택하세요.')
            if kind=='external':
                model_id=args['model_id']
                # Validate adapter, model and decryptable key before creating a profile.
                self.providers.environment([model_id]).clear()
                from manager_core.model_settings import resolve
                model=next(m for m in self.providers.list()['models'] if m['id']==model_id)
                settings=resolve(model,args.get('settings'))
                return self.store.add_profile(account_alias(args['alias'],self.store.read()['profiles']), external_model_id=model_id, external_settings=settings)
            profile=self.store.add_profile(account_alias(args['alias'],self.store.read()['profiles']))
            return self.native_login.prepare(profile['id'])
        if command=='profile.register_current':
            from manager_core.current_account import register
            return register(self.store, args['alias'], Path.home()/'.codex')
        if command=='profile.remove':
            with self.update_hooks.launch_admission(args['profile_id']):return self.profile_lifecycle.remove(args['profile_id'])
        if command=='profile.restore':return self.profile_lifecycle.restore(args['profile_id'],args.get('alias'))
        if command=='profile.move':return self.store.move_profile(args['profile_id'],args['target_profile_id'],args['position'])
        if command=='profile.restart':return self.restarts.schedule(args['profile_id'])
        if command=='profile.recover':
            from manager_core.profile_recovery import stop_profile
            return stop_profile(self.store, self.instances, args['profile_id'],
                                expected_generation=args['expected_generation'],
                                interrupt_running_work=args.get('interrupt_running_work') is True)
        if command=='profile.login':
            if self.store.profile(args['profile_id']).get('auth_mode')=='external':raise ValueError('외부 API 프로필은 공급자 설정에서 API 키를 관리합니다.')
            with self.update_hooks.launch_admission(args['profile_id']):return self.native_login.open(args['profile_id'])
        if command=='profile.login_status':
            if self.store.profile(args['profile_id']).get('auth_mode')=='external':return dict(state='api_key',message='외부 API 프로필 · 공급자 설정에서 API 키를 관리합니다.')
            if self.store.profile(args['profile_id']).get('auth_mode') == 'source':
                from manager_core.current_account import status
                return status(self.store, args['profile_id'], verify_server=args.get('verify_server') is True, root=self.root)
            return self.native_login.verify(args['profile_id']) if args.get('verify_server') is True else self.native_login.status(args['profile_id'])
        if command=='profile.rename':
            def rename(data):
                p=self.store.profile(args['profile_id'],data)
                if p.get('usage_account_id') and p.get('alias_authority')!='manager':raise ValueError('llm-usage에 연결한 별칭은 llm-usage에서 변경하세요.')
                p['alias']=account_alias(args['alias'],data['profiles'],excluding=p['id'])
                for source in data['sources']:
                    if source['id']=='manager:'+p['id']:source['alias']=p['alias']
                return p
            updated=self.store.mutate(rename)
            updated['message']='프로필 별칭을 변경했습니다.'
            return updated
        if command=='profile.bind':
            uid=identifier(args['usage_account_id'])
            account=next((a for a in self.accounts.list() if a['id']==uid),None)
            if not account:raise ValueError('llm-usage 계정을 찾을 수 없습니다.')
            def bind(data):
                p=self.store.profile(args['profile_id'],data)
                if p.get('auth_mode')=='native':raise ValueError('직접 로그인한 계정은 별칭만으로 기존 계정에 연결하지 않습니다.')
                if self.instances.observe(p)['status']=='running':raise ValueError('실행 중에는 연결 계정을 변경할 수 없습니다.')
                if any(x['id']!=p['id'] and x.get('usage_account_id')==uid for x in data['profiles']):
                    raise ValueError('이미 연결된 계정입니다. 해당 프로필을 선택하세요.')
                p.update(usage_account_id=uid,alias=account['alias'],source_home=account['home'],account_missing=False)
                Store._source(data,account['home'],'usage:'+uid,account['alias']);return p
            return self.store.mutate(bind)
        if command=='profile.prepare':return self.instances.prepare(self.store.profile(args['profile_id']))
        if command=='profile.show':
            profile=self.store.profile(args['profile_id'])
            observed=self.instances.observe(profile)
            warmup=self.profile_warmup.status()
            pending=next((p for p in warmup['profiles'] if p['profile_id']==profile['id']),None)
            if observed.get('status')!='running' and warmup['worker_active'] and (
                    not warmup['profiles'] or pending and pending['state'] in ('queued','checking','opening')):
                self.profile_warmup.prioritize(profile['id'])
                self.update_hooks.prioritize_launch(profile['id'])
                return dict(profile_id=profile['id'], profile={**profile,**observed},
                            state='opening')
            return self._open_profile_locally(profile['id'])
        if command=='shortcut.add':
            host=args.get('host_id','local');source_id=args['source_store_id']
            discovered=self.remote_catalog.shortcut_source(host,source_id,identifier(args['thread_id'])) if host.startswith('ssh:') and source_id.startswith('legacy:') else None
            return self.store.shortcut_add(args['alias'],args['profile_id'],args['thread_id'],host,source_id,catalog_source=discovered)
        if command=='shortcut.move':return self.store.shortcut_move(args['shortcut_id'],args['profile_id'])
        if command=='shortcut.rename':return self.store.shortcut_rename(args['shortcut_id'],args['alias'])
        if command=='shortcut.delete':return self.store.shortcut_delete(args['shortcut_id'])
        if command=='shortcut.undo':return self.store.shortcut_undo()
        if command=='handoff.preview':
            link=next((x for x in self.store.read()['shortcuts'] if x['id']==identifier(args['shortcut_id'])),None)
            if not link:raise ValueError('바로가기를 찾을 수 없습니다.')
            handoffs=self._handoff_for(link['host_id'])
            built=runtime_build(self.root).get('capabilities',{})
            if not all(built.get(key) for key in ('managed_store_binding','managed_close_idle','managed_reload_binding')):
                return dict(status='blocked',message='계정 간 이어하기 런타임을 아직 검증·배포 중입니다.',
                            blockers=[dict(code='runtime_not_verified',message='검증된 인계 런타임 배포가 필요합니다.')])
            return handoffs.preview(link,link['profile_id'])
        if command=='catalog.list':
            state=self.store.read()
            local=list_catalog(state['sources'], limit=None)
            remote=self.remote_catalog.snapshot(state)
            local['conversations']=sort_conversations([*local['conversations'],*remote['conversations']])
            local['remote_hosts']=remote['hosts']
            local['possibly_truncated'] |= any(host['possibly_truncated'] for host in remote['hosts'])
            if not args:
                return catalog_legacy_view(local)
            from manager_core.remote_catalog import groups
            scope = dict(local=[(s['id'],s['home']) for s in state['sources'] if s.get('host_id')=='local'],
                         remote={alias: group['scope'] for alias,group in groups(state).items()})
            return catalog_page(local,args,scope)
        if command=='catalog.resolve':
            viewer=self.store.profile(identifier(args['viewer_profile_id']))
            built=runtime_build(self.root).get('capabilities',{})
            shared=built.get('shared_record_catalog') and viewer.get('runtime_channel')!='packaged'
            if not viewer.get('view_only') and not shared:raise ValueError('공통 기록을 조회할 수 있는 프로필이 아닙니다.')
            from manager_core.shared_catalog import selected_path
            from manager_core.source_catalog import catalog_path, resolve as resolve_source
            path=Path(viewer.get('record_catalog_path') or selected_path(self.root,viewer,built))
            projection=identifier(args['projection_thread_id'])
            if path==catalog_path(self.root):
                from manager_core.runtime_admin import AdminClient
                thread=AdminClient(self.root,viewer['id'],viewer['generation']).request(
                    'thread/read',{'threadId':projection,'includeTurns':False})['thread']
                if thread.get('id')!=projection:raise ValueError('조회한 대화의 ID가 일치하지 않습니다.')
                resolved=resolve_source(self.root,path,self.store.read()['sources'],thread)
                return {**resolved,'representative_profile_id':viewer.get('representative_profile_id',viewer['id'])}
            expected=self.root/'work/control-center/catalog/local-records.json'
            if path.resolve(strict=True)!=expected or path.is_symlink() or path.stat().st_size>4*1024*1024:
                raise ValueError('전체 기록 연결 파일을 확인할 수 없습니다.')
            catalog=json.loads(path.read_text(encoding='utf-8'))
            projection=identifier(args['projection_thread_id'])
            matches=[entry for entry in catalog.get('entries',[]) if entry.get('projectionThreadId')==projection]
            if catalog.get('version')!=1 or catalog.get('hostId')!='local' or len(matches)!=1:
                raise ValueError('화면의 대화를 원본 기록 하나에 연결할 수 없습니다.')
            entry=matches[0]
            source=next((s for s in self.store.read()['sources'] if s['id']==entry['sourceStoreId'] and s['host_id']==entry['hostId']),None)
            if not source or Path(source['home']).resolve()!=Path(entry['codexHome']).resolve():
                raise ValueError('원본 기록의 등록 출처가 일치하지 않습니다.')
            return dict(thread_id=identifier(entry['threadId']),source_store_id=source['id'],host_id=source['host_id'],
                        source_alias=source['alias'],projection_thread_id=projection,
                        representative_profile_id=viewer.get('representative_profile_id',viewer['id']))
        if command=='catalog.show':
            parent_id=args.get('profile_id') or self.store.read()['representative_profile_id']
            parent=self.store.profile(parent_id)
            if parent.get('account_missing'):raise ValueError('대표 계정 연결을 먼저 확인하세요.')
            built=runtime_build(self.root).get('capabilities',{})
            if built.get('shared_record_execution') and not parent.get('view_only'):
                return {**self.instances.show(parent_id), 'readonly_viewer': False,
                        'representative_profile_id': parent_id,
                        'message': parent['alias'] + ' 프로필에서 공통 작업 목록을 열었습니다.'}
            if not built.get('native_record_catalog'):
                return dict(state='blocked',message='원본 화면의 전체 기록 조회 런타임을 아직 빌드·검증 중입니다.')
            catalog=self.current_catalog_refresh(built).ensure(self.store.read()['sources'],include_paginated=built.get('paginated_record_catalog',False))
            from uuid import uuid5,UUID
            vid=str(uuid5(UUID(parent_id),'codex-control-center-record-viewer-v1'))
            def viewer(data):
                profile=next((p for p in data['profiles'] if p['id']==vid),None)
                if profile is None:
                    directory=self.store.directory/'profiles'/vid
                    profile=dict(id=vid,alias='전체 기록 · '+parent['alias'],usage_account_id=None,
                                 home=str(directory/'codex'),ui_home=str(directory/'ui'),
                                 status='not_started',process_id=None,source_home=parent.get('source_home') or parent['home'],
                                 policy=dict(enabled=False,model_ids=[],desired_revision=0,effective_revision=None),
                                 view_only=True,representative_profile_id=parent_id,record_catalog_path=catalog['path'])
                    data['profiles'].append(profile)
                profile['alias']='전체 기록 · '+parent['alias']
                if parent.get('account_fingerprint'):
                    profile['account_fingerprint']=parent['account_fingerprint']
                profile['catalog_status']={k:v for k,v in catalog.items() if k not in ('mapping','path')}
                return profile
            self.store.mutate(viewer)
            result=self.instances.show(vid)
            return {**result,'readonly_viewer':True,'representative_profile_id':parent_id,
                    'catalog':{k:v for k,v in catalog.items() if k not in ('mapping','path')}}
        if command in ('conversation.open','conversation.continue'):
            from manager_core.conversation_open import open_shortcut
            return open_shortcut(self,args['shortcut_id'],runtime_build(self.root).get('capabilities',{}))
        if command=='conversation.navigate':
            return self.navigations.resume(args['navigation_id'])
        if command=='profile.model_settings':
            from manager_core.model_settings import resolve, render_options
            target=self.store.profile(args['profile_id'])
            if target.get('auth_mode')!='external':raise ValueError('외부 API 프로필을 선택하세요.')
            model=next(m for m in self.providers.list()['models'] if m['id']==target['external_model_id'])
            settings=resolve(model,args['settings'])
            preview={**target,'external_settings':settings}
            config=Path(target['home'])/'config.toml'
            self.providers.render_for_host(target['home'],target['policy']['enabled'],target['policy']['model_ids'],
                config.read_text(encoding='utf-8-sig') if config.is_file() else '',**render_options(preview))
            def save_settings(data):
                p=self.store.profile(target['id'],data)
                p['external_settings']=settings
                p['policy']['desired_revision']+=1
                return p
            p=self.store.mutate(save_settings)
            result=self.instances.prepare(p)
            if self.instances.observe(p)['status']=='running':
                result['restart']=self.restarts.schedule(p['id'])
            return {**result,'settings':settings,'message':'모델 설정을 저장했습니다. 진행 중인 작업이 끝나면 적용하고, 닫힌 프로필은 다음 실행부터 사용합니다.'}
        if command=='policy.set':
            enabled=args['enabled'];models=args.get('model_ids',[])
            if not isinstance(enabled,bool) or not isinstance(models,list):raise ValueError('모델 선택 값이 올바르지 않습니다.')
            selected=list(dict.fromkeys(identifier(m) for m in models))
            registered={m['id']:m for m in self.providers.list()['models']}
            if any(m not in registered for m in selected):raise ValueError('등록된 모델을 선택하세요.')
            target=self.store.profile(args['profile_id'])
            config=Path(target['home'])/'config.toml'
            from manager_core.model_settings import render_options
            selection_mode=args.get('selection_mode','automatic')
            options=render_options(target);options['selection_mode']=selection_mode
            self.providers.render_for_host(target['home'],enabled,selected,config.read_text(encoding='utf-8-sig') if config.is_file() else '',**options)
            def policy(data):
                p=self.store.profile(args['profile_id'],data)
                p['policy'].update(enabled=enabled,model_ids=selected,selection_mode=selection_mode,desired_revision=p['policy']['desired_revision']+1)
                if p.get('auth_mode')=='native':p['desired_runtime_channel']='managed'
                return p
            p=self.store.mutate(policy)
            result=self.instances.prepare(p)
            if self.instances.observe(p)['status']=='running':
                job=self.restarts.schedule(p['id'])
                return {**result,'profile_id':p['id'],'policy':p['policy'],'restart':job,
                        'message':'설정을 저장했습니다. 작업이 끝나면 이 프로필을 자동으로 다시 엽니다.'}
            return dict(profile_id=p['id'],policy=p['policy'],**result)
        if command=='providers.list':return self.providers.list()
        if command=='providers.save':return self.providers.save(args['provider'],args['model'])
        if command=='providers.key':
            self.providers.save_key(args['provider_id'],args['key']);return dict(saved=True,message='API 키를 이 Windows 사용자용으로 암호화해 저장했습니다.')
        if command=='providers.verify':return self.providers.verify(args['model_id'])
        if command=='updates.check':return self.update_jobs.check()
        if command=='updates.prepare':return self.updates.prepare()
        if command=='updates.apply':
            if self.supervisor_protocol is not None and self.supervisor_protocol < 7:
                return dict(status='blocked', code='supervisor_update_required', worker_active=False,
                            message='백그라운드 업데이트를 지원하는 새 관리 앱 실행 파일이 필요합니다. 관리 앱을 새 빌드로 다시 열어 주세요.')
            return self.update_jobs.schedule()
        if command=='remote.list':return dict(hosts=self.remote.list_hosts())
        if command.startswith('remote.updates.'):
            profile_id,alias=args['profile_id'],args['alias']
            if command=='remote.updates.status':return self.remote_updates.status(profile_id,alias)
            if command=='remote.updates.check':return self.remote_updates.check(profile_id,alias)
            if command=='remote.updates.settings':return self.remote_updates.settings(profile_id,alias,
                auto_check=args.get('auto_check'),auto_apply=args.get('auto_apply'))
            if command=='remote.updates.schedule':return self.remote_updates.schedule(profile_id,alias)
            if command=='remote.updates.cancel':return self.remote_updates.cancel(profile_id,alias)
            if command=='remote.updates.stock_update':return self.remote_updates.update_stock(profile_id,alias,
                confirmed=args.get('confirmed') is True,observation_id=args.get('observation_id'))
        if command=='remote.inspect':return self.remote.inspect(args['alias'])
        if command=='remote.prepare':
            p=self.store.profile(args['profile_id'])
            from manager_core.model_settings import render_options
            result=self._prepare_remote(p,args['alias'],p['policy']['model_ids'] if p['policy']['enabled'] else [],
                **render_options(p))
            if result.get('prepared'):
                def bind_remote(data):
                    profile=self.store.profile(p['id'],data)
                    if (profile.get('generation') != p.get('generation')
                            or profile['policy']['desired_revision'] != p['policy']['desired_revision']
                            or data.get('ssh_maintenance',{}).get(p['id'],{}).get('state') not in (None,'released')):
                        return
                    bindings=[b for b in profile.get('remote_bindings',[]) if b.get('alias')!=args['alias']]
                    bindings.append(result);profile['remote_bindings']=bindings
                    self.store.remote_source(data,result,profile['alias'])
                self.store.mutate(bind_remote)
            return result
        raise ValueError('지원하지 않는 관리 명령입니다.')

    def request(self,request):
        rid=request.get('id') if isinstance(request,dict) else None
        try:
            if not isinstance(request,dict) or not isinstance(request.get('command'),str):raise ValueError('올바른 관리 요청이 아닙니다.')
            from manager_core import rust_service
            if rust_service.enabled():
                # Same-profile mutations stay ordered. Slow work for another
                # profile must not hold a global UI/notes/connection lock.
                profile_id=request.get('args',{}).get('profile_id')
                key=('profile:'+str(profile_id)) if profile_id else request['command']
                with self._request_gate_lock:
                    gate=self._request_gates.setdefault(key,threading.RLock())
            else:gate=self._mutex
            with gate:result=self.dispatch(request['command'],request.get('args',{}))
            return dict(id=rid,ok=True,result=result)
        except (ValueError,RuntimeError,OSError,KeyError,TypeError,AttributeError) as error:
            message=('필수 입력 항목이 없습니다.' if isinstance(error,KeyError) else
                     '입력 항목의 형식이 올바르지 않습니다.' if isinstance(error,(TypeError,AttributeError)) else str(error))
            return dict(id=rid,ok=False,error=dict(code=getattr(error,'code','request_failed'),message=message))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serve',action='store_true');parser.add_argument('--once',action='store_true')
    parser.add_argument('--root',type=Path,default=ROOT)
    args=parser.parse_args()
    protocol=os.environ.get('CODEX_MANAGER_PROTOCOL_VERSION','0')
    center=ControlCenter(args.root,supervisor_protocol=int(protocol) if protocol.isdecimal() else 0)
    if args.serve:
        center.remote_updates.start()
        center.personal_skills.start()
        center.skill_bridge.start()
        center.shared_plugins.start()
        center.catalog_refresh.start(center.catalog_sources)
        center.source_catalog_refresh.start(center.native_catalog_sources)
        center.remote_catalog.start()
    try:
        from concurrent.futures import ThreadPoolExecutor
        from manager_core import rust_service
        concurrent = args.serve and rust_service.enabled()
        executor=ThreadPoolExecutor(max_workers=8) if concurrent else None
        output_lock=threading.Lock()
        slots=threading.BoundedSemaphore(64)
        def respond(raw):
            try:
                if len(raw)>1024*1024:
                    response=dict(id=None,ok=False,error=dict(code='request_too_large',message='요청이 너무 큽니다.'))
                else:
                    try:response=center.request(json.loads(raw))
                    except ValueError:response=dict(id=None,ok=False,error=dict(code='invalid_json',message='JSON 요청을 해석하지 못했습니다.'))
                with output_lock:print(json.dumps(response,ensure_ascii=False,allow_nan=False),flush=True)
            finally:
                if concurrent:slots.release()
        for raw in sys.stdin:
            if concurrent:
                slots.acquire();executor.submit(respond,raw)
            else:respond(raw)
            if args.once:break
    finally:
        center.profile_warmup.shutdown()
        center.remote_updates.shutdown()
        if executor is not None:executor.shutdown(wait=True)
        center.startup_updates.shutdown()
        center.update_jobs.shutdown()
        center.restarts.shutdown()
        if args.serve:
            center.skill_bridge.shutdown()
            center.shared_plugins.shutdown()
            center.personal_skills.shutdown()
            center.catalog_refresh.stop()
            center.source_catalog_refresh.stop()
            center.remote_catalog.stop()


if __name__=='__main__':
    sys.stdin.reconfigure(encoding='utf-8');sys.stdout.reconfigure(encoding='utf-8');sys.stderr.reconfigure(encoding='utf-8')
    main()
