"""Launch only isolated, manager-owned Codex instances; never the calling app."""
import ctypes
from ctypes import wintypes
from contextlib import nullcontext
import json
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4

from .store import identifier, now
from .model_settings import render_options


def process_identity(pid):
    if os.name != 'nt' or not pid:
        return None
    from . import rust_service
    if rust_service.enabled():
        return rust_service.request('process.identity', pid=int(pid))
    kernel=ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    kernel.QueryFullProcessImageNameW.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessTimes.argtypes=[wintypes.HANDLE,*([ctypes.POINTER(wintypes.FILETIME)]*4)]
    kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
    kernel.WaitForSingleObject.restype=wintypes.DWORD
    handle=kernel.OpenProcess(0x1000|0x100000,False,int(pid))
    if not handle:
        return None
    try:
        # Another supervisor can still hold a handle to an exited Electron
        # process. A queryable PID is not evidence that its singleton is alive.
        if kernel.WaitForSingleObject(handle,0)!=258:
            return None
        size=wintypes.DWORD(32768); buffer=ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle,0,buffer,ctypes.byref(size)):
            return None
        times=[wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle,*[ctypes.byref(t) for t in times]):
            return None
        created=(times[0].dwHighDateTime<<32)|times[0].dwLowDateTime
        return dict(process_id=int(pid),executable_path=buffer.value,process_created=created)
    finally:
        kernel.CloseHandle(handle)


def main_window(pid, remembered=None):
    if os.name != 'nt' or not pid:
        return None
    user=ctypes.WinDLL('user32',use_last_error=True)
    callback_type=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
    user.EnumWindows.argtypes=[callback_type,wintypes.LPARAM]
    user.GetWindowThreadProcessId.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowRect.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.RECT)]
    user.GetClassNameW.argtypes=[wintypes.HWND,wintypes.LPWSTR,ctypes.c_int]
    user.GetWindowTextW.argtypes=[wintypes.HWND,wintypes.LPWSTR,ctypes.c_int]
    user.GetWindowLongW.argtypes=[wintypes.HWND,ctypes.c_int]
    user.GetWindowLongW.restype=wintypes.LONG
    user.IsWindow.argtypes=[wintypes.HWND]
    user.IsWindowVisible.argtypes=[wintypes.HWND]
    user.GetWindow.argtypes=[wintypes.HWND,wintypes.UINT];user.GetWindow.restype=wintypes.HWND
    def belongs(handle):
        if not user.IsWindow(handle):return False
        owner=wintypes.DWORD()
        user.GetWindowThreadProcessId(handle,ctypes.byref(owner))
        if owner.value!=pid:return False
        cls=ctypes.create_unicode_buffer(256)
        user.GetClassNameW(handle,cls,256)
        return cls.value.startswith('Chrome_WidgetWin') and not user.GetWindow(handle,4)
    # EnumWindows lists top-level windows only. Once SetParent embeds the main
    # window, Chromium's remaining helper must never replace it on the next poll.
    # Validate the remembered HWND independently, including when its tab is hidden.
    if remembered and belongs(remembered):
        return int(remembered)
    windows=[]
    @callback_type
    def visit(handle,_):
        if belongs(handle):
            rect=wintypes.RECT()
            user.GetWindowRect(handle,ctypes.byref(rect))
            area=(rect.right-rect.left)*(rect.bottom-rect.top)
            visible=bool(user.IsWindowVisible(handle))
            title=ctypes.create_unicode_buffer(512)
            user.GetWindowTextW(handle,title,512)
            style=user.GetWindowLongW(handle,-16)
            extended=user.GetWindowLongW(handle,-20)
            # Hidden startup windows are eligible, but untitled/tool windows
            # used by Chromium are not a substitute for the main application.
            hidden_primary=bool(title.value.strip() and (style & 0x00C00000 or extended & 0x00040000))
            if area>10000 and not (extended & 0x00000080) and (visible or hidden_primary):
                windows.append((visible,area,int(handle)))
        return True
    user.EnumWindows(visit,0)
    return max(windows)[2] if windows else None


def embedded_startup(enabled):
    """Let the manager attach a new window before making its host visible."""
    if not enabled or os.name != 'nt':
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return {'startupinfo': startup}


