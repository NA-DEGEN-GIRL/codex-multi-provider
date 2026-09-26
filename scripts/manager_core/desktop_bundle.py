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

_NOTIFICATION_CLICK_VARIANTS = {_NOTIFICATION_CLICK: _NOTIFICATION_REPLACEMENT,
    _NOTIFICATION_CLICK.replace(b'l.on', b'd.on'): _NOTIFICATION_REPLACEMENT.replace(b'l.on', b'd.on')}
_NOTIFICATION_SHOW_VARIANTS = {_NOTIFICATION_SHOW: _NOTIFICATION_SHOW_REPLACEMENT,
    _NOTIFICATION_STAGED_SHOW: _NOTIFICATION_STAGED_SHOW.replace(b'let f=()=>{' + _NOTIFICATION_PRESENT + b'}',
        # The hook may decline after its pipe wait; re-check that this toast is
        # still current, as 26.917's own staged path does, before falling back.
        b'let f=()=>{let m=()=>{' + _NOTIFICATION_PRESENT + b'};globalThis.__codexManagerNotificationShow?'
        b'globalThis.__codexManagerNotificationShow(e,t,()=>{if(this.notifications.get(e.id)?.notification===d){'
        b'if(t.isDestroyed()){this.removeNotification(e.id);return}m()}}):m()}')}
_PIPE_VARIANTS = {_ORIGINAL: _REPLACEMENT,
    _ORIGINAL.replace(b'return i.join', b'return s.join'): _REPLACEMENT.replace(b'return i.join', b'return s.join')}
_RENDERER_VARIANTS = {_CONTEXT_RENDERER: _CONTEXT_RENDERER_REPLACEMENT,
    **{_CONTEXT_RENDERER.replace(b't7.', binding): _CONTEXT_RENDERER_REPLACEMENT.replace(b't7.', binding)
       for binding in (b'F9.', b'R9.', b'L9.')}}
_CONTEXT_VARIANTS = {_CONTEXT_MAIN: _CONTEXT_MAIN_REPLACEMENT,
    _CONTEXT_MAIN.replace(b's.type', b'c.type'): _CONTEXT_MAIN_REPLACEMENT.replace(
        b's.type', b'c.type').replace(b't.sender,s.route,s.title', b'i.sender,c.route,c.title')}


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


