"""Version-checked original-account companion; installed package stays untouched."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
from uuid import uuid4

from .desktop_bundle import read_header, _entries, check_archive_support, _long_path, seal_archive_integrity
from .store import atomic_json

PATCHES = {
    b'getGlobalStateValue(e){return': b'getGlobalStateValue(e){globalThis.__codexWorkspaceSync?.register(this.globalState,this.windowManager,this.remoteConnectionsHandler);globalThis.__codexLocalWorkspaceSync?.register(this.globalState,this.windowManager);return',
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


# 26.930 (webview app-shared chunk): onNotification takes a timestamp, and the
# hydration guard also checks a cancellation callback l.
RENDERER_PATCHES_930 = {
    b'onNotification(e,t,n=null,r,i=Date.now()){if(this.assertActive(),':
        b'onNotification(e,t,n=null,r,i=Date.now()){globalThis.__codexRendererRecordSync?.observe(this,e,t);if(this.assertActive(),',
    b'this.requestClient=n;let y=this.settings.restricted;': RENDERER_PATCHES[b'this.requestClient=n;let y=this.settings.restricted;'],
    b'shouldApplyHydratedThread:()=>f===this.hydrationGeneration&&(l?.()??!0)&&(n?.isCurrent()??!0)':
        b'shouldApplyHydratedThread:()=>f===this.hydrationGeneration&&(l?.()??!0)&&(n?.isCurrent()??!0)'
        b'&&(globalThis.__codexRendererRecordSync?.canApply(this)??true)',
}


def renderer_patches_for(data):
    found = [patches for patches in (RENDERER_PATCHES, RENDERER_PATCHES_930)
             if all(data.count(key) == 1 for key in patches)]
    return found[0] if len(found) == 1 else None


# Renderer names: selected host, host managers, archived IDs per host, catalog
# enabled, unique catalog host, catalog state, derived atom, its store and the
# constructor of the catalog-enabled atom.
#
# 26.930.4958 swapped two catalog bindings: uFn is now the initial catalog
# state and dFn=zk(Q,!1) the enabled flag (set by nFn, read as t(dFn)?t(TZ,e)
# in the same chunk, exported under the alias 3930 used for uFn). This patch
# stays optional on 26.930 too (not in require_project_grouping): without it a
# migrated task is hidden only when its ID is also archived on another host and
# it was not opened in this window (the native any-host fallback); the
# grouping patches instead decide whether remote threads are shown at all.
_HOST_IDENTITY_VARIANTS = (
    (b'RE', b'WE', b'n5n', b'AT', b'QZn', b'MT', b'uf', b'$', b'rf'),  # 26.915
    (b'oE', b'pE', b'KRn', b'Fw', b'Ckn', b'Rw', b'ns', b'X', b'Go'),  # 26.917
    (b'P3', b'B3', b'J3', b'uFn', b'gFn', b'wZ', b'Kk', b'Q', b'zk'),  # 26.930.3930 (app-shared)
    (b'F3', b'V3', b'Y3', b'dFn', b'_Fn', b'wZ', b'Kk', b'Q', b'zk'),  # 26.930.4958 (app-shared)
)


def renderer_host_identity_patches(data):
    # Verified 26.915 and 26.917 renderers: archive suppression falls back to ANY
    # host when a task has not been opened in this window. A migrated task can
    # keep its ID on Windows while its SSH original is archived. Prefer the
    # unique active catalog host before that fallback; an explicitly selected
    # host still wins. The catalog helper already returns null for ambiguous IDs
    # and excludes ChatGPT entries.
    patches = {}
    for selected, managers, archived, enabled, unique, catalog_state, atom, store, ctor in _HOST_IDENTITY_VARIANTS:
        before = (b'let n=%s(t,e);return n==null?t(%s).some(n=>t(%s,n.getHostId()).includes(e)):'
                  b't(%s,n).includes(e)' % (selected, managers, archived, archived))
        after = before.replace(b'let n=%s(t,e);' % selected,
                               b'let n=%s(t,e)??(t(%s)?t(%s,e):null);' % (selected, enabled, unique))
        catalog = (b'%s=%s(%s,(e,{get:t})=>{let n=null;for(let r of t(%s).entriesByKey.values())'
                   b'if(r.sourceKind!==`chatgpt`&&r.threadId===e){if(n!=null&&n!==r.hostId)'
                   b'return null;n=r.hostId}return n})' % (unique, atom, store, catalog_state))
        if data.count(before) > 1:
            raise ValueError('Ambiguous desktop host-specific archive filter.')
        # The replacement also reads the catalog-enabled atom; it must be this
        # renderer's own declaration, not a name that happens to be absent.
        declared = len(re.findall(rb'(?<![\w$])' + re.escape(b'%s=%s(%s,!1)' % (enabled, ctor, store)), data))
        if data.count(before) == 1 and data.count(catalog) == 1 and declared == 1:
            patches[before] = after
    if len(patches) > 1:
        raise ValueError('Ambiguous desktop host-specific archive filter.')
    return patches


# Renderer names in the sidebar grouping function (DXr in 26.917): same-machine
# host test, git origins for a host, groupBy, Codex worktree project lookup,
# worktree-root test, linked git worktree test, worktree remap, and the exact
# and canonical (pwd -P) remote project root lookups.
_REMOTE_ROOT_VARIANTS = (
    (b'TYr', b'gXr', b'wXr', b'hXr', b'R', b'_Xr', b'vXr', b'CYr', b'dXr'),  # 26.917
    (b'NFn', b'MIn', b'BIn', b'jIn', b'Ch', b'NIn', b'PIn', b'jFn', b'DIn'),  # 26.930.3930 (UIn)
    (b'yFn', b'yIn', b'DIn', b'vIn', b'Yu', b'bIn', b'xIn', b'_Fn', b'mIn'),  # 26.930.4958 (AIn)
)


def _remote_root_section(affinity, origins, group_by, codex_worktree, worktree, linked, remap, exact, canonical,
                         groups=b't'):
    # The whole remote part of the grouping function, from the remote-host flag
    # p and the remote projects m to the placement lookup after the remap, so
    # every binding the guard reads is this function's own. groups is the
    # remap's project list: the native t or the remote order (_remote_order).
    condition = b'%s(d,a,s?.worktreesRootsByHostId?.[c])||p&&%s(d,_)' % (worktree, linked)
    before = (b'let p=c!==o,m=s?.remoteProjects,h=s?.remoteConnections?.find(e=>e.hostId===c),'
              b'g=(m??[]).filter(e=>{if(e.hostId===c)return!1;let t=s?.remoteConnections?.find(t=>t.hostId===e.hostId);'
              b'return %s(h,t)});if(p&&g.length===0&&!m?.some(e=>e.hostId===c))return;'
              b'let _=%s({gitOrigins:r,gitOriginsByHostId:i,hostId:c??void 0,primaryHostId:o}),'
              b'v=[...p?Object.entries((0,%s.default)(g,e=>e.hostId)).flatMap(([e,t])=>'
              b'%s(t,e,d,s?.codexHomesByHostId?.[e],s?.worktreesRootsByHostId?.[e])):[]];'
              b'if(v.length===1){(t.find(e=>e.projectId===v[0]?.id)??null)?.threadKeys.push(e.key);return}'
              b'if(v.length>1)return;if(%s){let r=%s(d,e.conversationId,%s,n,_,s?.threadWorkspaceRootHints,'
              b'e.summary!=null);r&&(f=r)}let y=(m??[]).filter(e=>e.hostId===c),'
              b'b=%s(m,c,f)??%s(y,f,s?.canonicalProjectPathsByHostId)??%s(g,f,s?.canonicalProjectPathsByHostId);'
              % (affinity, origins, group_by, codex_worktree, condition, remap, groups, exact, canonical, canonical))
    guard = (b'!(p&&(%s(m,c,d)??%s((m??[]).filter(e=>e.hostId===c),d,s?.canonicalProjectPathsByHostId)))'
             % (exact, canonical))
    return before, before.replace(b'if(%s){' % condition, b'if(%s&&(%s)){' % (guard, condition))


# Beside the remap and linked-worktree test of each grouping variant: the git
# origin lookup by folder and the path key it compares with (qF/Ar in 26.917).
_REMOTE_ORDER_VARIANTS = (
    (b'vXr', b'_Xr', b'qF', b'Ar'),  # 26.917
    (b'PIn', b'NIn', b'gj', b'Kf'),  # 26.930.3930
    (b'xIn', b'bIn', b'lFn', b'yh'),  # 26.930.4958
)


def _remote_order(names):
    """The remap's project list for remote threads, or None without verified names.

    Verified 26.917/26.930 remap (vXr, PIn, xIn): for a cwd in a linked git
    worktree it chooses among the projects that share the cwd's common dir.
    Where its rules (same repo-relative path, a matching root folder name, the
    thread's workspace hint) do not decide, it takes the first candidate in the
    profile's own sidebar order. A thread in an undeclared linked worktree thus
    went to the parent repo or to a declared worktree project by sidebar
    position, which differs per profile.
    For remote threads (p) the list is stably reordered first: a remote project
    of the thread's host whose git root is the cwd's git root (the declared
    worktree that holds the cwd, e.g. a subfolder of it), then one whose path
    git knows and that is no linked worktree itself (the repository), then the
    rest in sidebar order. The native rules still run on the reordered list;
    only the sidebar-order fallback changes. Local threads keep t. The
    bindings are the grouping function's own: t projects, c thread host, d cwd,
    _ that host's git origins (lookup by folder, key as the lookup compares).
    """
    remap, linked = names[6], names[5]
    found = [(lookup, key) for variant_remap, variant_linked, lookup, key in _REMOTE_ORDER_VARIANTS
             if (variant_remap, variant_linked) == (remap, linked)]
    if len(found) != 1:
        return None
    (lookup, key), = found
    return (b'p?((w=%(lookup)s(d,_))=>t.map(g=>[g.projectKind===`remote`&&g.hostId===c&&g.path!=null?'
            b'(k=>k==null?0:k.root!=null&&w?.root!=null&&%(key)s(k.root)===%(key)s(w.root)?2:%(linked)s(g.path,_)?0:1)'
            b'(%(lookup)s(g.path,_)):0,g]).sort((x,y)=>y[0]-x[0]).map(x=>x[1]))():t'
            % {b'lookup': lookup, b'key': key, b'linked': linked})


def _remote_root_forms(names):
    # Every form the bundles leave: native, guarded, guarded with the order.
    native, guarded = _remote_root_section(*names)
    order = _remote_order(names)
    return (native, guarded) if order is None else (native, guarded, _remote_root_section(*names, groups=order)[1])


def renderer_remote_root_patches(data):
    # Verified 26.917 renderer: when a remote thread's cwd is a linked git
    # worktree and git origins are known for that directory (a selected, live
    # thread has no summary, so its cwd is queried), the remap compares the
    # declared project paths with git's physical toplevel. A project declared
    # through a symlink (/home/<user>/x for /srv/x) then never matches exactly
    # and every thread of the worktree project falls back to the first project
    # sharing the git common dir in this profile's sidebar order. Native code
    # guards local project roots only (uXr). Give remote roots the same guard:
    # when the cwd is exactly, or canonically, a project root declared on its
    # host, that project wins and the remap is skipped. Local threads (p false)
    # and remote cwds that are no declared root keep the native path.
    patches = {}
    for names in _REMOTE_ROOT_VARIANTS:
        before, after = _remote_root_section(*names)
        if data.count(before) > 1:
            raise ValueError('Ambiguous desktop remote project grouping.')
        if data.count(before) == 1:
            patches[before] = after
    if len(patches) > 1:
        raise ValueError('Ambiguous desktop remote project grouping.')
    return patches


def _remote_root_names(data):
    # The grouping variant this chunk holds, in any form the bundles leave.
    found = [names for names in _REMOTE_ROOT_VARIANTS
             if sum(data.count(form) for form in _remote_root_forms(names)) == 1]
    return found[0] if len(found) == 1 else None


def _remote_order_call(remap):
    # The remap call inside the section; the guard changes only the condition before it.
    return (b'{let r=%s(d,e.conversationId,t,n,_,s?.threadWorkspaceRootHints,e.summary!=null);r&&(f=r)}'
            % remap)


def renderer_remote_order_patches(data):
    # Sidebar-order independence for remote worktree threads (_remote_order).
    # Applied only beside this build's remote-root section (same chunk, same
    # names), like the folder lookup; the bundles install the guard first and
    # this call lies after the condition the guard changes. The call is patched
    # where the section holds it (native or guarded form), exactly once; more
    # than one call is refused and an ordered section is left as it is.
    names = _remote_root_names(data)
    order = None if names is None else _remote_order(names)
    if order is None:
        return {}
    before = _remote_order_call(names[6])
    count = data.count(before)
    if count > 1:
        raise ValueError('Ambiguous desktop remote worktree project order.')
    if count == 0 or not any(data.count(form) == 1 and before in form for form in _remote_root_section(*names)):
        return {}
    return {before: before.replace(b'e.conversationId,t,n,_,', b'e.conversationId,%s,n,_,' % order)}


# Renderer names in the sidebar's git-info folder collector (gIn in
# 26.930.4958, kIn in 26.930.3930, pXr in 26.917): the same-machine host test
# and the Codex worktree-root test (the grouping function's worktree name).
_SUMMARY_DIR_VARIANTS = (
    (b'zs', b'R'),  # 26.917
    (b'Pc', b'Ch'),  # 26.930.3930
    (b'Fi', b'Yu'),  # 26.930.4958
)


def _summary_dir_section(local_host, worktree):
    # One thread row of the collector, from its host binding e (t is the
    # collector's primary host) to the skip, so e and t are its own bindings.
    before = (b'let e=n.hostId==null||%s(n.hostId)?t:n.hostId,c=o?.threadProjectAssignments?.[n.conversationId];'
              b'if(!(o?.projectlessThreadIds?.has(n.conversationId)||c?.projectKind===`local`&&'
              b'(c.projectOrigin===`chatgpt`||d.has(c.projectId))||c?.projectKind===`remote`&&c.hostId===e&&'
              b'r.some(t=>t.id===c.projectId&&t.hostId===e))&&n.cwd&&f(e,n.cwd),'
              b'n.summary!=null&&!%s(n.cwd,i,a?.[e])||n.workspaceKind===`projectless`||n.cwd===`~`)continue'
              % (local_host, worktree))
    return before, before.replace(b'n.summary!=null&&!', b'n.summary!=null&&e===t&&!')


def renderer_summary_dir_patches(data):
    # Verified 26.930.4958 renderer (3930 is the same code up to names): the
    # sidebar asks git about a thread's cwd only for rows without a summary
    # (live, loaded threads) or inside a Codex worktree. A remote thread in a
    # linked git worktree folder that is no declared project root is placed
    # only through that answer (the common-dir remap); without it the
    # grouping drops the thread (`if(p)return`). So
    # worktree threads appeared under the repo project only after one of them
    # was opened in that window. Ask for remote rows with a summary too. The
    # collector still skips assigned, projectless and `~` rows, hosts without
    # a connected remote project, and local rows (unchanged).
    #
    # Querying every remote summary row makes the common-dir remap run for
    # threads of a worktree project declared through a symlink as well, which
    # is the revision 96 symptom for every such thread. The patch is therefore
    # applied only beside this build's remote-root guard (same chunk, same
    # worktree test): unguarded, the native lookup stays.
    found = []
    for local_host, worktree in _SUMMARY_DIR_VARIANTS:
        before, after = _summary_dir_section(local_host, worktree)
        count = data.count(before)
        if count > 1:
            raise ValueError('Ambiguous desktop remote worktree folder lookup.')
        if count == 1:
            found.append((before, after, worktree))
    if len(found) > 1:
        raise ValueError('Ambiguous desktop remote worktree folder lookup.')
    if not found:
        return {}
    (before, after, worktree), = found
    grouping = _remote_root_names(data)
    if grouping is None or grouping[4] != worktree:
        return {}
    return {before: after}


def project_grouping_patched(data):
    """True when data holds the guarded, ordered grouping and the summary-row lookup."""
    names = _remote_root_names(data)
    order = None if names is None else _remote_order(names)
    if order is None or data.count(_remote_root_section(*names, groups=order)[1]) != 1:
        return False
    return sum(data.count(_summary_dir_section(*pair)[1])
               for pair in _SUMMARY_DIR_VARIANTS if pair[1] == names[4]) == 1


def require_project_grouping(version, chunks):
    """Refuse a 26.930 renderer whose SSH worktree grouping is not patched.

    All three grouping patches are required there: the remote-root guard, the
    remote project order and the summary-row folder lookup. Other versions
    keep the rule of the other optional renderer patches (host filter, plugin
    refresh): a missing variant leaves the native grouping. On 26.930, the
    managed release line, a silent skip hides remote worktree threads or
    places them by sidebar order, so publication stops until the names are
    added. chunks are the final app-initial/app-shared sources; an archive
    patched earlier (isolated fixture) passes as it is.
    """
    if not isinstance(version, str) or not version.startswith('26.930.'):
        return
    if sum(1 for data in chunks if project_grouping_patched(data)) != 1:
        raise ValueError(f'이 Codex 버전({version})의 SSH worktree 프로젝트 분류 위치를 확인하지 못했습니다. '
                         '관리 앱 호환성 업데이트가 필요합니다.')


def archive_version(stream, base, entries):
    """The desktop version in the archive's package.json, or None."""
    item = dict(entries).get('package.json')
    if item is None or not 0 < item.get('size', 0) <= 1024 * 1024:
        return None
    stream.seek(base + int(item['offset']))
    try:
        value = json.loads(stream.read(item['size']))
    except ValueError:
        return None
    version = value.get('version') if isinstance(value, dict) else None
    return version if isinstance(version, str) else None


# Main process (t8e in 26.917, B6e in 26.915): before a local project gains
# folders (create, add folder, edit folders), the desktop pins every unassigned
# task in those folders as projectless, so the project starts empty. Only the
# window that made the change keeps the pin. Peers get only the shared
# declaration and group those tasks under the project by folder. The pins go
# through the membership adapter, which drops them in shared profiles.
# Explicit assignments to existing projects stay as they are. Without the
# adapter, the pins are unchanged.
_PROJECTLESS_PINS = re.compile(
    rb'(await [A-Za-z_$][\w$]*\.assignIfUnassigned\(\[\.\.\.Object\.entries\([A-Za-z_$][\w$]*\)'
    rb'\.flatMap\(\(\[e,t\]\)=>t==null\?\[\]:\[\{threadId:([A-Za-z_$][\w$]*\.[A-Za-z_$][\w$]*)\(e\),'
    rb'assignment:t,projectless:!1\}\]\),\.\.\.)([A-Za-z_$][\w$]*)'
    rb'(\.map\(e=>\(\{threadId:\2\(e\),assignment:null,projectless:!0\}\)\)\]\))')


def main_projectless_pin_patches(data):
    matches = list(_PROJECTLESS_PINS.finditer(data))
    if len(matches) > 1:
        raise ValueError('Ambiguous desktop project folder assignment.')
    if not matches:
        # Already patched, or a version without the verified expression: the
        # creating window keeps the native pins.
        return {}
    head, _, pins, tail = matches[0].groups()
    return {matches[0].group(0): head + b'(globalThis.__codexProjectMembership?.projectlessPins?.(' + pins +
            b')??' + pins + b')' + tail}


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
    return {**matches[0], **main_projectless_pin_patches(data)} if len(matches) == 1 else None


# 26.930 splits these points over main-*.js and bootstrap-*.js, binds the IPC
# message as a, gives onNotification a timestamp and the hydration guard a
# cancellation callback. Each point must still occur exactly once.
SPLIT_PATCHES = {
    b'getGlobalStateValue(e){return': PATCHES[b'getGlobalStateValue(e){return'],
    b'ensureProjectsReady(){if(this.disposed||!this.connected)': PATCHES[b'ensureProjectsReady(){if(this.disposed||!this.connected)'],
    b'if(a.type===`remote-hosted-pip-active-thread-changed`){':
        b'if(a.type===`manager-record-changed`){globalThis.__codexRecordSync?.publish(a.threadId,a.hostId,a.kind);return}'
        b'if(a.type===`remote-hosted-pip-active-thread-changed`){',
    b'onNotification(e,t,n=null,r,i=Date.now()){if(this.assertActive(),':
        b'onNotification(e,t,n=null,r,i=Date.now()){globalThis.__codexRecordSync?.observe(this,e,t);if(this.assertActive(),',
    b'this.requestClient=n;let b=this.settings.restricted;':
        b'this.requestClient=n;globalThis.__codexRecordSync?.register(this);let b=this.settings.restricted;',
    b'requestStartupSync(){if(!this.syncEnabled)': PATCHES[b'requestStartupSync(){if(!this.syncEnabled)'],
    b'shouldApplyHydratedThread:()=>p===this.hydrationGeneration&&(u?.()??!0)&&(n?.isCurrent()??!0)':
        b'shouldApplyHydratedThread:()=>p===this.hydrationGeneration&&(u?.()??!0)&&(n?.isCurrent()??!0)'
        b'&&(globalThis.__codexRecordSync?.canApply(this)??true)',
}


def main_sync_plan(modules):
    """({module: patches}, adapter module) for the main-process sync, or None.

    modules is [(name, data)] of every main-process bundle. The adapters go to
    the module holding getGlobalStateValue, which also creates the others.
    """
    single = [(name, patches) for name, data in modules if (patches := patches_for(data))]
    if len(single) > 1:
        raise ValueError('Ambiguous desktop main synchronization entry point.')
    if single:
        return {single[0][0]: single[0][1]}, single[0][0]
    located = {}
    for key in SPLIT_PATCHES:
        holders = [(name, data.count(key)) for name, data in modules if key in data]
        if len(holders) != 1 or holders[0][1] != 1:
            return None
        located[key] = holders[0][0]
    plan = {}
    for key, name in located.items():
        plan.setdefault(name, {})[key] = SPLIT_PATCHES[key]
    pins = [(name, found) for name, data in modules if (found := main_projectless_pin_patches(data))]
    if len(pins) > 1:
        raise ValueError('Ambiguous desktop project folder assignment.')
    for name, found in pins:
        plan.setdefault(name, {}).update(found)
    return plan, located[b'getGlobalStateValue(e){return']


def patch_archive(source, destination):
    with Path(source).open('rb') as stream:
        header, base = read_header(stream)
        entries = list(_entries(header))
        main_modules, renderers, webviews = [], [], {}
        for name, entry in entries:
            web = name.startswith('webview/assets/app-initial') or name.startswith('webview/assets/app-shared')
            if not name.endswith('.js') or not (name.startswith('.vite/build/') or web):
                continue
            stream.seek(base + int(entry['offset']))
            data = stream.read(entry['size'])
            if not web:
                main_modules.append((name, entry, data))
                continue
            webviews[name] = (entry, data)
            patches = renderer_patches_for(data)
            if patches:
                renderers.append((name, entry, data, patches))
        plan = main_sync_plan([(name, data) for name, _, data in main_modules])
        if plan is None or len(renderers) != 1:
            raise ValueError('Desktop main/renderer synchronization entry points are not verified for this version.')
        module_patches, name = plan
        entries_by_name = {module: (entry, data) for module, entry, data in main_modules}
        entries_by_name.update(webviews)
        changes = {}
        helper = lambda file: Path(__file__).with_name(file).read_bytes() + b'\n'
        for module, replacements in module_patches.items():
            updated = entries_by_name[module][1]
            for pattern, replacement in replacements.items():
                updated = updated.replace(pattern, replacement)
            if module == name:
                updated = (helper('desktop_network_policy.cjs') + b''.join(helper(file) for file in (
                    'desktop_signal_files.cjs', 'desktop_workspace_sync.cjs', 'desktop_project_membership.cjs',
                    'desktop_local_workspace_sync.cjs', 'desktop_plugin_sync.cjs')) + helper('desktop_profile_resume.cjs')
                    + helper('desktop_record_sync.cjs') + updated)
            changes[module] = updated
        renderer, _, data, replacements = renderers[0]
        updated = data
        for pattern, replacement in {**replacements, **renderer_plugin_patches(data)}.items():
            updated = updated.replace(pattern, replacement)
        changes[renderer] = updated
        # 26.917 keeps the host filter and grouping beside the sync; 26.930
        # moved the filter to app-shared. Each exists in at most one chunk.
        # The project order and the folder lookup follow the remote-root guard
        # of their chunk; the guard goes first (its section holds the call).
        for patches_of, label in ((renderer_host_identity_patches, 'host-specific archive filter'),
                                  (renderer_remote_root_patches, 'remote project grouping'),
                                  (renderer_remote_order_patches, 'remote worktree project order'),
                                  (renderer_summary_dir_patches, 'remote worktree folder lookup')):
            found = [(module, patches) for module, (_, data) in webviews.items() if (patches := patches_of(data))]
            if len(found) > 1:
                raise ValueError('Ambiguous desktop ' + label + '.')
            for module, patches in found:
                updated = changes.get(module, webviews[module][1])
                for pattern, replacement in patches.items():
                    updated = updated.replace(pattern, replacement)
                changes[module] = updated
        require_project_grouping(archive_version(stream, base, entries),
                                 [changes.get(module, data) for module, (_, data) in webviews.items()])
        changes[renderer] = (helper('desktop_profile_resume.cjs') + helper('desktop_plugin_renderer_sync.cjs')
                             + helper('desktop_renderer_record_sync.cjs') + changes[renderer])
        segments = []
        for module, updated in changes.items():
            entry = entries_by_name[module][0]
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
    return dict(module=name, original_sha256=hashlib.sha256(entries_by_name[name][1]).hexdigest(),
                patched_sha256=hashlib.sha256(changes[name]).hexdigest())


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
        patch_archive, check_archive_support, _long_path, seal_archive_integrity)
    atomic_json(target.parent/'last-selection.json',dict(directory=str(target),
        installed_version=app['Version'], selected_version=value['source']['version'], compatibility_notice=fallback))
    return target/'ChatGPT.exe'
