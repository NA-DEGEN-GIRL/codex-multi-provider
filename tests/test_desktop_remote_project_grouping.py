"""An SSH worktree project keeps its threads when one of them is opened.

The 26.917 sidebar grouping remaps a remote thread whose cwd is a linked git
worktree to a project that shares the git common dir. For projects declared
through a symlink, the remap cannot match git's physical toplevel. It then
falls back to the first such project in the profile's own sidebar order, so
opening a thread of the worktree project moved every thread of that project
under the parent repo project. The renderer patch keeps a declared remote root
(exact or canonical) for its own project and leaves every other path native.
"""
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from manager_core import desktop_bundle, original_sync_bundle
from manager_core.original_sync_bundle import renderer_remote_root_patches

# Verbatim 26.917 renderer (app-initial) sources: the sidebar grouping function
# and every grouping helper it reaches. The bundle guard checks them against a
# managed 26.917 copy when one exists on this machine.
DXR_917 = (
    r'''DXr=(e,t,n,r,i,a,o=df,s)=>{let c=e.hostId==null||zs(e.hostId)?o:e.hostId,l=s?.threadProjectAssignments?.[e.conversationId];'''
    r'''if(c!==o&&s?.enabledRemoteHostIds!=null&&!s.enabledRemoteHostIds.has(c))return;'''
    r'''let u=l!=null&&(l.projectKind===`local`||l.hostId!=null&&c===l.hostId)?fXr(l,t):null;'''
    r'''if(u!=null){u.threadKeys.push(e.key);return}if(l?.projectKind===`local`&&l.projectOrigin===`chatgpt`)return;'''
    r'''let d=e.cwd;if(!d||!bYr(d).length)return;let f=d;'''
    r'''if(e.workspaceKind===`projectless`||s?.projectlessThreadIds?.has(e.conversationId)===!0)return;'''
    r'''let p=c!==o,m=s?.remoteProjects,h=s?.remoteConnections?.find(e=>e.hostId===c),g=(m??[]).filter(e=>{if(e.hostId===c)return!1;'''
    r'''let t=s?.remoteConnections?.find(t=>t.hostId===e.hostId);return TYr(h,t)});'''
    r'''if(p&&g.length===0&&!m?.some(e=>e.hostId===c))return;'''
    r'''let _=gXr({gitOrigins:r,gitOriginsByHostId:i,hostId:c??void 0,primaryHostId:o}),v=[...p?Object.entries((0,wXr.default)(g,e=>e.hostId)).flatMap(([e,t])=>hXr(t,e,d,s?.codexHomesByHostId?.[e],s?.worktreesRootsByHostId?.[e])):[]];'''
    r'''if(v.length===1){(t.find(e=>e.projectId===v[0]?.id)??null)?.threadKeys.push(e.key);'''
    r'''return}if(v.length>1)return;'''
    r'''if(R(d,a,s?.worktreesRootsByHostId?.[c])||p&&_Xr(d,_)){let r=vXr(d,e.conversationId,t,n,_,s?.threadWorkspaceRootHints,e.summary!=null);'''
    r'''r&&(f=r)}let y=(m??[]).filter(e=>e.hostId===c),b=CYr(m,c,f)??dXr(y,f,s?.canonicalProjectPathsByHostId)??dXr(g,f,s?.canonicalProjectPathsByHostId);'''
    r'''if(b!=null){let n=t.find(e=>e.projectId===b.id)??null;if(n!=null){n.threadKeys.push(e.key);'''
    r'''return}}if(p)return;let x=lXr(n,f);'''
    r'''x&&(x.threadKeys.push(e.key),f!==d&&s?.onDiscoverThreadWorkspaceRootHint?.(e.conversationId,x.path))}'''
)

