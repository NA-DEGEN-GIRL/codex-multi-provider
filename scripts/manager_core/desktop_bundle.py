"""Private desktop copy with a profile-scoped Windows coordination pipe.

The installed app and its signatures/fuses are never changed. Each managed
profile keeps the native app protocol but cannot discover another account's
thread owner. A versioned, exact source match is required before publication.
"""
import hashlib
import json
import mmap
import os
from pathlib import Path
import re
import shutil
import struct
import threading
from uuid import UUID, uuid4

from .store import atomic_json

REVISION = 22
_LOCK = threading.Lock()
_ORIGINAL = b'if(process.platform===`win32`)return i.join(`\\\\\\\\.\\\\pipe`,`codex-ipc`);'
_REPLACEMENT = (b'if(process.platform===`win32`){let p=process.env.CODEX_MANAGER_DESKTOP_PIPE;'
    b'if(!/^codex-manager-[a-f0-9-]{36}$/.test(p||``))throw Error(`Managed profile pipe missing`);'
    b'return i.join(`\\\\\\\\.\\\\pipe`,p);}')
_NOTIFICATION_CLICK = b'l.on(`click`,()=>{let t=n({notificationId:e.id,actionId:null,actionType:`open`});'
_NOTIFICATION_REPLACEMENT = _NOTIFICATION_CLICK + b'globalThis.__codexManagerNotificationClick?.(e,t);'
_NOTIFICATION_SHOW = b'this.emitCompletedThreadsChanged(),l.show()}stageNotificationSoundIfNeeded()'
_NOTIFICATION_SHOW_REPLACEMENT = b'this.emitCompletedThreadsChanged(),(globalThis.__codexManagerNotificationShow?globalThis.__codexManagerNotificationShow(e,t,()=>l.show()):l.show())}stageNotificationSoundIfNeeded()'
# 26.917 names the toast d (l is now the sound setting). Its Windows toast is
# silent and f plays the bundled sound beside it, after the optional sound
# staging and still-current/destroyed guard. An accepted workspace toast brings
# its own Windows sound, as 26.915's native toast did, so the hook replaces f's
# whole presentation; a declined one runs f's native body unchanged.
_NOTIFICATION_PRESENT = (b'd.show(),this.options.platform!==`darwin`&&l!==`none`&&'
    b'this.playBundledNotificationSound(l===`classic`?`classic`:`default`)')
_NOTIFICATION_STAGED_SHOW = (b'this.emitCompletedThreadsChanged();let f=()=>{' + _NOTIFICATION_PRESENT +
    b'},p=l==="default"||l===`classic`?this.stageNotificationSoundIfNeeded(l):void 0;p==null?f():p.then(()=>{'
    b'if(this.notifications.get(e.id)?.notification===d){if(t.isDestroyed()){this.removeNotification(e.id);return}f()}})}'
    b'playBundledNotificationSound(e){')
_WINDOW_MESSAGE = b'sendMessageToWebContents(e,t,n){if(e.isDestroyed()'
_WINDOW_MESSAGE_REPLACEMENT = b'sendMessageToWebContents(e,t,n){globalThis.__codexManagerNavigation?.register(this,e);if(e.isDestroyed()'
_CONTEXT_MAIN = b'if(s.type===`remote-hosted-pip-active-thread-changed`){'
_CONTEXT_MAIN_REPLACEMENT = (b'if(s.type===`manager-task-context-changed`){globalThis.__codexManagerTaskContext?.(t.sender,s.route,s.title);return}' + _CONTEXT_MAIN)
_CONTEXT_RENDERER = b'(0,t7.useLayoutEffect)(()=>{},[n,r,c,t]);'
_CONTEXT_RENDERER_REPLACEMENT = (_CONTEXT_RENDERER + b'(0,t7.useEffect)(()=>{window.electronBridge?.sendMessageFromView?.({type:`manager-task-context-changed`,route:r+i,title:document.title})},[r,i]);')
# Native browser helpers start their own configuration-only app-server. They
# inherit the runtime's sanitized environment, so the desktop proxy (which needs
# manager launch metadata) cannot be used as their CLI. Only change the native
# helper path resolver: the desktop itself still uses the account-bound proxy.
_BROWSER_RUNTIME = b'rawValue:e.CODEX_CLI_PATH,resolveWindowsAppsPath:a}'
_BROWSER_RUNTIME_REPLACEMENT = b'rawValue:e.CODEX_MANAGER_REAL_RUNTIME??e.CODEX_CLI_PATH,resolveWindowsAppsPath:a}'