class Instances:
    def __init__(self, root, store, providers, *, embed_windows=False):
        self.root=Path(root).resolve(); self.store=store; self.providers=providers
        self.processes={};self.handles={}
        self.embed_windows=embed_windows
        self.launch_admission=None
        from .catalog_refresh import CatalogRefresh
        self.catalog_refresh=CatalogRefresh(self.root)
        from .source_catalog import build as source_catalog_build
        self.source_catalog_refresh=CatalogRefresh(self.root,builder=source_catalog_build)

    def paths(self, profile):
        pid=identifier(profile['id'])
        folder=self.store.directory/'profiles'/pid
        if Path(profile['home']).resolve()!=folder/'codex' or Path(profile['ui_home']).resolve()!=folder/'ui':
            raise ValueError('관리 앱이 만든 프로필 경로만 실행할 수 있습니다.')
        return folder/'codex',folder/'ui'

    def observe(self, profile):
        from .login_health import inspect as login_health
        login = login_health(profile)
        current=process_identity(profile.get('process_id'))
        if not current or current.get('process_created')!=profile.get('process_created') or os.path.normcase(current['executable_path'])!=os.path.normcase(profile.get('executable_path','')):
            return dict(status='not_started',process_id=None,window_handle=None,job_state='not_running', login_health=login)
        remembered=self.handles.get(profile['id']) or profile.get('window_handle')
        window=main_window(current['process_id'],remembered=remembered)
        if window:self.handles[profile['id']]=window
        else:self.handles.pop(profile['id'],None)
        state=dict(current,status='running',window_handle=window,job_state='unknown',login_health=login)
        state['sidebar_cache_pending'] = ((Path(profile['home']) / 'sqlite/codex-dev.db').is_file()
                                         and not (Path(profile['home']) / '.manager-sidebar-cache.json').is_file())
        status_path=self.store.directory/'instances'/profile['id']/'runtime-state.json'
        if status_path.is_file():
            try:
                observer=json.loads(status_path.read_text(encoding='utf-8'))
                # A stale observer from an earlier launch must not certify idle.
                if observer.get('generation')==profile.get('generation'):
                    state['runtime_state']=observer
                    state['job_state']=observer.get('activity','unknown')
            except (OSError,ValueError):
                pass
        return state

    def prepare(self, profile):
        if self.observe(profile)['status']=='running':
            return dict(state='pending',message='실행 중인 인스턴스의 설정은 유지합니다. 종료 후 다시 열 때 적용됩니다.')
        home,ui=self.paths(profile)
        home.mkdir(parents=True,exist_ok=True);ui.mkdir(parents=True,exist_ok=True)
        from .runtime_build import resolve
        canonical = (not profile.get('view_only') and profile.get('runtime_channel') != 'packaged'
                     and resolve(self.root).get('capabilities', {}).get('canonical_record_storage', False))
        if canonical:
            from .canonical_storage import migrate
            migrate(self.root)
        # Source settings are declarations only. Existing manager config is preserved.
        from .common import prepare_common
        common=prepare_common(home,Path.home()/'.codex') if not profile.get('view_only') else {'mcp':'viewer_disabled'}
        if not profile.get('view_only') and (self.store.directory/'personal-skills.json').is_file():
            from .personal_skills import PersonalSkills
            common['personal_skills'] = PersonalSkills(self.store).reconcile(force=True)
        if not profile.get('view_only'):
            from .app_preferences import prepare as prepare_app
            from .proxy_auth import read_existing_tokens, LoginNeededError
            account_id = None
            try:
                account_id = read_existing_tokens(home if profile.get('auth_mode') == 'native'
                    else profile.get('source_home') or home, minimum_validity=0).account_id
            except LoginNeededError:
                pass
            ready_aliases = None if profile.get('runtime_channel') == 'packaged' else {
                binding['alias'] for binding in profile.get('remote_bindings', []) if binding.get('prepared') is True}
            common['app'] = prepare_app(home, Path.home()/'.codex', account_id=account_id,
                                        ssh_ready_aliases=ready_aliases, canonical=canonical)
            if canonical:
                from .workspace_seed import ensure as seed_ssh_projects
                from .shared_workspaces import prepare_home
                seed_ssh_projects(self.root)
                prepare_home(self.root, home)
        from .managed_sources import mark
        if not profile.get('view_only'):mark(profile)
        policy=profile['policy']
        generated=(dict(runtime='packaged',external_models=False) if profile.get('runtime_channel')=='packaged'
                   else self.providers.generate(home,policy['enabled'],policy['model_ids'],
                        **render_options(profile)))
        return dict(state='prepared',generated=generated,common=common)

    def environment(self, profile):
        from desktop_launch import child_environment
        env=child_environment('original')
        home,ui=self.paths(profile)
        from .desktop_bundle import pipe_name
        env['CODEX_MANAGER_DESKTOP_PIPE'] = pipe_name(profile['id'])
        env['CODEX_MANAGER_ROOT'] = str(self.root)
        if profile.get('auth_mode') in ('native', 'source', 'external'):
            env={name:value for name,value in env.items() if not name.upper().startswith(('OPENAI_','AZURE_OPENAI_','CHATGPT_'))}
        if profile.get('runtime_channel')=='packaged':
            from desktop_launch import find_app
            from .login_probe import verification_runtime
            # An unpackaged desktop cannot reliably execute an MSIX CLI in
            # WindowsApps. Use the verified official copy used by login probes.
            runtime=verification_runtime(self.root,find_app())
            env.update(CODEX_HOME=str(home),CODEX_ELECTRON_USER_DATA_PATH=str(ui),CODEX_CLI_PATH=str(runtime))
            return env
        from .runtime_build import resolve
        runtime_info=resolve(self.root)
        runtime=Path(runtime_info['runtime'])
        if not runtime.is_file():
            raise RuntimeError('빌드된 패치 런타임이 없습니다.')
        proxy=self.root/'artifacts/manager/Codex.ControlCenter.RuntimeProxy.exe'
        manifest=self.root/'artifacts/manager/current.json'
        if manifest.is_file():
            release=json.loads(manifest.read_text(encoding='utf-8-sig'))
            selected=Path(release['runtime_proxy']).resolve()
            if not selected.is_relative_to(self.root/'artifacts/manager') or not selected.is_file():
                raise RuntimeError('관리 런타임 실행기의 배포 경로가 올바르지 않습니다.')
            proxy=selected
        if profile.get('native_login_pending') and not proxy.is_file():
            raise RuntimeError('공통 대화와 로그인을 연결할 관리 실행기가 없습니다. 관리 앱 배포를 확인하세요.')
        env.update(CODEX_HOME=str(home),CODEX_ELECTRON_USER_DATA_PATH=str(ui),CODEX_CLI_PATH=str(proxy if proxy.is_file() else runtime))
        from .workspace_seed import ensure as seed_ssh_projects
        seed_ssh_projects(self.root)
        env['CODEX_MANAGER_RECORD_SIGNALS'] = str(self.root / 'work/control-center/record-signals')
        if (home / '.manager-project-aliases.json').is_file():
            env['CODEX_MANAGER_PROJECT_ALIASES'] = str(home / '.manager-project-aliases.json')
        if proxy.is_file():
            env.update(CODEX_MANAGER_ROOT=str(self.root),CODEX_MANAGER_PROFILE_ID=profile['id'],
                       CODEX_MANAGER_GENERATION=profile['generation'],CODEX_MANAGER_REAL_RUNTIME=str(runtime),
                       CODEX_MANAGER_PYTHON=os.sys.executable,
                       CODEX_MANAGER_OBSERVER_PATH=str(self.store.directory/'instances'/profile['id']/'runtime-state.json'))
            if profile.get('source_home') and profile.get('auth_mode')!='native':
                env['CODEX_MANAGER_AUTH_SOURCE']=str(Path(profile['source_home']))
            if profile.get('account_fingerprint'):
                env['CODEX_MANAGER_EXPECTED_ACCOUNT_FINGERPRINT']=profile['account_fingerprint']
            if profile.get('auth_mode')=='native' and profile.get('native_login_pending'):
                env['CODEX_MANAGER_NATIVE_LOGIN']='1'
        selected_models = list(profile['policy']['model_ids']) if profile['policy']['enabled'] else []
        env['CODEX_MANAGER_MODEL_OPTIONS'] = json.dumps(render_options(profile))
        if profile.get('auth_mode') == 'external':
            selected_models.append(profile['external_model_id'])
            binding = json.loads((home/'manager-provider-binding.json').read_text(encoding='utf-8'))['primary']
            env['CODEX_MANAGER_PRIMARY_MODEL'] = json.dumps(binding)
        if selected_models:
            env.update(self.providers.environment(list(dict.fromkeys(selected_models))))
        if profile.get('record_catalog_path'):
            if not runtime_info.get('capabilities',{}).get('native_record_catalog'):
                raise RuntimeError('전체 기록 조회 런타임이 아직 검증·배포되지 않았습니다.')
            env['CODEX_MANAGER_RECORD_CATALOG']=profile['record_catalog_path']
        elif runtime_info.get('capabilities',{}).get('managed_store_binding'):
            from .managed_sources import manifest as source_manifest
            env['CODEX_MANAGER_MANAGED_SOURCES']=str(source_manifest(self.store,profile))
        from .shared_catalog import environment as shared_environment
        env.update(shared_environment(self.store,profile,runtime_info.get('capabilities',{}),
                                      self.catalog_refresh,self.source_catalog_refresh))
        if env.get('CODEX_RECORD_HOME'):
            env.pop('CODEX_MANAGER_MANAGED_SOURCES', None)
            env.pop('CODEX_MANAGER_PROJECT_ALIASES', None)
            env['CODEX_MANAGER_SHARED_WRITER_ID'] = profile['id']
        if env.get('CODEX_MANAGER_SHARED_CATALOG') and runtime_info.get('capabilities', {}).get('shared_record_execution'):
            env.pop('CODEX_MANAGER_MANAGED_SOURCES', None)
            env['CODEX_MANAGER_SHARED_EXECUTION'] = '1'
            env['CODEX_MANAGER_SHARED_WRITER_ID'] = profile['id']
            if runtime_info.get('capabilities', {}).get('shared_new_task_storage'):
                original = Path.home() / '.codex'
                sources = self.store.read()['sources']
                if any(s.get('host_id') == 'local' and Path(s['home']) == original for s in sources):
                    env['CODEX_MANAGER_NEW_THREAD_HOME'] = str(original)
        if manifest.is_file() and json.loads(manifest.read_text(encoding='utf-8-sig')).get('ssh_proxy'):
            if profile.get('auth_mode')=='native':
                env['CODEX_MANAGER_SSH_AUTH_SOURCE']=str(home)
            if not manifest.is_file():
                raise RuntimeError('SSH 실행기가 포함된 관리 앱 배포가 필요합니다.')
            release=json.loads(manifest.read_text(encoding='utf-8-sig'))
            from .ssh_shim import prepare_environment
            from .ssh_compatibility import fingerprint
            from desktop_launch import find_app
            installed_app = find_app()
            env=prepare_environment(self.root,profile['id'],profile.get('remote_bindings',[]),env,
                                    app_version=installed_app['Version'],ssh_proxy=release.get('ssh_proxy'),
                                    app_source_sha256=fingerprint(installed_app['executable']),
                                    selected_model_ids=profile['policy']['model_ids'] if profile['policy']['enabled'] else [])
            frozen_shim = proxy.parent / 'scripts/manager_core/ssh_shim.py'
            if (proxy.parent / 'runtime-manifest.json').is_file():
                if not frozen_shim.is_file():raise RuntimeError('배포된 SSH 구성요소가 없습니다.')
                env['CODEX_MANAGER_SSH_SCRIPT'] = str(frozen_shim)
        return env

    def show(self, profile_id, *, reopen_existing=True):
        with self.launch_admission(profile_id) if self.launch_admission else nullcontext():
            return self._show(profile_id, reopen_existing=reopen_existing)

    def _show(self, profile_id, *, reopen_existing=True):
        profile=self.store.profile(profile_id)
        if profile.get('removed_at'):
            raise ValueError('목록에서 제거한 계정입니다. 계정 복원 후 열 수 있습니다.')
        if profile.get('account_missing') and profile.get('auth_mode')!='native':
            raise RuntimeError('llm-usage에서 삭제된 계정입니다. 다른 계정에 자동 연결하지 않았습니다.')
        from .login_health import require as require_login
        active=self.observe(profile)
        if active['status']=='running':
            if not reopen_existing:
                return dict(profile_id=profile['id'],profile={**profile,**active},state='existing')
            # Ask Electron itself to restore/show the existing profile. Merely
            # unhiding an old HWND can leave its renderer in the app's hidden
            # state. No deep link is sent, so its current conversation stays put.
            environment=self.environment(profile)
            try:
                from . import rust_service
                request=rust_service.launch(profile,active['executable_path'],environment,embed=self.embed_windows,reopen=True) if rust_service.enabled() else subprocess.Popen([active['executable_path'],f'--user-data-dir={profile["ui_home"]}'],
                    cwd=self.root,env=environment,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                try:request.wait(timeout=4)
                except subprocess.TimeoutExpired:pass
            finally:
                environment.clear()
            active=self.observe(profile)
            return dict(profile_id=profile['id'],profile={**profile,**active},state='reopen_requested',
                        message='이 프로필의 기존 Codex에 창 표시를 요청했습니다.')
        require_login(profile)
        if profile.get('desired_runtime_channel')=='managed':
            from .runtime_build import resolve
            if not resolve(self.root).get('capabilities',{}).get('managed_store_binding'):
                raise RuntimeError('계정별 관리 런타임의 검증이 아직 진행 중입니다. 현재 로그인 창은 계속 사용할 수 있습니다.')
            profile['runtime_channel']='managed'
        if profile.get('auth_mode')=='native' and profile.get('runtime_channel')=='managed':
            from .native_account_guard import NativeAccountGuard
            if not profile.get('native_login_pending') and (profile.get('login_state') not in ('signed_in','credential_saved') or not profile.get('account_fingerprint')
                    or not NativeAccountGuard({'CODEX_HOME':profile['home'],
                        'CODEX_MANAGER_EXPECTED_ACCOUNT_FINGERPRINT':profile['account_fingerprint']}).matches()):
                raise RuntimeError('이 프로필의 로그인 계정을 먼저 확인하세요. 다른 계정으로 작업을 시작하지 않았습니다.')
        preparation = self.prepare(profile)
        from desktop_launch import find_app
        from .desktop_bundle import prepare as prepare_desktop
        app=prepare_desktop(self.root, find_app());profile['generation']=str(uuid4())
        profile['shared_catalog_path']=None
        if profile.get('view_only'):
            from .runtime_build import resolve
            capabilities=resolve(self.root).get('capabilities',{})
            refresh=self.source_catalog_refresh if capabilities.get('mixed_source_catalog') else self.catalog_refresh
            catalog=refresh.ensure(self.store.read()['sources'],
                                   include_paginated=capabilities.get('paginated_record_catalog',False))
            profile['record_catalog_path']=catalog['path']
        restoration = getattr(self, 'authorize_restoration_generation', None)
        if restoration is not None:
            restoration(profile)
        env=self.environment(profile)
        profile['shared_catalog_path']=env.get('CODEX_MANAGER_SHARED_CATALOG')
        profile['manager_release']=env.get('CODEX_CLI_PATH')
        from .release_code import runtime_revision
        profile['manager_runtime_revision'] = runtime_revision(profile['manager_release'])
        try:
            from .app_catalog_cache import prepare as prepare_catalog_cache
            preparation['sidebar_cache'] = prepare_catalog_cache(profile['home'], env)
            # The combined desktop also offers ChatGPT/Work. Its native deep link
            # selects the Codex composer without creating or submitting a task.
            from . import rust_service
            process=rust_service.launch(profile,app['executable'],env,embed=self.embed_windows) if rust_service.enabled() else subprocess.Popen([app['executable'],f'--user-data-dir={profile["ui_home"]}',
                                      'codex://threads/new?mode=codex'],cwd=self.root,
                                     env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                                     creationflags=subprocess.CREATE_NEW_PROCESS_GROUP, **embedded_startup(self.embed_windows))
        finally:
            env.clear()
        self.processes[profile['id']]=process
        current=process_identity(process.pid)
        if not current:
            raise RuntimeError('인스턴스 시작을 확인하지 못했습니다. 다른 계정으로 전환하지 않았습니다.')
        def save(data):
            p=self.store.profile(profile['id'],data)
            p.update(current,generation=profile['generation'],app_version=app['Version'],started_at=now(),status='running')
            p['desktop_isolation_revision'] = app['desktop_isolation_revision']
            p['desktop_compatibility_notice'] = app.get('desktop_compatibility_notice', '')
            p['shared_catalog_path']=profile.get('shared_catalog_path')
            p['manager_release']=profile.get('manager_release')
            p['manager_runtime_revision']=profile.get('manager_runtime_revision')
            if profile.get('view_only'):p['record_catalog_path']=profile['record_catalog_path']
            if profile.get('runtime_channel'):p['runtime_channel']=profile['runtime_channel']
            p['policy']['launched_revision']=profile['policy']['desired_revision']
            p['policy']['effective_revision']=None
            from .profile_restart import supersede_previous_notice
            supersede_previous_notice(data, p)
            return p
        profile=self.store.mutate(save)
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            window=main_window(process.pid)
            if window or process.poll() is not None:
                break
            time.sleep(.15)
        if profile.get('runtime_channel') == 'packaged' or profile.get('native_login_pending'):
            # A startup failure dialog can briefly look like a ready window.
            # Never report an already exited login process as awaiting input.
            try:
                process.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                pass
            if process.poll() is not None:
                raise RuntimeError('로그인 창이 시작 중 종료되었습니다. 로그인 완료 대기 상태가 아닙니다. 관리 앱의 최신 버전에서 ‘이 프로필에 로그인’을 다시 누르세요.')
        if window:
            self.handles[profile['id']]=window
            self.store.mutate(lambda data:self.store.profile(profile['id'],data).update(window_handle=window))
        return dict(profile_id=profile['id'],profile={**profile,**self.observe(profile)},state='launched', preparation=preparation)