HELPERS_917 = {
    'eXr': (
        r'''function eXr(e,t,n,r,i,a){let o=n.map(e=>({...e,threadKeys:[]})),s=(0,CXr.default)((r??[]).flatMap(({dir:e,originUrl:t})=>{let n=t?dYr(t):null;'''
        r'''return n?[[Ar(e),n]]:[]})),c=(0,wXr.default)(t??[],e=>e.label),l=$Yr(o);'''
        r'''return e.forEach(e=>{if(e.kind===`local`)e.pendingWorktree==null?e.pendingThreadStart==null?DXr(e,o,l,r,a?.gitOriginsByHostId,i,a?.primaryHostId,a):fXr(e.pendingThreadStart.task.projectAssignment??void 0,o)?.threadKeys.push(e.key):yXr(e,s,o,l);'''
        r'''else if(e.kind===`remote`){let t=fXr(a?.threadProjectAssignments?.[e.task.id],o);'''
        r'''if(t!=null){t.threadKeys.push(e.key);return}if(a?.projectlessThreadIds?.has(e.task.id)===!0)return;'''
        r'''bXr(e,c,o,l)}}),o}'''
    ),
    '$Yr': (
        r'''function $Yr(e){let t=e.filter(e=>e.projectKind===`local`),n=new Set(t.flatMap(e=>JF(e).map(Ar))),r=new Map;'''
        r'''for(let e of t){let t=[...JF(e).map(e=>({alias:e,path:e})),...rXr(e).filter(({alias:e})=>!n.has(Ar(e)))];'''
        r'''for(let{alias:n,path:i}of t){let t=Ar(n),a=e.path===i&&nXr(e)?e:{...e,path:i},o=r.get(t);'''
        r'''(o==null||iXr(a,o))&&r.set(t,a)}}return r}'''
    ),
    'rXr': (
        r'''function rXr(e){return[...e.pathAlias==null||e.path==null?[]:[{alias:e.pathAlias,path:e.path}],...e.rootPathAliases??[]]}'''
    ),
    'iXr': (
        r'''function iXr(e,t){return Sbe({createdAt:e.projectCreatedAt??0,projectId:e.projectId,rootPath:e.path,rootPaths:JF(e)},{createdAt:t.projectCreatedAt??0,projectId:t.projectId,rootPath:t.path,rootPaths:JF(t)})}'''
    ),
    'nXr': (
        r'''function nXr(e){return e.path!=null}'''
    ),
    'JF': (
        r'''function JF(e){return e.rootPaths??(e.path==null?[]:[e.path])}'''
    ),
    'fXr': (
        r'''function fXr(e,t){return e==null?null:t.find(t=>t.projectId!==e.projectId||t.projectKind!==e.projectKind?!1:e.projectKind===`local`||t.hostId===e.hostId)??null}'''
    ),
    'gXr': (
        r'''function gXr({gitOrigins:e,gitOriginsByHostId:t,hostId:n,primaryHostId:r}){return n&&t?.[n]?t[n]:n&&t&&n!==r?[]:e??[]}'''
    ),
    'hXr': (
        r'''function hXr(e,t,n,r,i){let a=ie(n),o=i??(r==null?null:Mle(r)),s=o==null?null:ie(o),c=a.lastIndexOf(`/.codex/worktrees/`),l=s!=null&&a.startsWith(`${s}/`)?s.length+1:c===-1?null:c+18;'''
        r'''if(l==null)return[];let u=a.slice(l).split(`/`).filter(Boolean);'''
        r'''if(u.length<2||!/^[0-9a-f]{4,}$/i.test(u[0]??``)&&!sie.safeParse(u[0]).success)return[];'''
        r'''let d=u.slice(1),f=(e??[]).filter(e=>e.hostId===t);for(let e=d.length;e>0;'''
        r'''--e){let t=d.slice(0,e).join(`/`),n=f.filter(e=>{let n=ie(e.remotePath);return n===t||n.endsWith(`/${t}`)});'''
        r'''if(n.length>0)return n}return[]}'''
    ),
    '_Xr': (
        r'''function _Xr(e,t){let n=qF(e,t??[]);'''
        r'''return n?.commonDir?Ar(n.commonDir).replace(/\/+$/,``)!==`${Ar(n.root).replace(/\/+$/,``)}/.git`:!1}'''
    ),
    'vXr': (
        r'''function vXr(e,t,n,r,i,a,o=!1){if(uXr(r,e))return null;let s=a?.[t],c=s?lXr(r,s):null;'''
        r'''if(!i)return c?.path??null;let l=qF(e,i);if(!l)return c?.path??null;'''
        r'''let u=l.originUrl,d=e=>e?u?e.originUrl===u:e.commonDir===l.commonDir:!1,f=Ar(e),p=yYr(e,i),m=n.flatMap(e=>{if(!nXr(e))return[];'''
        r'''let t=Ar(e.path);if(e.isCodexWorktree&&f!==t)return[];'''
        r'''let n=(e.projectKind===`local`?JF(e):[e.path]).flatMap(e=>{let t=qF(e,i);'''
        r'''return t==null||!d(t)?[]:[{repoPath:yYr(e,i),origin:t}]});'''
        r'''return n.length===0?[]:[{group:e,matchingRepos:n}]}),h=m.filter(({matchingRepos:e})=>e.some(({repoPath:e})=>e===p)),g=mYr(l.root,h.map(({matchingRepos:e})=>e.filter(({repoPath:e})=>e===p).map(({origin:e})=>e.root))),_=g==null?null:h[g];'''
        r'''if(_)return _.group.path;if(c&&m.some(({group:e})=>e===c))return c.path;'''
        r'''let v=m.filter(({matchingRepos:e})=>e.some(({repoPath:e})=>e===``)),y=mYr(l.root,v.map(({matchingRepos:e})=>e.filter(({repoPath:e})=>e===``).map(({origin:e})=>e.root))),b=y==null?null:v[y];'''
        r'''if(b)return b.group.path;let x=m[0];return x?x.group.path:o?c?.path??null:null}'''
    ),
    'uXr': (
        r'''function uXr(e,t){return e.has(Ar(t))}'''
    ),
    'lXr': (
        r'''function lXr(e,t){return e.get(Ar(t))??null}'''
    ),
    'yYr': (
        r'''function yYr(e,t){let n=qF(e,t);if(n?.root==null)return``;let r=bYr(Ar(e)),i=bYr(Ar(n.root));'''
        r'''return r.slice(i.length).join(`/`)}'''
    ),
    'bYr': (
        r'''function bYr(e){return e.split(/[/\\]+/).filter(Boolean)}'''
    ),
    'qF': (
        r'''function qF(e,t){let n=Ar(e).replace(/\/+$/,``);return t.find(e=>Ar(e.dir).replace(/\/+$/,``)===n)??null}'''
    ),
    'mYr': (
        r'''function mYr(e,t){if(t.length===0)return null;let n=pr(e),r=t.findIndex(e=>e.some(e=>pr(e)===n));'''
        r'''return r===-1?0:r}'''
    ),
    'CYr': (
        r'''function CYr(e,t,n){if(t==null||e==null)return null;let r=ie(n);'''
        r'''return e.find(e=>e.hostId===t&&ie(e.remotePath)===r)??null}'''
    ),
    'dXr': (
        r'''function dXr(e,t,n){let r=ie(t);return e.find(e=>ie(n?.[e.hostId]?.[e.remotePath]??e.remotePath)===r)??null}'''
    ),
}