# 26.930 activates through an async helper that awaits the target webContents;
# the hook needs only that webContents, as before.
_NOTIFICATION_CLICK_930 = (b'let f=async(t,r=!1)=>{try{let e=await n(t);'
    b'r&&e!=null&&!e.isDestroyed()&&this.options.onAppEntrySource?.(')
_NOTIFICATION_CLICK_VARIANTS = {_NOTIFICATION_CLICK: _NOTIFICATION_REPLACEMENT,
    _NOTIFICATION_CLICK.replace(b'l.on', b'd.on'): _NOTIFICATION_REPLACEMENT.replace(b'l.on', b'd.on'),
    _NOTIFICATION_CLICK_930: _NOTIFICATION_CLICK_930.replace(b'await n(t);',
        b'await n(t);r&&globalThis.__codexManagerNotificationClick?.(t,e);')}
# 26.930 names the toast h and presents it from g, after its own current,
# destroyed and focused-window suppression guard.
_NOTIFICATION_GUARD_930 = (b'if(this.notifications.get(e.id)?.notification===h){if(t.isDestroyed()||'
    b'this.shouldSuppressNotification(e.kind)){this.removeNotification(e.id);return}')
_NOTIFICATION_PRESENT_930 = b'h.show(),this.options.platform!==`darwin`&&l!==`none`&&this.playNotificationSound(l,t)'
_NOTIFICATION_SHOW_930 = (b'h=p(d),g=()=>{' + _NOTIFICATION_GUARD_930 + _NOTIFICATION_PRESENT_930 +
    b'}},_=this.stageNotificationSoundIfNeeded(l,d,u);')
_NOTIFICATION_SHOW_VARIANTS = {_NOTIFICATION_SHOW: _NOTIFICATION_SHOW_REPLACEMENT,
    _NOTIFICATION_STAGED_SHOW: _NOTIFICATION_STAGED_SHOW.replace(b'let f=()=>{' + _NOTIFICATION_PRESENT + b'}',
        # The hook may decline after its pipe wait; re-check that this toast is
        # still current, as 26.917's own staged path does, before falling back.
        b'let f=()=>{let m=()=>{' + _NOTIFICATION_PRESENT + b'};globalThis.__codexManagerNotificationShow?'
        b'globalThis.__codexManagerNotificationShow(e,t,()=>{if(this.notifications.get(e.id)?.notification===d){'
        b'if(t.isDestroyed()){this.removeNotification(e.id);return}m()}}):m()}'),
    _NOTIFICATION_SHOW_930: (b'h=p(d),g=()=>{' + _NOTIFICATION_GUARD_930 + b'let m=()=>{' + _NOTIFICATION_PRESENT_930 +
        b'};globalThis.__codexManagerNotificationShow?globalThis.__codexManagerNotificationShow(e,t,()=>{' +
        _NOTIFICATION_GUARD_930 + b'm()}}):m()}},_=this.stageNotificationSoundIfNeeded(l,d,u);')}
_PIPE_VARIANTS = {_ORIGINAL: _REPLACEMENT,
    **{_ORIGINAL.replace(b'return i.join', b'return '+binding+b'.join'):
       _REPLACEMENT.replace(b'return i.join', b'return '+binding+b'.join')
       for binding in (b's', b'o', b'c')}}  # 26.924 renames the path import to o, 26.930 to c.
# 26.930 compiles the route component with the React compiler: its layout
# effect is cached, and the pathname/search bindings are s and c.
_CONTEXT_RENDERER_930 = (b'(g=[o,s,m,t],e[11]=o,e[12]=s,e[13]=m,e[14]=t,e[15]=g):g=e[15],'
                         b'(0,X5.useLayoutEffect)(h,g);')