def check_archive_support(directory):
    """Refuse an integrity-enforced build; never disable its protection."""
    sentinel = b'dL7pKGdnNz796PbbjQWNKmHXBZaB9tsX'
    with (Path(directory) / 'chrome.dll').open('rb') as file:
        with mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as image:
            offset = image.find(sentinel)
            if offset < 0:
                raise ValueError('Codex 데스크톱 호환성 정보를 찾지 못했습니다.')
            flags = image[offset + len(sentinel):offset + len(sentinel) + 11]
    # Wire header (version, count), then FuseV1 index 4: ASAR integrity.
    if len(flags) < 7 or flags[0] != 1 or flags[1] < 5 or flags[6] != ord('0'):
        raise ValueError('이 Codex 버전은 관리용 데스크톱 복사본을 지원하지 않습니다.')


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
    from .original_sync_bundle import patches_for, renderer_patches_for, renderer_plugin_patches, renderer_host_identity_patches
    with Path(source).open('rb') as src:
        header, base = read_header(src)
        entries = list(_entries(header))
        matches = []
        notifications = []
        contexts = []
        renderers = []
        sync_modules = []
        sync_renderers = []
        browser_runtimes = []
        for name, item in entries:
            if (name.startswith('.vite/build/') or name.startswith('webview/assets/app-initial')) and name.endswith('.js') and item['size'] <= 32 * 1024 * 1024:
                src.seek(base + int(item['offset']))
                data = src.read(item['size'])
                if _BROWSER_RUNTIME in data:
                    if data.count(_BROWSER_RUNTIME) != 1:
                        raise ValueError('Ambiguous desktop browser helper runtime.')
                    browser_runtimes.append((name, item, data))
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
                sync_patches = patches_for(data)
                if sync_patches:
                    sync_modules.append((name, item, data, sync_patches))
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
        browser_name, browser_target, browser_data = browser_runtimes[0]
        browser_data = changed.get(browser_name, (None, browser_data))[1]
        changed[browser_name] = (browser_target, browser_data.replace(_BROWSER_RUNTIME, _BROWSER_RUNTIME_REPLACEMENT))
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
            for before, after in renderer_host_identity_patches(renderer_data).items():
                renderer_data = renderer_data.replace(before, after)
            changed[renderer_name] = (renderer_target, renderer_data)
        if len(sync_modules) != 1:
            # A previously sync-patched archive is used by the isolated desktop
            # integration fixture. Production always starts from installed assets.
            already = any(b'globalThis.__codexRecordSync?.observe(this,e,t)' in data for _, _, data, _ in contexts)
            if not already:
                raise ValueError('이 Codex 버전의 대화 자동 갱신 연결을 확인하지 못했습니다.')
        else:
            sync_name, sync_target, sync_data, sync_patches = sync_modules[0]
            sync_data = changed.get(sync_name, (None, sync_data))[1]
            for before, after in sync_patches.items():
                sync_data = sync_data.replace(before, after)
            signal = b'globalThis.__codexRecordSync?.observe(this,e,t);'
            sync_data = sync_data.replace(signal, signal + b'globalThis.__codexManagerNotificationActivity?.(this.hostId,e,t);')
            sync_adapter = b'\n'.join(Path(__file__).with_name(name).read_bytes() for name in
                ('desktop_signal_files.cjs', 'desktop_profile_resume.cjs', 'desktop_workspace_sync.cjs', 'desktop_project_membership.cjs', 'desktop_local_workspace_sync.cjs', 'desktop_plugin_sync.cjs', 'desktop_record_sync.cjs'))
            changed[sync_name] = (sync_target, sync_adapter + b'\n' + sync_data)
        if len(sync_renderers) != 1:
            if not any(b'globalThis.__codexRendererRecordSync?.register(this)' in row[2] for row in renderers):
                raise ValueError('Desktop renderer history synchronization is not verified for this version.')
        else:
            sync_name,sync_target,sync_data,sync_patches=sync_renderers[0]
            sync_data=changed.get(sync_name,(None,sync_data))[1]
            for before,after in sync_patches.items():sync_data=sync_data.replace(before,after)
            from .desktop_reasoning_ui import patch as patch_reasoning
            sync_data=patch_reasoning(sync_data)
            changed[sync_name]=(sync_target,Path(__file__).with_name('desktop_profile_resume.cjs').read_bytes()+b'\n'+Path(__file__).with_name('desktop_renderer_record_sync.cjs').read_bytes()+b'\n'+Path(__file__).with_name('desktop_plugin_renderer_sync.cjs').read_bytes()+b'\n'+sync_data)
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
        patched_sha256=hashlib.sha256(changed[name][1]).hexdigest(), notification_module=notification_name)


def prepare(root, app):
    """Copy program assets once per package version; never copy account data."""
    source = Path(app['executable']).resolve().parent
    archive = source / 'resources/app.asar'
    version = app['Version']
    if not re.fullmatch(r'[0-9.]{1,40}', version):
        raise ValueError('Invalid desktop version.')
    stat = archive.stat()
    adapters = ['desktop_network_policy.cjs', 'desktop_window_host.cjs', 'desktop_window_health.cjs', 'desktop_notification_activation.cjs', 'desktop_task_context.cjs',
        'desktop_signal_files.cjs', 'desktop_record_sync.cjs', 'desktop_renderer_record_sync.cjs', 'desktop_plugin_renderer_sync.cjs', 'desktop_profile_resume.cjs',
        'desktop_workspace_sync.cjs', 'desktop_project_membership.cjs', 'desktop_local_workspace_sync.cjs', 'desktop_plugin_sync.cjs', 'desktop_reasoning_ui.py', 'original_sync_bundle.py',
        'desktop_publication.py']
    identity = dict(version=version, source=str(source), size=stat.st_size, modified=stat.st_mtime_ns, revision=REVISION,
        patch=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        adapters={name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in adapters})
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16]
    directory = Path(root).resolve() / 'artifacts/managed-desktop' / (version + '-' + key)
    from .desktop_publication import publish
    directory, value, fallback = publish(source, directory, identity, 'manager-desktop.json',
        patch_archive, check_archive_support, _long_path)
    version = value['source']['version']
    return {**app, 'Version': version, 'executable': str(directory / 'ChatGPT.exe'),
        'desktop_isolation_revision': REVISION, 'desktop_compatibility_notice':
            f"설치된 Codex {app['Version']} 호환성 확인 대기 · 검증된 {version} 관리용 앱으로 실행합니다." if fallback else ''}