CONDITION = b'if(R(d,a,s?.worktreesRootsByHostId?.[c])||p&&_Xr(d,_)){let r=vXr('
GUARDED = (b'if(!(p&&(CYr(m,c,d)??dXr((m??[]).filter(e=>e.hostId===c),d,s?.canonicalProjectPathsByHostId)))'
           b'&&(R(d,a,s?.worktreesRootsByHostId?.[c])||p&&_Xr(d,_))){let r=vXr(')


def patched(source):
    data = source.encode() if isinstance(source, str) else source
    for before, after in renderer_remote_root_patches(data).items():
        data = data.replace(before, after)
    return data


class RemoteRootPatchSelectionTests(unittest.TestCase):
    def test_verified_grouping_function_gets_one_guard(self):
        source = DXR_917.encode()
        patches = renderer_remote_root_patches(b'let x=1;' + source + b';let y=2')
        self.assertEqual(len(patches), 1)
        (before, after), = patches.items()
        self.assertEqual(source.count(before), 1)
        # Only the worktree-remap condition changes; the rest stays byte-identical.
        self.assertEqual(source.replace(before, after), source.replace(CONDITION, GUARDED))

    def test_missing_or_changed_function_stays_native(self):
        source = DXR_917.encode()
        self.assertEqual(renderer_remote_root_patches(b''), {})
        for old, new in ((b'CYr(m,c,f)', b'CYx(m,c,f)'), (b'let p=c!==o,', b'let p=c!=o,'),
                         (b'p&&_Xr(d,_)', b'_Xr(d,_)'), (b'dXr(y,f,', b'dXr(f,y,')):
            with self.subTest(old):
                mutated = source.replace(old, new)
                self.assertNotEqual(mutated, source)
                self.assertEqual(renderer_remote_root_patches(mutated), {})

    def test_ambiguous_function_is_refused(self):
        source = DXR_917.encode()
        with self.assertRaises(ValueError):
            renderer_remote_root_patches(source + b';' + source)

    def test_patched_renderer_is_not_patched_again(self):
        once = patched(DXR_917)
        self.assertEqual(once.count(GUARDED), 1)
        self.assertEqual(renderer_remote_root_patches(once), {})
        self.assertEqual(patched(once), once)