_RENDERER_VARIANTS = {_CONTEXT_RENDERER: _CONTEXT_RENDERER_REPLACEMENT,
    **{_CONTEXT_RENDERER.replace(b't7.', binding): _CONTEXT_RENDERER_REPLACEMENT.replace(b't7.', binding)
       for binding in (b'F9.', b'R9.', b'L9.')},
    # 26.930.3930 binds React as X5, 26.930.4958 as Z5.
    **{_CONTEXT_RENDERER_930.replace(b'X5.', binding): _CONTEXT_RENDERER_930.replace(b'X5.', binding) + (
        b'(0,' + binding + b'useEffect)(()=>{window.electronBridge?.sendMessageFromView?.('
        b'{type:`manager-task-context-changed`,route:s+c,title:document.title})},[s,c]);')
       for binding in (b'X5.', b'Z5.')}}
_CONTEXT_VARIANTS = {_CONTEXT_MAIN: _CONTEXT_MAIN_REPLACEMENT,
    _CONTEXT_MAIN.replace(b's.type', b'c.type'): _CONTEXT_MAIN_REPLACEMENT.replace(
        b's.type', b'c.type').replace(b't.sender,s.route,s.title', b'i.sender,c.route,c.title'),
    _CONTEXT_MAIN.replace(b's.type', b'a.type'): _CONTEXT_MAIN_REPLACEMENT.replace(
        b's.type', b'a.type').replace(b't.sender,s.route,s.title', b'n.sender,a.route,a.title')}
_BROWSER_RUNTIME_VARIANTS = {_BROWSER_RUNTIME: _BROWSER_RUNTIME_REPLACEMENT,
    _BROWSER_RUNTIME.replace(b':a}', b':i}'): _BROWSER_RUNTIME_REPLACEMENT.replace(b':a}', b':i}')}  # 26.930


def _matching_variant(data, variants):
    found = [key for key in variants if key in data]
    if len(found) > 1 or (found and data.count(found[0]) != 1):
        raise ValueError('Ambiguous desktop integration implementation.')
    return found[0] if found else None


def pipe_name(profile_id):
    return 'codex-manager-' + str(UUID(profile_id))


def _long_path(path):
    value = str(Path(path).resolve())
    if os.name != 'nt' or value.startswith('\\\\?\\'):
        return value
    return '\\\\?\\UNC\\' + value[2:] if value.startswith('\\\\') else '\\\\?\\' + value


# The executable's embedded ASAR integrity record (Electron) for the app archive.
_INTEGRITY = re.compile(rb'"file":"resources\\\\app\.asar","alg":"SHA256","value":"([0-9a-f]{64})"')


def _integrity_enforced(directory):
    sentinel = b'dL7pKGdnNz796PbbjQWNKmHXBZaB9tsX'
    with (Path(directory) / 'chrome.dll').open('rb') as file:
        with mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as image:
            offset = image.find(sentinel)
            if offset < 0:
                raise ValueError('Codex 데스크톱 호환성 정보를 찾지 못했습니다.')
            flags = image[offset + len(sentinel):offset + len(sentinel) + 11]
    # Wire header (version, count), then FuseV1 index 4: ASAR integrity.
    if len(flags) < 7 or flags[0] != 1 or flags[1] < 5 or flags[6] not in b'01':
        raise ValueError('이 Codex 버전은 관리용 데스크톱 복사본을 지원하지 않습니다.')
    return flags[6] == ord('1')


def header_digest(archive):
    """SHA-256 of the archive's header string, the value Electron validates."""
    with Path(archive).open('rb') as stream:
        prefix = stream.read(16)
        if len(prefix) != 16:
            raise ValueError('Invalid desktop archive header.')
        length = struct.unpack('<4I', prefix)[3]
        if not 0 < length <= 16 * 1024 * 1024:
            raise ValueError('Unsupported desktop archive header.')
        header = stream.read(length)
    if len(header) != length:
        raise ValueError('Truncated desktop archive header.')
    return hashlib.sha256(header).hexdigest()


def _integrity_record(executable, expected):
    data = Path(executable).read_bytes()
    found = list(_INTEGRITY.finditer(data))
    if len(found) != 1 or found[0].group(1).decode() != expected:
        raise ValueError('이 Codex 버전의 앱 무결성 정보를 확인하지 못했습니다. 관리 앱 호환성 업데이트가 필요합니다.')
    return data, found[0]


def check_archive_support(directory):
    """Accept an integrity-enforced build only when its record can be re-sealed.

    The installed package is never changed. With Electron's ASAR integrity fuse
    on, the private copy keeps validation enabled; its executable's record must
    match the installed archive exactly, so a patched copy can carry the digest
    of its own archive instead (see seal_archive_integrity).
    """
    if _integrity_enforced(directory):
        _integrity_record(Path(directory) / 'ChatGPT.exe', header_digest(Path(directory) / 'resources/app.asar'))


