"""Version-checked original-account companion; installed package stays untouched."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
from uuid import uuid4

from .desktop_bundle import read_header, _entries, check_archive_support, _long_path
from .store import atomic_json

PATCHES = {
    b'getGlobalStateValue(e){return': b'getGlobalStateValue(e){globalThis.__codexWorkspaceSync?.register(this.globalState,this.windowManager);globalThis.__codexLocalWorkspaceSync?.register(this.globalState,this.windowManager);return',
    b'ensureProjectsReady(){if(this.disposed||!this.connected)':
        b'ensureProjectsReady(){globalThis.__codexLocalWorkspaceSync?.registerBackend(this);if(this.disposed||!this.connected)',
    b'if(s.type===`remote-hosted-pip-active-thread-changed`){':
        b'if(s.type===`manager-record-changed`){globalThis.__codexRecordSync?.publish(s.threadId,s.hostId,s.kind);return}if(s.type===`remote-hosted-pip-active-thread-changed`){',
    b'onNotification(e,t,n=null,r){if(this.assertActive(),':
        b'onNotification(e,t,n=null,r){globalThis.__codexRecordSync?.observe(this,e,t);if(this.assertActive(),',
    b'this.requestClient=r;let b=this.settings.restricted;':
        b'this.requestClient=r;globalThis.__codexRecordSync?.register(this);let b=this.settings.restricted;',
    b'requestStartupSync(){if(!this.syncEnabled)':
        b'requestStartupSync(){globalThis.__codexRecordSync?.catalog(this);if(!this.syncEnabled)',
    b'shouldApplyHydratedThread:()=>c===this.hydrationGeneration&&(r?.isCurrent()??!0)':
        b'shouldApplyHydratedThread:()=>c===this.hydrationGeneration&&(r?.isCurrent()??!0)&&'
        b'(globalThis.__codexRecordSync?.canApply(this)??true)',
}

RENDERER_PATCHES = {
    b'onNotification(e,t,n=null,r){if(this.assertActive(),':
        b'onNotification(e,t,n=null,r){globalThis.__codexRendererRecordSync?.observe(this,e,t);if(this.assertActive(),',
    b'this.requestClient=n;let y=this.settings.restricted;':
        b'this.requestClient=n;globalThis.__codexRendererRecordSync?.register(this);globalThis.__codexPluginRendererSync?.register(this);let y=this.settings.restricted;',
    b'shouldApplyHydratedThread:()=>c===this.hydrationGeneration&&(n?.isCurrent()??!0)':
        b'shouldApplyHydratedThread:()=>c===this.hydrationGeneration&&(n?.isCurrent()??!0)&&(globalThis.__codexRendererRecordSync?.canApply(this)??true)',
}

# The native renderer notification case for `skills/changed` invalidates only
# the skills query. Plugin/skill membership changed, so the verified case also
# invalidates the `plugins` prefix (Fw=[`plugins`]) and exposes a bounded
# counter for the live fixture. A missing case only skips this extra
# invalidation; the main adapter still reloads through the native app-server.
_PLUGIN_RENDERER_MARK = b'globalThis.__codexPluginInvalidations'
_PLUGIN_RENDERER_CASE = re.compile(
    rb'case`skills/changed`:([A-Za-z_$][A-Za-z0-9_$]*)\.queryClient'
    rb'\.invalidateQueries\(\{queryKey:\[`skills`\]\}\);break;')


def renderer_plugin_patches(data):
    matches = list(_PLUGIN_RENDERER_CASE.finditer(data))
    if len(matches) > 1:
        raise ValueError('Ambiguous desktop plugin refresh case.')
    if not matches:
        # Already patched (isolated fixture) or a version without the verified
        # case: the reload still runs, only this extra invalidation is skipped.
        return {}
    receiver = matches[0].group(1)
    replacement = (b'case`skills/changed`:' + _PLUGIN_RENDERER_MARK + b'=(' + _PLUGIN_RENDERER_MARK +
        b'||0)+1;' + receiver + b'.queryClient.invalidateQueries({queryKey:[`skills`]});' +
        receiver + b'.queryClient.invalidateQueries({queryKey:[`plugins`]});break;')
    return {matches[0].group(0): replacement}


def renderer_patches_for(data):
    return RENDERER_PATCHES if all(data.count(key)==1 for key in RENDERER_PATCHES) else None


# Verified in 26.908, 26.911 and 26.915. Keep the complete expression, including its
# cancellation checks; a renamed/minified binding is not a protocol change.
def patches_for(data):
    variants = [PATCHES, {
        key.replace(b'()=>c===', b'()=>l==='): value.replace(b'()=>c===', b'()=>l===')
        for key, value in PATCHES.items()
    }]
    variants.append({
        key.replace(b'()=>c===', b'()=>l===').replace(b'if(s.type', b'if(c.type'):
        value.replace(b'()=>c===', b'()=>l===').replace(b'if(s.type', b'if(c.type').replace(
            b'publish(s.threadId,s.hostId,s.kind)', b'publish(c.threadId,c.hostId,c.kind)')
        for key, value in PATCHES.items()
    })
    matches = [patches for patches in variants if all(data.count(key) == 1 for key in patches)]
    return matches[0] if len(matches) == 1 else None


def patch_archive(source, destination):
    with Path(source).open('rb') as stream:
        header, base = read_header(stream)
        entries = list(_entries(header))
        matched = []
        renderers = []
        for name, entry in entries:
            if not name.endswith('.js') or not (name.startswith('.vite/build/') or name.startswith('webview/assets/app-initial')):
                continue
            stream.seek(base + int(entry['offset']))
            data = stream.read(entry['size'])
            patches = renderer_patches_for(data) if name.startswith('webview/') else patches_for(data)
            if patches:
                (renderers if name.startswith('webview/') else matched).append((name, entry, data, patches))
        if len(matched) != 1 or len(renderers) != 1:
            raise ValueError('Desktop main/renderer synchronization entry points are not verified for this version.')
        name, target, before, patches = matched[0]
        segments = []
        for module, entry, data, replacements in matched + renderers:
            updated = data
            for pattern, replacement in replacements.items():
                updated = updated.replace(pattern, replacement)
            if module.startswith('webview/'):
                for pattern, replacement in renderer_plugin_patches(data).items():
                    updated = updated.replace(pattern, replacement)
            helper = 'desktop_renderer_record_sync.cjs' if module.startswith('webview/') else 'desktop_record_sync.cjs'
            updated = Path(__file__).with_name(helper).read_bytes() + b'\n' + updated
            if module.startswith('webview/'):
                updated = Path(__file__).with_name('desktop_plugin_renderer_sync.cjs').read_bytes() + b'\n' + updated
            updated = Path(__file__).with_name('desktop_profile_resume.cjs').read_bytes() + b'\n' + updated
            if not module.startswith('webview/'):
                updated = b'\n'.join(Path(__file__).with_name(name).read_bytes() for name in (
                    'desktop_signal_files.cjs', 'desktop_workspace_sync.cjs', 'desktop_project_membership.cjs', 'desktop_local_workspace_sync.cjs',
                    'desktop_plugin_sync.cjs')) + b'\n' + updated
            if module == name:
                updated = Path(__file__).with_name('desktop_network_policy.cjs').read_bytes() + b'\n' + updated
                after = updated
            offset, size = int(entry['offset']), entry['size']
            segments.append((offset,size,updated))
            block = entry.get('integrity', {}).get('blockSize',4*1024*1024)
            if type(block) is not int or not 0 < block <= 16*1024*1024:
                raise ValueError('Invalid archive block size.')
            entry.update(size=len(updated),integrity=dict(algorithm='SHA256',blockSize=block,
                hash=hashlib.sha256(updated).hexdigest(),
                blocks=[hashlib.sha256(updated[i:i+block]).hexdigest() for i in range(0,len(updated),block)]))
        for _, entry in entries:
            offset = int(entry['offset'])
            entry['offset'] = str(offset + sum(len(data)-size for start,size,data in segments if start<offset))
        document = json.dumps(header,ensure_ascii=False,separators=(',',':')).encode()
        payload = struct.pack('<I',len(document))+document+b'\0'*(-len(document)%4)
        with Path(destination).open('wb') as output:
            output.write(struct.pack('<III',4,len(payload)+4,len(payload))+payload)
            position = 0
            for offset,size,data in sorted(segments):
                stream.seek(base+position)
                left=offset-position
                while left:
                    chunk=stream.read(min(left,1024*1024))
                    if not chunk:raise ValueError('Truncated archive.')
                    output.write(chunk);left-=len(chunk)
                output.write(data);position=offset+size
            stream.seek(base+position)
            shutil.copyfileobj(stream,output)
    return dict(module=name, original_sha256=hashlib.sha256(before).hexdigest(), patched_sha256=hashlib.sha256(after).hexdigest())


def prepare(root, app):
    source = Path(app['executable']).resolve().parent
    archive = source/'resources/app.asar'
    stat = archive.stat()
    adapter = Path(__file__).with_name('desktop_record_sync.cjs')
    identity = dict(source=str(source), version=app['Version'], size=stat.st_size, modified=stat.st_mtime_ns,
        signal_files=hashlib.sha256(Path(__file__).with_name('desktop_signal_files.cjs').read_bytes()).hexdigest(),
        adapter=hashlib.sha256(adapter.read_bytes()).hexdigest(),
        workspace=hashlib.sha256(Path(__file__).with_name('desktop_workspace_sync.cjs').read_bytes()).hexdigest(),
        local_workspace=hashlib.sha256(Path(__file__).with_name('desktop_local_workspace_sync.cjs').read_bytes()).hexdigest(),
        plugin=hashlib.sha256(Path(__file__).with_name('desktop_plugin_sync.cjs').read_bytes()).hexdigest(),
        plugin_renderer=hashlib.sha256(Path(__file__).with_name('desktop_plugin_renderer_sync.cjs').read_bytes()).hexdigest(),
        membership=hashlib.sha256(Path(__file__).with_name('desktop_project_membership.cjs').read_bytes()).hexdigest(),
        profile_resume=hashlib.sha256(Path(__file__).with_name('desktop_profile_resume.cjs').read_bytes()).hexdigest(),
        renderer=hashlib.sha256(Path(__file__).with_name('desktop_renderer_record_sync.cjs').read_bytes()).hexdigest(),
        network=hashlib.sha256(Path(__file__).with_name('desktop_network_policy.cjs').read_bytes()).hexdigest(),
        publication=hashlib.sha256(Path(__file__).with_name('desktop_publication.py').read_bytes()).hexdigest(),
        patch=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    key = hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()[:16]
    target = Path(root).resolve()/'artifacts/original-sync-desktop'/key
    from .desktop_publication import publish
    target, value, fallback = publish(source, target, identity, 'original-sync.json',
        patch_archive, check_archive_support, _long_path)
    atomic_json(target.parent/'last-selection.json',dict(directory=str(target),
        installed_version=app['Version'], selected_version=value['source']['version'], compatibility_notice=fallback))
    return target/'ChatGPT.exe'