def write_archive(path, chunks):
    tree, offset = {'files': {}}, 0
    for name, data in chunks:
        node = tree
        *folders, leaf = name.split('/')
        for folder in folders:
            node = node['files'].setdefault(folder, {'files': {}})
        node['files'][leaf] = {'offset': str(offset), 'size': len(data)}
        offset += len(data)
    header = json.dumps(tree).encode()
    payload = struct.pack('<I', len(header)) + header + b'\0' * (-len(header) % 4)
    path.write_bytes(struct.pack('<III', 4, len(payload) + 4, len(payload)) + payload + b''.join(d for _, d in chunks))


def read_entries(path, accept):
    with Path(path).open('rb') as stream:
        header, base = desktop_bundle.read_header(stream)
        found = {}
        for name, item in desktop_bundle._entries(header):
            if accept(name):
                stream.seek(base + int(item['offset']))
                found[name] = stream.read(item['size'])
        return found


class RemoteRootArchiveTests(unittest.TestCase):
    RENDERER = 'webview/assets/app-initial-fixture.js'

    def test_managed_and_original_copies_install_the_guard(self):
        sync = b';'.join(original_sync_bundle.PATCHES)
        renderer = b';'.join(original_sync_bundle.RENDERER_PATCHES) + b';' + DXR_917.encode()
        copies = {
            'managed': (desktop_bundle.patch_archive, [
                ('.vite/build/main.js', b'function s9(){' + desktop_bundle._ORIGINAL + b'return "posix";}'),
                ('.vite/build/notifications.js', desktop_bundle._NOTIFICATION_CLICK + b'originalCallback();})' +
                 desktop_bundle._NOTIFICATION_SHOW + desktop_bundle._WINDOW_MESSAGE),
                ('.vite/build/browser-runtime.js', b'Qr({' + desktop_bundle._BROWSER_RUNTIME + b');'),
                ('.vite/build/context.js', sync),
                (self.RENDERER, desktop_bundle._CONTEXT_RENDERER + b';' + renderer)]),
            'original': (original_sync_bundle.patch_archive, [
                ('.vite/build/main.js', sync), (self.RENDERER, renderer)]),
        }
        with tempfile.TemporaryDirectory() as temporary:
            for label, (patch_archive, chunks) in copies.items():
                with self.subTest(label):
                    source, target = Path(temporary) / (label + '.asar'), Path(temporary) / (label + '-patched.asar')
                    write_archive(source, chunks)
                    patch_archive(source, target)
                    data = read_entries(target, lambda name: name == self.RENDERER)[self.RENDERER]
                    self.assertEqual(data.count(GUARDED), 1)
                    self.assertNotIn(CONDITION, data)