def seal_archive_integrity(source, stage):
    """Point the private copy's integrity record at its patched archive.

    Validation stays enabled: Electron still rejects any archive whose header
    differs from the sealed digest, and every changed entry carries its block
    hashes. Only the staged copy's executable is rewritten, in place and with
    the same length; the copy's Authenticode signature no longer applies.
    """
    if not _integrity_enforced(source):
        return None
    before = header_digest(Path(source) / 'resources/app.asar')
    after = header_digest(Path(stage) / 'resources/app.asar')
    executable = Path(stage) / 'ChatGPT.exe'
    data, found = _integrity_record(_long_path(executable), before)
    data = data[:found.start(1)] + after.encode() + data[found.end(1):]
    Path(_long_path(executable)).write_bytes(data)
    _integrity_record(_long_path(executable), after)
    return dict(algorithm='SHA256', installed=before, sealed=after)


def _entries(tree, prefix=''):
    for name, item in tree['files'].items():
        path = prefix + name
        if 'files' in item:
            yield from _entries(item, path + '/')
        elif 'offset' in item and not item.get('unpacked'):
            yield path, item


def read_header(stream):
    prefix = stream.read(16)
    if len(prefix) != 16:
        raise ValueError('Invalid desktop archive header.')
    size, outer, inner, length = struct.unpack('<4I', prefix)
    if size != 4 or inner + 4 != outer or not 0 < length <= 16 * 1024 * 1024 or inner - length not in (4, 5, 6, 7):
        raise ValueError('Unsupported desktop archive header.')
    return json.loads(stream.read(length)), 8 + outer


def patch_archive(source, destination):
    """Patch exact native entry points and preserve unrelated archive assets."""
    from .original_sync_bundle import (main_sync_plan, renderer_patches_for, renderer_plugin_patches,
        renderer_host_identity_patches, renderer_remote_root_patches, renderer_summary_dir_patches,
        require_project_grouping, archive_version)
    from .desktop_reasoning_ui import PICKER_MARKER, patch as patch_reasoning
    from .desktop_chrome_host import Plan as ChromeHostPlan
    with Path(source).open('rb') as src:
        header, base = read_header(src)
        entries = list(_entries(header))
        matches = []
        notifications = []
        contexts = []
        renderers = []
        main_modules = []
        sync_renderers = []
        composer_renderers = []
        browser_runtimes = []
        pickers = []
        reasoning_settings = False
        host_filters = []
        remote_roots = []
        summary_dirs = []
        grouping_chunks = []
        chrome_host = ChromeHostPlan()
        for name, item in entries:
            if name.startswith('webview/assets/app-primary') and name.endswith('.js') and item['size'] <= 32 * 1024 * 1024:
                from .desktop_reasoning_ui import patch_composer
                src.seek(base + int(item['offset']))
                original = src.read(item['size'])
                updated = patch_composer(original)
                if updated != original:
                    composer_renderers.append((name, item, updated))
            # 26.930 moved renderer synchronization and the host filter into a
            # shared webview chunk; its main-process sync spans two modules.
            if (name.startswith('webview/assets/app-shared') and name.endswith('.js')
                    and item['size'] <= 32 * 1024 * 1024):
                src.seek(base + int(item['offset']))
                data = src.read(item['size'])
                renderer_sync_patches = renderer_patches_for(data)
                if renderer_sync_patches:
                    sync_renderers.append((name, item, data, renderer_sync_patches))
                grouping_chunks.append(name)
                if renderer_host_identity_patches(data):
                    host_filters.append(name)
                if renderer_remote_root_patches(data):
                    remote_roots.append(name)
                if renderer_summary_dir_patches(data):
                    summary_dirs.append(name)
                if PICKER_MARKER in data:
                    pickers.append(name)
                reasoning_settings = reasoning_settings or b'enabled-reasoning-efforts' in data
                continue
            if (name.startswith('.vite/build/') or name.startswith('webview/assets/app-initial')) and name.endswith('.js') and item['size'] <= 32 * 1024 * 1024:
                src.seek(base + int(item['offset']))
                data = src.read(item['size'])
                chrome_host.scan(name, item, data)
                browser_pattern = _matching_variant(data, _BROWSER_RUNTIME_VARIANTS)
                if browser_pattern:
                    browser_runtimes.append((name, item, data, browser_pattern))
                if name.startswith('webview/'):
                    grouping_chunks.append(name)
                    if renderer_host_identity_patches(data):
                        host_filters.append(name)
                    if renderer_remote_root_patches(data):
                        remote_roots.append(name)
                    if renderer_summary_dir_patches(data):
                        summary_dirs.append(name)
                    if PICKER_MARKER in data:
                        pickers.append(name)
                    reasoning_settings = reasoning_settings or b'enabled-reasoning-efforts' in data
                pipe_pattern = _matching_variant(data, _PIPE_VARIANTS)
                if pipe_pattern:
                    matches.append((name, item, data, pipe_pattern))
                click_pattern = _matching_variant(data, _NOTIFICATION_CLICK_VARIANTS)
                if click_pattern:
                    notifications.append((name, item, data, click_pattern))
                context_pattern = _matching_variant(data, _CONTEXT_VARIANTS)
                if context_pattern:
                    contexts.append((name, item, data, context_pattern))
                renderer_pattern = _matching_variant(data, _RENDERER_VARIANTS)
                if renderer_pattern:
                    renderers.append((name, item, data, renderer_pattern))
                renderer_sync_patches = renderer_patches_for(data)
                if renderer_sync_patches:
                    sync_renderers.append((name,item,data,renderer_sync_patches))
                if name.startswith('.vite/build/'):
                    main_modules.append((name, item, data))
        if len(matches) != 1:
            raise ValueError('이 Codex 버전의 프로필 통신 분리를 확인하지 못했습니다. 관리 앱 호환성 업데이트가 필요합니다.')
        name, target, data, pipe_pattern = matches[0]
        if len(notifications) != 1:
            raise ValueError('이 Codex 버전의 알림 클릭 연결 위치를 확인하지 못했습니다.')
        show_pattern = _matching_variant(notifications[0][2], _NOTIFICATION_SHOW_VARIANTS)
        if not show_pattern or notifications[0][2].count(_WINDOW_MESSAGE) != 1:
            raise ValueError('이 Codex 버전의 작업공간 알림·작업 이동 경로를 확인하지 못했습니다.')
        if len(contexts) != 1 or len(renderers) != 1:
            raise ValueError('이 Codex 버전의 선택한 작업 연결 위치를 확인하지 못했습니다.')
        if len(browser_runtimes) != 1:
            raise ValueError('이 Codex 버전의 브라우저 도구 실행 경로를 확인하지 못했습니다.')
        adapter = b'\n'.join(Path(__file__).with_name(name).read_bytes() for name in
            ('desktop_network_policy.cjs', 'desktop_window_host.cjs', 'desktop_window_health.cjs'))
        notification_adapter = Path(__file__).with_name('desktop_notification_activation.cjs').read_bytes()
        changed = {name: (target, adapter + b'\n' + data.replace(pipe_pattern, _PIPE_VARIANTS[pipe_pattern]))}
        if len(composer_renderers) > 1:
            raise ValueError('Desktop composer effort bundle is ambiguous.')
        for composer_name, composer_target, composer_data in composer_renderers:
            changed[composer_name] = (composer_target, composer_data)
        browser_name, browser_target, browser_data, browser_pattern = browser_runtimes[0]
        browser_data = changed.get(browser_name, (None, browser_data))[1]
        changed[browser_name] = (browser_target, browser_data.replace(
            browser_pattern, _BROWSER_RUNTIME_VARIANTS[browser_pattern]))
        notification_name, notification_target, notification_data, click_pattern = notifications[0]
        notification_data = changed.get(notification_name, (None, notification_data))[1]
        changed[notification_name] = (notification_target, notification_adapter + b'\n' +
            notification_data.replace(click_pattern, _NOTIFICATION_CLICK_VARIANTS[click_pattern])
            .replace(show_pattern, _NOTIFICATION_SHOW_VARIANTS[show_pattern]).replace(_WINDOW_MESSAGE, _WINDOW_MESSAGE_REPLACEMENT))
        context_adapter = Path(__file__).with_name('desktop_task_context.cjs').read_bytes()
        for context_name, context_target, context_data, context_pattern in contexts:
            context_data = changed.get(context_name, (None, context_data))[1]
            changed[context_name] = (context_target, context_adapter + b'\n' + context_data.replace(context_pattern, _CONTEXT_VARIANTS[context_pattern]))
        for renderer_name, renderer_target, renderer_data, renderer_pattern in renderers:
            renderer_data = changed.get(renderer_name, (None, renderer_data))[1]
            renderer_data = renderer_data.replace(renderer_pattern, _RENDERER_VARIANTS[renderer_pattern])
            for before, after in renderer_plugin_patches(renderer_data).items():
                renderer_data = renderer_data.replace(before, after)
            changed[renderer_name] = (renderer_target, renderer_data)
        # The host filter and the remote project grouping live in app-initial
        # (26.917) or app-shared (26.930); each exists in at most one chunk.
        # The remote worktree folder lookup follows the guard of its chunk.
        entries_by_name = dict(entries)
        def webview_data(name):
            if name in changed:
                return changed[name][1]
            item = entries_by_name[name]
            src.seek(base + int(item['offset']))
            return src.read(item['size'])
        for found, patches_of, label in ((host_filters, renderer_host_identity_patches, 'host-specific archive filter'),
                                         (remote_roots, renderer_remote_root_patches, 'remote project grouping'),
                                         (summary_dirs, renderer_summary_dir_patches, 'remote worktree folder lookup')):
            if len(found) > 1:
                raise ValueError('Ambiguous desktop ' + label + '.')
            for filter_name in found:
                filter_data = webview_data(filter_name)
                for before, after in patches_of(filter_data).items():
                    filter_data = filter_data.replace(before, after)
                changed[filter_name] = (entries_by_name[filter_name], filter_data)
        # 26.930 refuses to publish without both grouping patches (see
        # require_project_grouping); other versions keep the native fallback.
        require_project_grouping(archive_version(src, base, entries),
                                 [webview_data(chunk) for chunk in grouping_chunks])
        plan = main_sync_plan([(module, data) for module, _, data in main_modules])
        if plan is None:
            # A previously sync-patched archive is used by the isolated desktop
            # integration fixture. Production always starts from installed assets.
            already = any(b'globalThis.__codexRecordSync?.observe(this,e,t)' in data for _, _, data, _ in contexts)
            if not already:
                raise ValueError('이 Codex 버전의 대화 자동 갱신 연결을 확인하지 못했습니다.')
        else:
            module_patches, adapter_home = plan
            for sync_name, sync_patches in module_patches.items():
                sync_data = webview_data(sync_name)
                for before, after in sync_patches.items():
                    sync_data = sync_data.replace(before, after)
                signal = b'globalThis.__codexRecordSync?.observe(this,e,t);'
                sync_data = sync_data.replace(signal, signal + b'globalThis.__codexManagerNotificationActivity?.(this.hostId,e,t);')
                if sync_name == adapter_home:
                    sync_data = b'\n'.join(Path(__file__).with_name(name).read_bytes() for name in
                        ('desktop_signal_files.cjs', 'desktop_profile_resume.cjs', 'desktop_workspace_sync.cjs', 'desktop_project_membership.cjs',
                         'desktop_local_workspace_sync.cjs', 'desktop_plugin_sync.cjs', 'desktop_record_sync.cjs')) + b'\n' + sync_data
                changed[sync_name] = (entries_by_name[sync_name], sync_data)
        if len(sync_renderers) != 1:
            if not any(b'globalThis.__codexRendererRecordSync?.register(this)' in row[2] for row in renderers):
                raise ValueError('Desktop renderer history synchronization is not verified for this version.')
        else:
            sync_name,sync_target,sync_data,sync_patches=sync_renderers[0]
            sync_data=changed.get(sync_name,(None,sync_data))[1]
            for before,after in sync_patches.items():sync_data=sync_data.replace(before,after)
            changed[sync_name]=(sync_target,Path(__file__).with_name('desktop_profile_resume.cjs').read_bytes()+b'\n'+Path(__file__).with_name('desktop_renderer_record_sync.cjs').read_bytes()+b'\n'+Path(__file__).with_name('desktop_plugin_renderer_sync.cjs').read_bytes()+b'\n'+sync_data)
        # The effort picker lives in app-initial in 26.917 and 26.930.
        if len(pickers) > 1:
            raise ValueError('External reasoning picker is ambiguous.')
        if not pickers and reasoning_settings:
            raise ValueError('External reasoning picker is not verified for this desktop version.')
        for picker_name in pickers:
            changed[picker_name] = (entries_by_name[picker_name], patch_reasoning(webview_data(picker_name)))
        # Bundled marketplace add with a '#' root, and one Chrome registration
        # (see desktop_chrome_host). Applied last, onto every earlier change.
        chrome_native_host = chrome_host.apply(changed)
        segments = []
        for item, updated in changed.values():
            offset, old_size = int(item['offset']), item['size']
            segments.append((offset, old_size, updated))
            block_size = item.get('integrity', {}).get('blockSize', 4 * 1024 * 1024)
            if type(block_size) is not int or not 0 < block_size <= 16 * 1024 * 1024:
                raise ValueError('Unsupported desktop archive integrity block size.')
            item['size'] = len(updated)
            item['integrity'] = dict(algorithm='SHA256', hash=hashlib.sha256(updated).hexdigest(), blockSize=block_size,
                blocks=[hashlib.sha256(updated[n:n + block_size]).hexdigest() for n in range(0, len(updated), block_size)])
        for _, item in entries:
            original_offset = int(item['offset'])
            item['offset'] = str(original_offset + sum(len(updated) - old_size
                for offset, old_size, updated in segments if offset < original_offset))
        document = json.dumps(header, ensure_ascii=False, separators=(',', ':')).encode()
        payload = struct.pack('<I', len(document)) + document + b'\0' * (-len(document) % 4)
        packed_header = struct.pack('<I', len(payload)) + payload
        with Path(destination).open('wb') as dst:
            dst.write(struct.pack('<II', 4, len(packed_header)) + packed_header)
            src.seek(base)
            position = 0
            for offset, old_size, updated in sorted(segments):
                remaining = offset - position
                while remaining:
                    chunk = src.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError('Truncated desktop archive.')
                    dst.write(chunk)
                    remaining -= len(chunk)
                dst.write(updated)
                position = offset + old_size
                src.seek(base + position)
            shutil.copyfileobj(src, dst, 1024 * 1024)
    return dict(module=name, source_sha256=hashlib.sha256(data).hexdigest(),
        patched_sha256=hashlib.sha256(changed[name][1]).hexdigest(), notification_module=notification_name,
        chrome_native_host=chrome_native_host)