# Path helpers the grouping functions import from app-shared, reduced to what
# these paths need: Ar (path key: forward slashes, lower case, drive letters as
# /mnt/x), ie (normalized root), pr (base name), zs (primary host), Mle (Codex
# worktrees root) and R (inside a Codex worktrees root). No SSH host is the same
# machine as another (TYr); no origin URLs (dYr); no duplicate local roots.
STUBS = r'''
const Ar = e => { const t = e.replace(/\\/g, `/`).toLowerCase(), r = t.match(/^\/?([a-z]):(?:\/(.*))?$/);
  return r ? (r[2] ? `/mnt/${r[1]}/${r[2]}` : `/mnt/${r[1]}`) : t; };
const ie = e => { const t = Ar(e.trim()).replace(/\/+/g, `/`); return t === `/` ? t : t.replace(/\/+$/, ``); };
const pr = e => { const t = e.replace(/\\/g, `/`).replace(/\/+$/, ``); return t.split(`/`).at(-1) ?? t; };
const df = `local`, zs = e => e === df;
const Mle = e => `${e.replace(/[\\/]+$/, ``)}/worktrees`;
const R = (e, t, n) => { if (!e) return !1; const r = Ar(e);
  return [n?.trim(), t == null ? null : Mle(t)].some(x => { if (!x) return !1; const t = Ar(x).replace(/\/+$/, ``);
    return r === t || r.startsWith(`${t}/`); }) || /\/\.codex\/worktrees\/[^/]+\/./.test(r); };
const sie = {safeParse: () => ({success: !1})};
const TYr = () => !1, Sbe = () => !1, dYr = () => null;
const CXr = {default: Object.fromEntries};
const wXr = {default: (items, key) => { const out = {}; for (const item of items) (out[key(item)] ??= []).push(item); return out; }};
const yXr = () => { throw Error(`pending worktree`); }, bXr = () => { throw Error(`cloud task`); };
'''

# The sidebar atom's own call: eXr(items, cloud labels, groups in sidebar
# order, all origins, local Codex home, options). Returns each thread's project.
RUNNER = r'''
const results = CASES.map(c => {
  const groups = eXr(c.items, [], c.groups, Object.values(c.origins).flat(), c.codexHome, {
    canonicalProjectPathsByHostId: c.canonical ?? void 0, codexHomesByHostId: {}, worktreesRootsByHostId: {},
    gitOriginsByHostId: c.origins, primaryHostId: df, remoteConnections: [], remoteProjects: c.remoteProjects,
    threadProjectAssignments: {}, projectlessThreadIds: new Set, threadWorkspaceRootHints: {},
    onDiscoverThreadWorkspaceRootHint: () => {}});
  const placed = {};
  for (const group of groups) for (const key of group.threadKeys) placed[key] = group.projectId;
  return Object.fromEntries(c.items.map(item => [item.key, placed[item.key] ?? null]));
});
console.log(JSON.stringify(results));
'''

HOST = 'ssh:dev-server'
OTHER_HOST = 'ssh:build-server'  # a second machine with its own checkout at the same paths
COMMON = '/srv/projects/parent/.git'  # shared by the parent repo and its linked worktree
LOCAL_COMMON = 'C:/work/app/.git'
CODEX_HOME = 'C:/Users/dev/.codex'


def remote(project_id, path, host=HOST):
    return dict(id=project_id, hostId=host, label=project_id, remotePath=path)


def origin(directory, root, common=COMMON):
    return dict(dir=directory, root=root, commonDir=common, originUrl=None)


def thread(key, cwd, host=HOST):
    # A selected, live thread has no summary; that is when its cwd is queried.
    return dict(kind='local', key=key, conversationId='thread-' + key, hostId=host, cwd=cwd)


def local_group(project_id, path):
    return dict(groupId=project_id, projectId=project_id, projectKind='local', label=project_id, path=path,
                rootPaths=[path], isCodexWorktree=False, projectCreatedAt=0)


def case(order, items, origins, canonical='symlinked', local_groups=(), local_origins=(), other_origins=()):
    projects = sorted(order, key=lambda project: project['id'])  # declaration order, not the sidebar order
    groups = [*local_groups, *(dict(groupId=p['id'], projectId=p['id'], projectKind='remote', hostId=p['hostId'],
                                    hostDisplayName=None, label=p['label'], path=p['remotePath'], gitRepos=[],
                                    isCodexWorktree=False) for p in order)]
    return dict(groups=groups, remoteProjects=projects, items=items, codexHome=CODEX_HOME,
                origins={HOST: list(origins), OTHER_HOST: list(other_origins), 'local': list(local_origins)},
                canonical=CANONICAL if canonical == 'symlinked' else canonical)


# The SSH projects as declared: through a symlink (/home/dev/projects ->
# /srv/projects). studio is a linked git worktree of parent.
PARENT, STUDIO = remote('parent', '/home/dev/projects/parent'), remote('studio', '/home/dev/projects/studio')
CANONICAL = {HOST: {'/home/dev/projects/parent': '/srv/projects/parent',
                    '/home/dev/projects/studio': '/srv/projects/studio'}}