def adapter_identity():
    """The complete code identity shared by normal publication and upgrades."""
    adapters = ['desktop_network_policy.cjs', 'desktop_window_host.cjs', 'desktop_window_health.cjs', 'desktop_notification_activation.cjs', 'desktop_task_context.cjs',
        'desktop_signal_files.cjs', 'desktop_record_sync.cjs', 'desktop_renderer_record_sync.cjs', 'desktop_plugin_renderer_sync.cjs', 'desktop_profile_resume.cjs',
        'desktop_workspace_sync.cjs', 'desktop_project_membership.cjs', 'desktop_local_workspace_sync.cjs', 'desktop_plugin_sync.cjs', 'desktop_reasoning_ui.py', 'original_sync_bundle.py',
        'desktop_publication.py', 'desktop_chrome_host.py', 'desktop_managed_upgrade.py']
    return dict(revision=REVISION,
        patch=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        adapters={name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in adapters})


def prepare(root, app):
    """Copy program assets once per package version; never copy account data."""
    source = Path(app['executable']).resolve().parent
    archive = source / 'resources/app.asar'
    version = app['Version']
    if not re.fullmatch(r'[0-9.]{1,40}', version):
        raise ValueError('Invalid desktop version.')
    stat = archive.stat()
    identity = dict(version=version, source=str(source), size=stat.st_size, modified=stat.st_mtime_ns,
        **adapter_identity())
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16]
    directory = Path(root).resolve() / 'artifacts/managed-desktop' / (version + '-' + key)
    from .desktop_publication import publish
    directory, value, fallback = publish(source, directory, identity, 'manager-desktop.json',
        patch_archive, check_archive_support, _long_path, seal_archive_integrity)
    version = value['source']['version']
    return {**app, 'Version': version, 'executable': str(directory / 'ChatGPT.exe'),
        'desktop_isolation_revision': REVISION, 'desktop_compatibility_notice':
            f"설치된 Codex {app['Version']} 호환성 확인 대기 · 검증된 {version} 관리용 앱으로 실행합니다." if fallback else ''}