# The declared directories' origins: git reports the physical toplevels.
DECLARED = [origin('/home/dev/projects/parent', '/srv/projects/parent'),
            origin('/home/dev/projects/studio', '/srv/projects/studio')]
STUDIO_CWD = origin('/srv/projects/studio', '/srv/projects/studio')
FORWARD, REVERSE = (PARENT, STUDIO), (STUDIO, PARENT)
STUDIO_THREADS = [thread('a', '/srv/projects/studio'), thread('b', '/srv/projects/studio')]

# The same projects declared with their physical paths.
PARENT_P, STUDIO_P = remote('parent', '/srv/projects/parent'), remote('studio', '/srv/projects/studio')
PHYSICAL = [origin('/srv/projects/parent', '/srv/projects/parent'), STUDIO_CWD]
PHYSICAL_CANONICAL = {HOST: {'/srv/projects/parent': '/srv/projects/parent', '/srv/projects/studio': '/srv/projects/studio'}}

# Local projects: a repo root and a declared linked worktree of it.
LOCAL_GROUPS = (local_group('app', 'C:/work/app'), local_group('app-wt', 'C:/work/app-wt'))
LOCAL_ORIGINS = (origin('C:/work/app', 'C:/work/app', LOCAL_COMMON),
                 origin('C:/work/app-wt', 'C:/work/app-wt', LOCAL_COMMON),
                 origin('C:/work/app-other', 'C:/work/app-other', LOCAL_COMMON),
                 origin('C:/work/app/src', 'C:/work/app', LOCAL_COMMON),
                 origin(CODEX_HOME + '/worktrees/ab12/app', CODEX_HOME + '/worktrees/ab12/app', LOCAL_COMMON))
LOCAL_THREADS = [thread('root', 'C:\\work\\app', None), thread('declared-worktree', 'C:/work/app-wt', None),
                 thread('codex-worktree', CODEX_HOME + '/worktrees/ab12/app', 'local'),
                 thread('undeclared-worktree', 'C:/work/app-other', None), thread('subdirectory', 'C:/work/app/src', None)]
LOCAL_PLACEMENT = {'root': 'app', 'declared-worktree': 'app-wt', 'codex-worktree': 'app',
                   'undeclared-worktree': None, 'subdirectory': None}

# The second host declares only its parent checkout, with the physical path. A
# thread there in /srv/projects/studio (an undeclared linked worktree on that
# host) has the canonical root of studio on the first host as its cwd; only the
# thread's own host can make it a declared root.
OTHER_PARENT = remote('other-parent', '/srv/projects/parent', OTHER_HOST)
OTHER_ORIGINS = [origin('/srv/projects/parent', '/srv/projects/parent'), STUDIO_CWD]
OTHER_CANONICAL = {**CANONICAL, OTHER_HOST: {'/srv/projects/parent': '/srv/projects/parent'}}

# Opening a studio thread: native result, patched result.
SELECTED = {
    'parent first, before the cwd query': (case(FORWARD, STUDIO_THREADS, DECLARED), 'studio', 'studio'),
    'parent first, after the cwd query': (case(FORWARD, STUDIO_THREADS, DECLARED + [STUDIO_CWD]), 'parent', 'studio'),
    'studio first, before the cwd query': (case(REVERSE, STUDIO_THREADS, DECLARED), 'studio', 'studio'),
    'studio first, after the cwd query': (case(REVERSE, STUDIO_THREADS, DECLARED + [STUDIO_CWD]), 'studio', 'studio'),
}

# Paths the guard must leave native: expected placement, identical before and after the patch.
UNCHANGED = {
    'remote Codex worktree': (case(FORWARD, [thread('w', '/home/dev/.codex/worktrees/ab12/parent')], DECLARED + [
        origin('/home/dev/.codex/worktrees/ab12/parent', '/home/dev/.codex/worktrees/ab12/parent')]), {'w': 'parent'}),
    'remote subdirectory with its origin': (case(FORWARD, [thread('s', '/srv/projects/studio/src')], DECLARED + [
        origin('/srv/projects/studio/src', '/srv/projects/studio')]), {'s': 'parent'}),
    'remote subdirectory without its origin': (case(FORWARD, [thread('s', '/srv/projects/studio/src')], DECLARED),
                                               {'s': None}),
    'no canonical map, after the cwd query': (case(FORWARD, STUDIO_THREADS, DECLARED + [STUDIO_CWD], None),
                                              {'a': 'parent', 'b': 'parent'}),
    'no canonical map, before the cwd query': (case(FORWARD, STUDIO_THREADS, DECLARED, None), {'a': None, 'b': None}),
    'physical declarations, parent first': (case((PARENT_P, STUDIO_P), [thread('a', '/srv/projects/studio'),
        thread('p', '/srv/projects/parent')], PHYSICAL, PHYSICAL_CANONICAL), {'a': 'studio', 'p': 'parent'}),
    'physical declarations, studio first': (case((STUDIO_P, PARENT_P), [thread('a', '/srv/projects/studio'),
        thread('p', '/srv/projects/parent')], PHYSICAL, PHYSICAL_CANONICAL), {'a': 'studio', 'p': 'parent'}),
    'local roots, worktrees and subdirectories': (case(FORWARD, LOCAL_THREADS, DECLARED + [STUDIO_CWD],
        local_groups=LOCAL_GROUPS, local_origins=LOCAL_ORIGINS), LOCAL_PLACEMENT),
    'local threads without a canonical map': (case(FORWARD, LOCAL_THREADS, DECLARED, None,
        local_groups=LOCAL_GROUPS, local_origins=LOCAL_ORIGINS), LOCAL_PLACEMENT),
    'another host, cwd canonical root of the first host': (case((*FORWARD, OTHER_PARENT),
        [thread('o', '/srv/projects/studio', OTHER_HOST)], DECLARED, OTHER_CANONICAL, other_origins=OTHER_ORIGINS),
        {'o': 'other-parent'}),
}


@unittest.skipUnless(shutil.which('node'), 'Node.js required for renderer behavior')
class RemoteProjectGroupingBehaviorTests(unittest.TestCase):
    def place(self, grouping, cases):
        program = '\n'.join([STUBS, *HELPERS_917.values(), 'const ' + grouping + ';',
                             RUNNER.replace('CASES', json.dumps(cases))])
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / 'grouping.cjs'
            script.write_text(program, encoding='utf-8')
            result = subprocess.run(['node', str(script)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def both(self, cases):
        native = self.place(DXR_917, cases)
        guarded = self.place(patched(DXR_917).decode(), cases)
        return native, guarded

    def test_opening_a_worktree_thread_keeps_its_project(self):
        native, guarded = self.both([scenario for scenario, _, _ in SELECTED.values()])
        for (label, (_, before, after)), old, new in zip(SELECTED.items(), native, guarded):
            with self.subTest(label):
                # Origins are keyed by directory, so every thread in the cwd moves together.
                self.assertEqual(old, {'a': before, 'b': before})
                self.assertEqual(new, {'a': after, 'b': after})

    def test_other_paths_are_placed_as_before(self):
        native, guarded = self.both([scenario for scenario, _ in UNCHANGED.values()])
        for (label, (_, expected)), old, new in zip(UNCHANGED.items(), native, guarded):
            with self.subTest(label):
                self.assertEqual(old, expected)
                self.assertEqual(new, old)


class ManagedRendererGuardTests(unittest.TestCase):
    def test_managed_26_917_renderer_matches_the_verified_sources(self):
        archives = sorted((ROOT / 'artifacts/managed-desktop').glob('26.917.*/resources/app.asar'))
        if not archives:
            self.skipTest('No managed 26.917 desktop copy on this machine.')
        (before, after), = renderer_remote_root_patches(DXR_917.encode()).items()
        for archive in archives:
            with self.subTest(archive.parent.parent.name):
                renderers = read_entries(archive, lambda name: name.startswith('webview/assets/app-initial')
                                         and name.endswith('.js'))
                self.assertEqual(len(renderers), 1)
                data, = renderers.values()
                # Built before or after this patch: exactly one grouping function either way.
                self.assertEqual(data.count(before) + data.count(after), 1)
                self.assertEqual(data.count(DXR_917.encode()) + data.count(patched(DXR_917)), 1)
                for name, source in HELPERS_917.items():
                    self.assertEqual(data.count(source.encode()), 1, name)


if __name__ == '__main__':
    unittest.main()
