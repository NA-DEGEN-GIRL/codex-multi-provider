"""An SSH worktree project keeps its threads when one of them is opened.

The 26.917 sidebar grouping remaps a remote thread whose cwd is a linked git
worktree to a project that shares the git common dir. For projects declared
through a symlink, the remap cannot match git's physical toplevel. It then
falls back to the first such project in the profile's own sidebar order, so
opening a thread of the worktree project moved every thread of that project
under the parent repo project. The renderer patch keeps a declared remote root
(exact or canonical) for its own project and leaves every other path native.

26.930.4958 renamed the grouping helpers (the guard needs its own names) and
still asks git about a thread folder only for rows without a summary. Remote
threads in an undeclared linked worktree were therefore dropped until one of
them was opened. The collector patch asks for remote summary rows too; it is
applied only beside the guard.

The remap that places such threads falls back to the first project sharing the
git common dir in each profile's sidebar order. The order patch ranks, for
remote threads only, the declared worktree holding the cwd first and then the
repository project, so the result no longer depends on the sidebar order.
26.930 (3930 and 4958, the same code up to names; 7945 keeps the 4958 names)
refuses to publish without all three grouping patches.
"""
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from manager_core import desktop_bundle, original_sync_bundle
from manager_core.original_sync_bundle import (project_grouping_patched, renderer_remote_order_patches,
                                                renderer_remote_root_patches, renderer_summary_dir_patches)
sys.path.insert(0, str(ROOT / 'tests'))
# A managed 26.930 copy also requires the startup link guard of its main bundle.
from test_manager_desktop_bundle import LINKS_930


def managed_archives(pattern):
    """Managed desktop archives matching pattern, read-only.

    Looks in this checkout and, from a linked git worktree, in its main
    checkout (worktrees have no artifacts). Publication staging folders are
    skipped.
    """
    roots, marker = [ROOT], ROOT / '.git'
    if marker.is_file():
        text = marker.read_text(encoding='utf-8').strip()
        if text.startswith('gitdir:'):
            gitdir = Path(text[len('gitdir:'):].strip())
            gitdir = gitdir if gitdir.is_absolute() else (ROOT / gitdir).resolve()
            if gitdir.parent.name == 'worktrees':
                roots.append(gitdir.parent.parent.parent)
    found = {}
    for root in roots:
        for archive in sorted((root / 'artifacts/managed-desktop').glob(pattern + '/resources/app.asar')):
            if '.staging-' not in archive.parent.parent.name:
                found.setdefault(archive.parent.parent.name, archive)
    return list(found.values())

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

# Verbatim 26.917 folder collector: which thread folders the sidebar asks git about.
PXR_917 = (
    r'''function pXr(e,t,n,r,i,a,o){let s=new Set(r.map(e=>e.hostId)),c=new Map([[t,(n??[]).filter(e=>e!==`~`)]]),l=ne'''
    r'''w Map,u=new Set,d=new Set(Object.values(o?.localProjects??{}).map(e=>e.id)),f=(e,t)=>{let n=l.get(e)??new Set;'''
    r'''n.add(Ar(t).replace(/\/+$/,``)),l.set(e,n)},p=(e,t)=>{let n=c.get(e);c.set(e,n==null?[t]:[...n,t])};'''
    r'''for(let n of e)if(n.kind===`local`){if(n.pendingThreadStart!=null)continue;'''
    r'''if(n.pendingWorktree!=null){let e=n.pendingWorktree.hostId,r=n.pendingWorktree.sourceWorkspaceRoot;'''
    r'''r&&(e===t||s.has(e))&&(p(e,r),f(e,r));let i=As(n.pendingWorktree.startConversationParamsInput?.workspaceRoots)'''
    r'''??n.pendingWorktree.startConversationParamsInput?.cwd??r;i&&u.add(Ar(i));continue}'''
    r'''let e=n.hostId==null||zs(n.hostId)?t:n.hostId,c=o?.threadProjectAssignments?.[n.conversationId];'''
    r'''if(!(o?.projectlessThreadIds?.has(n.conversationId)||c?.projectKind===`local`&&(c.projectOrigin===`chatgpt`||d'''
    r'''.has(c.projectId))||c?.projectKind===`remote`&&c.hostId===e&&r.some(t=>t.id===c.projectId&&t.hostId===e))&&n.c'''
    r'''wd&&f(e,n.cwd),n.summary!=null&&!R(n.cwd,i,a?.[e])||n.workspaceKind===`projectless`||n.cwd===`~`)continue;'''
    r'''let l=n.cwd;if(!l||e!==t&&!s.has(e))continue;p(e,l);continue}if(o!=null){for(let e of n??[])f(t,e);'''
    r'''for(let[e,t]of c)c.set(e,t.filter(t=>l.get(e)?.has(Ar(t).replace(/\/+$/,``))||u.has(Ar(t))))}'''
    r'''for(let e of r)p(e.hostId,e.remotePath);return Array.from(c.entries()).map(([e,t])=>({hostId:e,dirs:(0,TXr.def'''
    r'''ault)(t).sort((e,t)=>e.localeCompare(t))})).filter(({hostId:e,dirs:n})=>e===t||n.length>0)}'''
)
SKIP_917 = b'n.summary!=null&&!R(n.cwd,i,a?.[e])||'
REMOTE_SKIP_917 = b'n.summary!=null&&e===t&&!R(n.cwd,i,a?.[e])||'

CONDITION = b'if(R(d,a,s?.worktreesRootsByHostId?.[c])||p&&_Xr(d,_)){let r=vXr('
GUARDED = (b'if(!(p&&(CYr(m,c,d)??dXr((m??[]).filter(e=>e.hostId===c),d,s?.canonicalProjectPathsByHostId)))'
           b'&&(R(d,a,s?.worktreesRootsByHostId?.[c])||p&&_Xr(d,_))){let r=vXr(')


def order_call(remap, lookup, key, linked):
    """(native, ordered) remap call: for remote threads (p) the project list is
    ranked: same git root as the cwd (2), a known non-worktree repository (1),
    the rest (0); a stable sort keeps the sidebar order within a rank."""
    native = b'{let r=%s(d,e.conversationId,t,n,_,' % remap
    order = (b'p?((w=%s(d,_))=>t.map(g=>[g.projectKind===`remote`&&g.hostId===c&&g.path!=null?'
             b'(k=>k==null?0:k.root!=null&&w?.root!=null&&%s(k.root)===%s(w.root)?2:%s(g.path,_)?0:1)'
             b'(%s(g.path,_)):0,g]).sort((x,y)=>y[0]-x[0]).map(x=>x[1]))():t' % (lookup, key, key, linked, lookup))
    return native, b'{let r=%s(d,e.conversationId,%s,n,_,' % (remap, order)


CALL_917, ORDERED_CALL_917 = order_call(b'vXr', b'qF', b'Ar', b'_Xr')


def patched(source, order=False):
    """The 26.917 grouping with the guard (revision 96), and the order when asked."""
    data = source.encode() if isinstance(source, str) else source
    steps = (renderer_remote_root_patches, renderer_remote_order_patches) if order else (renderer_remote_root_patches,)
    for patches_of in steps:
        for before, after in patches_of(data).items():
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

    def test_26_917_collector_follows_its_guard(self):
        source = (PXR_917 + ';' + DXR_917).encode()
        (before, after), = renderer_summary_dir_patches(source).items()
        self.assertEqual(source.replace(before, after), source.replace(SKIP_917, REMOTE_SKIP_917))
        self.assertEqual(len(renderer_summary_dir_patches(PXR_917.encode() + b';' + patched(DXR_917))), 1)
        self.assertEqual(len(renderer_summary_dir_patches(PXR_917.encode() + b';' + patched(DXR_917, True))), 1)
        self.assertEqual(renderer_summary_dir_patches(PXR_917.encode()), {})

    def test_26_917_order_changes_only_the_remap_call(self):
        source = DXR_917.encode()
        for label, data in (('native', source), ('guarded', patched(source))):
            with self.subTest(label):
                (before, after), = renderer_remote_order_patches(b'let x=1;' + data + b';let y=2').items()
                self.assertEqual(data.count(before), 1)
                self.assertEqual(data.replace(before, after), data.replace(CALL_917, ORDERED_CALL_917))
        once = patched(source, True)
        self.assertEqual(once.count(GUARDED), 1)
        self.assertEqual(once.count(ORDERED_CALL_917), 1)
        self.assertEqual(renderer_remote_order_patches(once), {})
        self.assertEqual(renderer_remote_root_patches(once), {})
        self.assertEqual(patched(once, True), once)
        # The call alone, without its grouping section, is not this function's.
        call = CALL_917 + b's?.threadWorkspaceRootHints,e.summary!=null);r&&(f=r)}'
        self.assertEqual(renderer_remote_order_patches(call), {})
        # A second copy of the call beside the section is refused; a second
        # section is no verified section (the guard refuses that archive).
        with self.assertRaises(ValueError):
            renderer_remote_order_patches(source + b';' + call)
        self.assertEqual(renderer_remote_order_patches(source + b';' + source), {})
        with self.assertRaises(ValueError):
            renderer_remote_root_patches(source + b';' + source)


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


RENDERER_CHUNK = 'webview/assets/app-initial-fixture.js'


def copies(grouping, version=None):
    """Minimal managed and original-sync archives whose renderer ends with grouping."""
    sync = b';'.join(original_sync_bundle.PATCHES)
    renderer = b';'.join(original_sync_bundle.RENDERER_PATCHES) + b';' + grouping
    package = [] if version is None else [('package.json', json.dumps({'version': version}).encode())]
    return {
        'managed': (desktop_bundle.patch_archive, [
            ('.vite/build/main.js', b'function s9(){' + desktop_bundle._ORIGINAL + b'return "posix";}' + LINKS_930),
            ('.vite/build/notifications.js', desktop_bundle._NOTIFICATION_CLICK + b'originalCallback();})' +
             desktop_bundle._NOTIFICATION_SHOW + desktop_bundle._WINDOW_MESSAGE),
            ('.vite/build/browser-runtime.js', b'Qr({' + desktop_bundle._BROWSER_RUNTIME + b');'),
            ('.vite/build/context.js', sync),
            (RENDERER_CHUNK, desktop_bundle._CONTEXT_RENDERER + b';' + renderer), *package]),
        'original': (original_sync_bundle.patch_archive, [
            ('.vite/build/main.js', sync), (RENDERER_CHUNK, renderer), *package]),
    }


COPIES = ('managed', 'original')


def patch_copy(label, grouping, version=None):
    """The patched renderer chunk of one copy; raises what its patch_archive raises."""
    patch_archive, chunks = copies(grouping, version)[label]
    with tempfile.TemporaryDirectory() as temporary:
        source, target = Path(temporary) / 'source.asar', Path(temporary) / 'patched.asar'
        write_archive(source, chunks)
        patch_archive(source, target)
        return read_entries(target, lambda name: name == RENDERER_CHUNK)[RENDERER_CHUNK]


class RemoteRootArchiveTests(unittest.TestCase):
    def test_managed_and_original_copies_install_the_guard(self):
        for label in COPIES:
            with self.subTest(label):
                data = patch_copy(label, DXR_917.encode() + b';' + PXR_917.encode())
                self.assertEqual(data.count(GUARDED), 1)
                self.assertNotIn(CONDITION, data)
                self.assertEqual(data.count(ORDERED_CALL_917), 1)
                self.assertNotIn(CALL_917, data)
                self.assertEqual(data.count(REMOTE_SKIP_917), 1)
                self.assertNotIn(SKIP_917, data)


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

# A linked worktree of parent that is no declared project, and a subfolder of
# the declared studio worktree; git reports their physical toplevels.
FEATURE = '/srv/projects/feature'
FEATURE_CWD = origin(FEATURE, FEATURE)
STUDIO_SRC = '/srv/projects/studio/src'
STUDIO_SRC_CWD = origin(STUDIO_SRC, '/srv/projects/studio')
PHYSICAL_FORWARD, PHYSICAL_REVERSE = (PARENT_P, STUDIO_P), (STUDIO_P, PARENT_P)


def remap_case(order, cwd, origins, canonical='symlinked'):
    return case(order, [thread('f', cwd)], origins, canonical)


# Opened remote threads the remap places: guard only (sidebar order decides),
# guard and order (worktree holding the cwd, then the repository project).
ORDERED = {
    'undeclared worktree, symlinked, parent first': (remap_case(FORWARD, FEATURE, DECLARED + [FEATURE_CWD]),
                                                     'parent', 'parent'),
    'undeclared worktree, symlinked, studio first': (remap_case(REVERSE, FEATURE, DECLARED + [FEATURE_CWD]),
                                                     'studio', 'parent'),
    'undeclared worktree, physical, parent first': (remap_case(PHYSICAL_FORWARD, FEATURE, PHYSICAL + [FEATURE_CWD],
                                                               PHYSICAL_CANONICAL), 'parent', 'parent'),
    'undeclared worktree, physical, studio first': (remap_case(PHYSICAL_REVERSE, FEATURE, PHYSICAL + [FEATURE_CWD],
                                                               PHYSICAL_CANONICAL), 'studio', 'parent'),
    'worktree subfolder, symlinked, parent first': (remap_case(FORWARD, STUDIO_SRC, DECLARED + [STUDIO_SRC_CWD]),
                                                    'parent', 'studio'),
    'worktree subfolder, symlinked, studio first': (remap_case(REVERSE, STUDIO_SRC, DECLARED + [STUDIO_SRC_CWD]),
                                                    'studio', 'studio'),
    'worktree subfolder, physical, parent first': (remap_case(PHYSICAL_FORWARD, STUDIO_SRC, PHYSICAL + [STUDIO_SRC_CWD],
                                                              PHYSICAL_CANONICAL), 'studio', 'studio'),
    'worktree root without a canonical map, parent first': (remap_case(FORWARD, '/srv/projects/studio',
                                                                       DECLARED + [STUDIO_CWD], None), 'parent', 'studio'),
    'worktree root without a canonical map, studio first': (remap_case(REVERSE, '/srv/projects/studio',
                                                                       DECLARED + [STUDIO_CWD], None), 'studio', 'studio'),
}
# Paths of UNCHANGED that the order places differently: inside the studio worktree.
ORDER_CHANGES = {'remote subdirectory with its origin': {'s': 'studio'},
                 'no canonical map, after the cwd query': {'a': 'studio', 'b': 'studio'}}


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

    def test_order_makes_the_remap_independent_of_the_sidebar_order(self):
        cases = [scenario for scenario, _, _ in ORDERED.values()]
        guarded, ordered = self.place(patched(DXR_917).decode(), cases), self.place(patched(DXR_917, True).decode(), cases)
        for (label, (_, before, after)), old, new in zip(ORDERED.items(), guarded, ordered):
            with self.subTest(label):
                self.assertEqual(old, {'f': before})
                self.assertEqual(new, {'f': after})

    def test_order_keeps_declared_roots_and_local_threads(self):
        cases = [*(scenario for scenario, _, _ in SELECTED.values()), *(scenario for scenario, _ in UNCHANGED.values())]
        ordered = self.place(patched(DXR_917, True).decode(), cases)
        expected = [{'a': after, 'b': after} for _, _, after in SELECTED.values()]
        expected += [ORDER_CHANGES.get(label, placement) for label, (_, placement) in UNCHANGED.items()]
        for label, new, want in zip([*SELECTED, *UNCHANGED], ordered, expected):
            with self.subTest(label):
                self.assertEqual(new, want)


class ManagedRendererGuardTests(unittest.TestCase):
    def test_managed_26_917_renderer_matches_the_verified_sources(self):
        archives = managed_archives('26.917.*')
        if not archives:
            self.skipTest('No managed 26.917 desktop copy on this machine.')
        forms = original_sync_bundle._remote_root_forms(original_sync_bundle._REMOTE_ROOT_VARIANTS[0])
        for archive in archives:
            with self.subTest(archive.parent.parent.name):
                renderers = read_entries(archive, lambda name: name.startswith('webview/assets/app-initial')
                                         and name.endswith('.js'))
                self.assertEqual(len(renderers), 1)
                data, = renderers.values()
                # Built before or after these patches: exactly one grouping function either way.
                self.assertEqual(sum(data.count(form) for form in forms), 1)
                self.assertEqual(sum(data.count(form) for form in grouping_forms(
                    DXR_917, CONDITION, GUARDED, CALL_917, ORDERED_CALL_917)), 1)
                collector = PXR_917.encode()
                self.assertEqual(data.count(collector) + data.count(collector.replace(SKIP_917, REMOTE_SKIP_917)), 1)
                for name, source in HELPERS_917.items():
                    self.assertEqual(data.count(source.encode()), 1, name)


# 26.930.4958 (app-initial), verbatim: the folder collector (gIn), the grouping
# function (AIn) and every helper they reach in that chunk. ManagedRenderer4958Tests
# checks them against a 4958 renderer when one is available on this machine.
GIN_4958 = (
    r'''function gIn(e,t,n,r,i,a,o){let s=new Set(r.map(e=>e.hostId)),c=new Map([[t,(n??[]).filter(e=>e!==`~`)]]),l=ne'''
    r'''w Map,u=new Set,d=new Set(Object.values(o?.localProjects??{}).map(e=>e.id)),f=(e,t)=>{let n=l.get(e)??new Set;'''
    r'''n.add(yh(t).replace(/\/+$/,``)),l.set(e,n)},p=(e,t)=>{let n=c.get(e);c.set(e,n==null?[t]:[...n,t])};'''
    r'''for(let n of e)if(n.kind===`local`){if(n.pendingThreadStart!=null)continue;'''
    r'''if(n.pendingWorktree!=null){let e=n.pendingWorktree.hostId,r=n.pendingWorktree.sourceWorkspaceRoot;'''
    r'''r&&(e===t||s.has(e))&&(p(e,r),f(e,r));let i=To(n.pendingWorktree.startConversationParamsInput?.workspaceRoots)'''
    r'''??n.pendingWorktree.startConversationParamsInput?.cwd??r;i&&u.add(yh(i));continue}'''
    r'''let e=n.hostId==null||Fi(n.hostId)?t:n.hostId,c=o?.threadProjectAssignments?.[n.conversationId];'''
    r'''if(!(o?.projectlessThreadIds?.has(n.conversationId)||c?.projectKind===`local`&&(c.projectOrigin===`chatgpt`||d'''
    r'''.has(c.projectId))||c?.projectKind===`remote`&&c.hostId===e&&r.some(t=>t.id===c.projectId&&t.hostId===e))&&n.c'''
    r'''wd&&f(e,n.cwd),n.summary!=null&&!Yu(n.cwd,i,a?.[e])||n.workspaceKind===`projectless`||n.cwd===`~`)continue;'''
    r'''let l=n.cwd;if(!l||e!==t&&!s.has(e))continue;p(e,l);continue}if(o!=null){for(let e of n??[])f(t,e);'''
    r'''for(let[e,t]of c)c.set(e,t.filter(t=>l.get(e)?.has(yh(t).replace(/\/+$/,``))||u.has(yh(t))))}'''
    r'''for(let e of r)p(e.hostId,e.remotePath);return Array.from(c.entries()).map(([e,t])=>({hostId:e,dirs:(0,OIn.def'''
    r'''ault)(t).sort((e,t)=>e.localeCompare(t))})).filter(({hostId:e,dirs:n})=>e===t||n.length>0)}'''
)

AIN_4958 = (
    r'''AIn=(e,t,n,r,i,a,o=$r,s)=>{let c=e.hostId==null||Fi(e.hostId)?o:e.hostId,l=s?.threadProjectAssignments?.[e.con'''
    r'''versationId];if(c!==o&&s?.enabledRemoteHostIds!=null&&!s.enabledRemoteHostIds.has(c))return;'''
    r'''let u=l!=null&&(l.projectKind===`local`||l.hostId!=null&&c===l.hostId)?hIn(l,t):null;'''
    r'''if(u!=null){u.threadKeys.push(e.key);return}if(l?.projectKind===`local`&&l.projectOrigin===`chatgpt`)return;'''
    r'''let d=e.cwd;if(!d||!mFn(d).length)return;let f=d;if(e.workspaceKind===`projectless`||s?.projectlessThreadIds?.'''
    r'''has(e.conversationId)===!0)return;let p=c!==o,m=s?.remoteProjects,h=s?.remoteConnections?.find(e=>e.hostId===c'''
    r'''),g=(m??[]).filter(e=>{if(e.hostId===c)return!1;let t=s?.remoteConnections?.find(t=>t.hostId===e.hostId);'''
    r'''return yFn(h,t)});if(p&&g.length===0&&!m?.some(e=>e.hostId===c))return;'''
    r'''let _=yIn({gitOrigins:r,gitOriginsByHostId:i,hostId:c??void 0,primaryHostId:o}'''
    r'''),v=[...p?Object.entries((0,DIn.default)(g,e=>e.hostId)).flatMap(([e,t])=>vIn(t,e,d,s?.codexHomesByHostId?.[e]'''
    r''',s?.worktreesRootsByHostId?.[e])):[]];if(v.length===1){(t.find(e=>e.projectId===v[0]?.id)??null)?.threadKeys.p'''
    r'''ush(e.key);return}if(v.length>1)return;if(Yu(d,a,s?.worktreesRootsByHostId?.[c])||p&&bIn(d,_)){let r=xIn(d,e.c'''
    r'''onversationId,t,n,_,s?.threadWorkspaceRootHints,e.summary!=null);r&&(f=r)}'''
    r'''let y=(m??[]).filter(e=>e.hostId===c),b=_Fn(m,c,f)??mIn(y,f,s?.canonicalProjectPathsByHostId)??mIn(g,f,s?.cano'''
    r'''nicalProjectPathsByHostId);if(b!=null){let n=t.find(e=>e.projectId===b.id)??null;'''
    r'''if(n!=null){n.threadKeys.push(e.key);return}}if(p)return;let x=fIn(n,f);'''
    r'''x&&(x.threadKeys.push(e.key),f!==d&&s?.onDiscoverThreadWorkspaceRootHint?.(e.conversationId,x.path))}'''
)

HELPERS_4958 = {
    'rIn': (
        r'''function rIn(e,t,n,r,i,a){let o=n.map(e=>({...e,threadKeys:[]}'''
        r''')),s=(0,EIn.default)((r??[]).flatMap(({dir:e,originUrl:t})=>{let n=t?oFn(t):null;return n?[[yh(e),n]]:[]}'''
        r''')),c=(0,DIn.default)(t??[],e=>e.label),l=nIn(o);return e.forEach(e=>{if(e.kind===`local`)e.pendingWorktree==nu'''
        r'''ll?e.pendingThreadStart==null?AIn(e,o,l,r,a?.gitOriginsByHostId,i,a?.primaryHostId,a):hIn(e.pendingThreadStart'''
        r'''.task.projectAssignment??void 0,o)?.threadKeys.push(e.key):SIn(e,s,o,l);'''
        r'''else if(e.kind===`remote`){let t=hIn(a?.threadProjectAssignments?.[e.task.id],o);'''
        r'''if(t!=null){t.threadKeys.push(e.key);return}if(a?.projectlessThreadIds?.has(e.task.id)===!0)return;'''
        r'''CIn(e,c,o,l)}}),o}'''
    ),
    'nIn': (
        r'''function nIn(e){let t=e.filter(e=>e.projectKind===`local`),n=new Set(t.flatMap(e=>Mj(e).map(yh))),r=new Map;'''
        r'''for(let e of t){let t=[...Mj(e).map(e=>({alias:e,path:e})),...oIn(e).filter(({alias:e})=>!n.has(yh(e)))];'''
        r'''for(let{alias:n,path:i}of t){let t=yh(n),a=e.path===i&&aIn(e)?e:{...e,path:i},o=r.get(t);'''
        r'''(o==null||sIn(a,o))&&r.set(t,a)}}return r}'''
    ),
    'oIn': (
        r'''function oIn(e){return[...e.pathAlias==null||e.path==null?[]:[{alias:e.pathAlias,path:e.path}'''
        r'''],...e.rootPathAliases??[]]}'''
    ),
    'sIn': (
        r'''function sIn(e,t){return Ne({createdAt:e.projectCreatedAt??0,projectId:e.projectId,rootPath:e.path,rootPaths:M'''
        r'''j(e)},{createdAt:t.projectCreatedAt??0,projectId:t.projectId,rootPath:t.path,rootPaths:Mj(t)})}'''
    ),
    'aIn': (
        r'''function aIn(e){return e.path!=null}'''
    ),
    'Mj': (
        r'''function Mj(e){return e.rootPaths??(e.path==null?[]:[e.path])}'''
    ),
    'hIn': (
        r'''function hIn(e,t){return e==null?null:t.find(t=>t.projectId!==e.projectId||t.projectKind!==e.projectKind?!1:e.'''
        r'''projectKind===`local`||t.hostId===e.hostId)??null}'''
    ),
    'yIn': (
        r'''function yIn({gitOrigins:e,gitOriginsByHostId:t,hostId:n,primaryHostId:r}'''
        r'''){return n&&t?.[n]?t[n]:n&&t&&n!==r?[]:e??[]}'''
    ),
    'vIn': (
        r'''function vIn(e,t,n,r,i){let a=op(n),o=i??(r==null?null:ORe(r)),s=o==null?null:op(o),c=a.lastIndexOf(`/.codex/w'''
        r'''orktrees/`),l=s!=null&&a.startsWith(`${s}/`)?s.length+1:c===-1?null:c+18;if(l==null)return[];'''
        r'''let u=a.slice(l).split(`/`).filter(Boolean);if(u.length<2||!/^[0-9a-f]{4,}'''
        r'''$/i.test(u[0]??``)&&!Wge.safeParse(u[0]).success)return[];let d=u.slice(1),f=(e??[]).filter(e=>e.hostId===t);'''
        r'''for(let e=d.length;e>0;--e){let t=d.slice(0,e).join(`/`),n=f.filter(e=>{let n=op(e.remotePath);'''
        r'''return n===t||n.endsWith(`/${t}`)});if(n.length>0)return n}return[]}'''
    ),
    'bIn': (
        r'''function bIn(e,t){let n=lFn(e,t??[]);return n?.commonDir?yh(n.commonDir).replace(/\/+$/,``)!==`${yh(n.root).re'''
        r'''place(/\/+$/,``)}/.git`:!1}'''
    ),
    'xIn': (
        r'''function xIn(e,t,n,r,i,a,o=!1){if(pIn(r,e))return null;let s=a?.[t],c=s?fIn(r,s):null;'''
        r'''if(!i)return c?.path??null;let l=lFn(e,i);if(!l)return c?.path??null;'''
        r'''let u=l.originUrl,d=e=>e?u?e.originUrl===u:e.commonDir===l.commonDir:!1,f=yh(e),p=pFn(e,i),m=n.flatMap(e=>{if('''
        r'''!aIn(e))return[];let t=yh(e.path);if(e.isCodexWorktree&&f!==t)return[];'''
        r'''let n=(e.projectKind===`local`?Mj(e):[e.path]).flatMap(e=>{let t=lFn(e,i);'''
        r'''return t==null||!d(t)?[]:[{repoPath:pFn(e,i),origin:t}]});return n.length===0?[]:[{group:e,matchingRepos:n}]}'''
        r'''),h=m.filter(({matchingRepos:e})=>e.some(({repoPath:e})=>e===p)),g=uFn(l.root,h.map(({matchingRepos:e}'''
        r''')=>e.filter(({repoPath:e})=>e===p).map(({origin:e})=>e.root))),_=g==null?null:h[g];if(_)return _.group.path;'''
        r'''if(c&&m.some(({group:e})=>e===c))return c.path;let v=m.filter(({matchingRepos:e})=>e.some(({repoPath:e}'''
        r''')=>e===``)),y=uFn(l.root,v.map(({matchingRepos:e})=>e.filter(({repoPath:e})=>e===``).map(({origin:e}'''
        r''')=>e.root))),b=y==null?null:v[y];if(b)return b.group.path;let x=m[0];'''
        r'''return x?x.group.path:o?c?.path??null:null}'''
    ),
    'pIn': (
        r'''function pIn(e,t){return e.has(yh(t))}'''
    ),
    'fIn': (
        r'''function fIn(e,t){return e.get(yh(t))??null}'''
    ),
    'pFn': (
        r'''function pFn(e,t){let n=lFn(e,t);if(n?.root==null)return``;let r=mFn(yh(e)),i=mFn(yh(n.root));'''
        r'''return r.slice(i.length).join(`/`)}'''
    ),
    'mFn': (
        r'''function mFn(e){return e.split(/[/\\]+/).filter(Boolean)}'''
    ),
    'lFn': (
        r'''function lFn(e,t){let n=yh(e).replace(/\/+$/,``);return t.find(e=>yh(e.dir).replace(/\/+$/,``)===n)??null}'''
    ),
    'uFn': (
        r'''function uFn(e,t){if(t.length===0)return null;let n=ig(e),r=t.findIndex(e=>e.some(e=>ig(e)===n));'''
        r'''return r===-1?0:r}'''
    ),
    '_Fn': (
        r'''function _Fn(e,t,n){if(t==null||e==null)return null;let r=op(n);'''
        r'''return e.find(e=>e.hostId===t&&op(e.remotePath)===r)??null}'''
    ),
    'mIn': (
        r'''function mIn(e,t,n){let r=op(t);return e.find(e=>op(n?.[e.hostId]?.[e.remotePath]??e.remotePath)===r)??null}'''
    ),
    'yFn': (
        r'''function yFn(e,t){if(e==null||t==null)return!1;let[n,r]=IUe(e)&&DSe(t)?[e,t]:IUe(t)&&DSe(e)?[t,e]:[];'''
        r'''if(n==null||r==null)return!1;let i=n.hostName.trim().toLowerCase().replace(/\.$/,``),a=r.sshHost.trim().toLowe'''
        r'''rCase().replace(/\.$/,``);return i.length>0&&a.length>0&&(i===a||i.startsWith(`${a}.`)||a.startsWith(`${i}'''
        r'''.`))}'''
    ),
    '_In': (
        r'''function _In(e,t){let n=new Map(t.map(({hostId:e,dirs:t})=>[e,new Set(t.map(yh))]));'''
        r'''return e.map(({hostId:e,dirs:t})=>({hostId:e,dirs:t.filter(t=>!n.get(e)?.has(yh(t)))})).filter(({dirs:e}'''
        r''')=>e.length>0)}'''
    ),
}

CONDITION_4958 = b'if(Yu(d,a,s?.worktreesRootsByHostId?.[c])||p&&bIn(d,_)){let r=xIn('
GUARDED_4958 = (b'if(!(p&&(_Fn(m,c,d)??mIn((m??[]).filter(e=>e.hostId===c),d,s?.canonicalProjectPathsByHostId)))'
                b'&&(Yu(d,a,s?.worktreesRootsByHostId?.[c])||p&&bIn(d,_))){let r=xIn(')
SKIP_4958 = b'n.summary!=null&&!Yu(n.cwd,i,a?.[e])||'
REMOTE_SKIP_4958 = b'n.summary!=null&&e===t&&!Yu(n.cwd,i,a?.[e])||'
CALL_4958, ORDERED_CALL_4958 = order_call(b'xIn', b'lFn', b'yh', b'bIn')
VERSION_4958 = '26.930.41038'  # package.json version of the 26.930.4958 desktop
# 26.930.7945 ships gIn, AIn and HELPERS_4958 byte for byte (ManagedRenderer7945Tests).
VERSION_7945 = '26.930.61225'  # package.json version of the 26.930.7945 desktop
SOURCE_4958 = (GIN_4958 + ';' + AIN_4958).encode()


def apply_grouping(data, order=True):
    """data with every grouping patch the bundle selects for it, in bundle order
    (order=False: the guard and the collector only, as revision 119 part 3 left it)."""
    for patches_of in (renderer_remote_root_patches, renderer_remote_order_patches, renderer_summary_dir_patches):
        if order or patches_of is not renderer_remote_order_patches:
            for before, after in patches_of(data).items():
                data = data.replace(before, after)
    return data


def patched_4958(order=True):
    """(collector, grouping) sources as the 26.930.4958 bundle patch leaves them."""
    collector, grouping = apply_grouping(SOURCE_4958, order).split(b';AIn=(', 1)
    return collector.decode(), 'AIn=(' + grouping.decode()


class Grouping4958SelectionTests(unittest.TestCase):
    def test_4958_grouping_gets_one_guard(self):
        source = AIN_4958.encode()
        patches = renderer_remote_root_patches(b'let x=1;' + source + b';let y=2')
        self.assertEqual(len(patches), 1)
        (before, after), = patches.items()
        self.assertEqual(source.count(before), 1)
        self.assertEqual(source.replace(before, after), source.replace(CONDITION_4958, GUARDED_4958))

    def test_collector_asks_for_remote_summary_rows(self):
        patches = renderer_summary_dir_patches(b'let x=1;' + SOURCE_4958 + b';let y=2')
        self.assertEqual(len(patches), 1)
        (before, after), = patches.items()
        self.assertEqual(SOURCE_4958.count(before), 1)
        # Only the summary skip changes; e is the row's host and t the primary
        # (local) host, so local rows keep the native test.
        self.assertEqual(SOURCE_4958.replace(before, after), SOURCE_4958.replace(SKIP_4958, REMOTE_SKIP_4958))

    def test_order_changes_only_the_remap_call(self):
        guarded = SOURCE_4958.replace(CONDITION_4958, GUARDED_4958)
        for label, data in (('native', SOURCE_4958), ('guarded', guarded)):
            with self.subTest(label):
                (before, after), = renderer_remote_order_patches(b'let x=1;' + data + b';let y=2').items()
                self.assertEqual(data.count(before), 1)
                # Remote threads (p) get the ranked list; local threads keep t.
                self.assertEqual(data.replace(before, after), data.replace(CALL_4958, ORDERED_CALL_4958))
        self.assertEqual(renderer_remote_order_patches(GIN_4958.encode()), {})
        # Without exactly one verified name row for this remap the call stays native.
        with mock.patch.object(original_sync_bundle, '_REMOTE_ORDER_VARIANTS',
                               ((b'xIn', b'bIn', b'qF', b'Ar'), (b'xIn', b'bIn', b'lFn', b'yh'))):
            self.assertEqual(renderer_remote_order_patches(SOURCE_4958), {})
        with mock.patch.object(original_sync_bundle, '_REMOTE_ORDER_VARIANTS', ((b'vXr', b'_Xr', b'lFn', b'yh'),)):
            self.assertEqual(renderer_remote_order_patches(SOURCE_4958), {})

    def test_patched_renderer_is_not_patched_again(self):
        once = apply_grouping(SOURCE_4958)
        self.assertEqual(once.count(REMOTE_SKIP_4958), 1)
        self.assertEqual(once.count(GUARDED_4958), 1)
        self.assertEqual(once.count(ORDERED_CALL_4958), 1)
        self.assertTrue(project_grouping_patched(once))
        self.assertEqual(renderer_summary_dir_patches(once), {})
        self.assertEqual(renderer_remote_root_patches(once), {})
        self.assertEqual(renderer_remote_order_patches(once), {})
        self.assertEqual(apply_grouping(once), once)
        # No patch alone or pair is the verified grouping.
        guarded = SOURCE_4958.replace(CONDITION_4958, GUARDED_4958)
        unordered = apply_grouping(SOURCE_4958, order=False)
        for label, data in (('native', SOURCE_4958), ('guard', guarded),
                            ('collector', SOURCE_4958.replace(SKIP_4958, REMOTE_SKIP_4958)),
                            ('order', SOURCE_4958.replace(CALL_4958, ORDERED_CALL_4958)),
                            ('guard and order', guarded.replace(CALL_4958, ORDERED_CALL_4958)),
                            ('guard and collector', unordered)):
            with self.subTest(label):
                self.assertFalse(project_grouping_patched(data))
        # A guard (and collector) installed earlier still admits the missing patches.
        self.assertEqual(len(renderer_summary_dir_patches(guarded)), 1)
        self.assertEqual(len(renderer_remote_order_patches(unordered)), 1)
        self.assertEqual(apply_grouping(unordered), once)
        # An order installed without the guard is no verified section.
        self.assertEqual(renderer_summary_dir_patches(SOURCE_4958.replace(CALL_4958, ORDERED_CALL_4958)), {})

    def test_ambiguous_collector_is_refused(self):
        for source in (GIN_4958 + ';' + GIN_4958 + ';' + AIN_4958, GIN_4958 + ';' + GIN_4958):
            with self.subTest(len(source)), self.assertRaises(ValueError):
                renderer_summary_dir_patches(source.encode())

    def test_collector_without_its_guard_stays_native(self):
        # Without the guard, asking for every remote summary row would move the
        # threads of a declared worktree root (revision 96; see behavior tests).
        self.assertEqual(renderer_summary_dir_patches(GIN_4958.encode()), {})
        # A guard of another build (other worktree-root test) is not this one.
        self.assertEqual(renderer_summary_dir_patches((GIN_4958 + ';' + DXR_917).encode()), {})
        self.assertEqual(renderer_summary_dir_patches((PXR_917 + ';' + AIN_4958).encode()), {})

    def test_missing_or_changed_collector_stays_native(self):
        self.assertEqual(renderer_summary_dir_patches(b''), {})
        for old, new in ((b'||Fi(n.hostId)?t:', b'||Fx(n.hostId)?t:'), (b'n.cwd===`~`)continue', b'n.cwd===`~`)break'),
                         (b'&&n.cwd&&f(e,n.cwd),', b'&&f(e,n.cwd),'), (SKIP_4958, b'n.summary!=null&&!Yu(n.cwd,i)||')):
            with self.subTest(old):
                mutated = SOURCE_4958.replace(old, new)
                self.assertNotEqual(mutated, SOURCE_4958)
                self.assertEqual(renderer_summary_dir_patches(mutated), {})


class Grouping4958ArchiveTests(unittest.TestCase):
    def test_26_930_copies_install_all_three_patches(self):
        for label in COPIES:
            with self.subTest(label):
                data = patch_copy(label, SOURCE_4958, VERSION_4958)
                self.assertEqual(data.count(REMOTE_SKIP_4958), 1)
                self.assertEqual(data.count(GUARDED_4958), 1)
                self.assertEqual(data.count(ORDERED_CALL_4958), 1)
                self.assertNotIn(SKIP_4958, data)
                self.assertNotIn(CONDITION_4958, data)
                self.assertNotIn(CALL_4958, data)

    def test_26_930_without_all_grouping_patches_is_refused(self):
        incomplete = {'neither': b'', 'grouping only': AIN_4958.encode(), 'collector only': GIN_4958.encode(),
                      'grouping of another build': (GIN_4958 + ';' + DXR_917).encode()}
        for name, source in incomplete.items():
            for label in COPIES:
                with self.subTest(name, copy=label), self.assertRaisesRegex(ValueError, 'SSH worktree'):
                    patch_copy(label, source, VERSION_4958)
        # Guard and collector without verified order names: refused as well.
        with mock.patch.object(original_sync_bundle, '_REMOTE_ORDER_VARIANTS', ()):
            for label in COPIES:
                with self.subTest('no order names', copy=label), self.assertRaisesRegex(ValueError, 'SSH worktree'):
                    patch_copy(label, SOURCE_4958, VERSION_4958)

    def test_other_versions_keep_the_native_fallback(self):
        for version in (None, '26.917.71314'):
            for label in COPIES:
                with self.subTest(version, copy=label):
                    # Like the other optional renderer patches: unverified means native, not an error.
                    self.assertIn(SKIP_4958, patch_copy(label, GIN_4958.encode(), version))
                    self.assertEqual(patch_copy(label, AIN_4958.encode(), version).count(GUARDED_4958), 1)
                    self.assertIn(original_sync_bundle.RENDERER_PATCHES[
                        b'this.requestClient=n;let y=this.settings.restricted;'], patch_copy(label, b'', version))

    def test_previously_patched_26_930_archive_is_accepted(self):
        once = apply_grouping(SOURCE_4958)
        for name, source in (('all patches', once), ('without the order', apply_grouping(SOURCE_4958, order=False))):
            for label in COPIES:
                with self.subTest(name, copy=label):
                    data = patch_copy(label, source, VERSION_4958)
                    self.assertEqual(data.count(REMOTE_SKIP_4958), 1)
                    self.assertEqual(data.count(GUARDED_4958), 1)
                    self.assertEqual(data.count(ORDERED_CALL_4958), 1)

    def test_26_930_7945_copies_are_patched_or_refused_like_4958(self):
        # Same grouping code, new package version: the 26.930 requirement applies.
        for label in COPIES:
            with self.subTest(label):
                data = patch_copy(label, SOURCE_4958, VERSION_7945)
                self.assertEqual(data, patch_copy(label, SOURCE_4958, VERSION_4958))
                self.assertEqual(data.count(REMOTE_SKIP_4958), 1)
                self.assertEqual(data.count(GUARDED_4958), 1)
                self.assertEqual(data.count(ORDERED_CALL_4958), 1)
                with self.assertRaisesRegex(ValueError, 'SSH worktree'):
                    patch_copy(label, AIN_4958.encode(), VERSION_7945)


# 26.930.3930 (app-initial), verbatim apart from the guard (the managed copy
# carries it; removed here): the folder collector (kIn) and the grouping
# function (UIn). The same code as 4958 up to names; ManagedRenderer3930Tests
# checks them against a managed 3930 copy when one exists.
KIN_3930 = (
    r'''function kIn(e,t,n,r,i,a,o){let s=new Set(r.map(e=>e.hostId)),c=new Map([[t,(n??[]).filter(e=>e!==`~`)]]),l=new '''
    r'''Map,u=new Set,d=new Set(Object.values(o?.localProjects??{}).map(e=>e.id)),f=(e,t)=>{let n=l.get(e)??new Set;n.ad'''
    r'''d(Kf(t).replace(/\/+$/,``)),l.set(e,n)},p=(e,t)=>{let n=c.get(e);c.set(e,n==null?[t]:[...n,t])};for(let n of e)i'''
    r'''f(n.kind===`local`){if(n.pendingThreadStart!=null)continue;if(n.pendingWorktree!=null){let e=n.pendingWorktree.h'''
    r'''ostId,r=n.pendingWorktree.sourceWorkspaceRoot;r&&(e===t||s.has(e))&&(p(e,r),f(e,r));let i=Rr(n.pendingWorktree.s'''
    r'''tartConversationParamsInput?.workspaceRoots)??n.pendingWorktree.startConversationParamsInput?.cwd??r;i&&u.add(Kf'''
    r'''(i));continue}let e=n.hostId==null||Pc(n.hostId)?t:n.hostId,c=o?.threadProjectAssignments?.[n.conversationId];if'''
    r'''(!(o?.projectlessThreadIds?.has(n.conversationId)||c?.projectKind===`local`&&(c.projectOrigin===`chatgpt`||d.has'''
    r'''(c.projectId))||c?.projectKind===`remote`&&c.hostId===e&&r.some(t=>t.id===c.projectId&&t.hostId===e))&&n.cwd&&f('''
    r'''e,n.cwd),n.summary!=null&&!Ch(n.cwd,i,a?.[e])||n.workspaceKind===`projectless`||n.cwd===`~`)continue;let l=n.cwd'''
    r''';if(!l||e!==t&&!s.has(e))continue;p(e,l);continue}if(o!=null){for(let e of n??[])f(t,e);for(let[e,t]of c)c.set(e'''
    r''',t.filter(t=>l.get(e)?.has(Kf(t).replace(/\/+$/,``))||u.has(Kf(t))))}for(let e of r)p(e.hostId,e.remotePath);ret'''
    r'''urn Array.from(c.entries()).map(([e,t])=>({hostId:e,dirs:(0,VIn.default)(t).sort((e,t)=>e.localeCompare(t))})).f'''
    r'''ilter(({hostId:e,dirs:n})=>e===t||n.length>0)}'''
)

UIN_3930 = (
    r'''UIn=(e,t,n,r,i,a,o=Qr,s)=>{let c=e.hostId==null||Pc(e.hostId)?o:e.hostId,l=s?.threadProjectAssignments?.[e.conve'''
    r'''rsationId];if(c!==o&&s?.enabledRemoteHostIds!=null&&!s.enabledRemoteHostIds.has(c))return;let u=l!=null&&(l.proj'''
    r'''ectKind===`local`||l.hostId!=null&&c===l.hostId)?OIn(l,t):null;if(u!=null){u.threadKeys.push(e.key);return}if(l?'''
    r'''.projectKind===`local`&&l.projectOrigin===`chatgpt`)return;let d=e.cwd;if(!d||!OFn(d).length)return;let f=d;if(e'''
    r'''.workspaceKind===`projectless`||s?.projectlessThreadIds?.has(e.conversationId)===!0)return;let p=c!==o,m=s?.remo'''
    r'''teProjects,h=s?.remoteConnections?.find(e=>e.hostId===c),g=(m??[]).filter(e=>{if(e.hostId===c)return!1;let t=s?.'''
    r'''remoteConnections?.find(t=>t.hostId===e.hostId);return NFn(h,t)});if(p&&g.length===0&&!m?.some(e=>e.hostId===c))'''
    r'''return;let _=MIn({gitOrigins:r,gitOriginsByHostId:i,hostId:c??void 0,primaryHostId:o}),v=[...p?Object.entries((0'''
    r''',BIn.default)(g,e=>e.hostId)).flatMap(([e,t])=>jIn(t,e,d,s?.codexHomesByHostId?.[e],s?.worktreesRootsByHostId?.['''
    r'''e])):[]];if(v.length===1){(t.find(e=>e.projectId===v[0]?.id)??null)?.threadKeys.push(e.key);return}if(v.length>1'''
    r''')return;if(Ch(d,a,s?.worktreesRootsByHostId?.[c])||p&&NIn(d,_)){let r=PIn(d,e.conversationId,t,n,_,s?.threadWork'''
    r'''spaceRootHints,e.summary!=null);r&&(f=r)}let y=(m??[]).filter(e=>e.hostId===c),b=jFn(m,c,f)??DIn(y,f,s?.canonica'''
    r'''lProjectPathsByHostId)??DIn(g,f,s?.canonicalProjectPathsByHostId);if(b!=null){let n=t.find(e=>e.projectId===b.id'''
    r''')??null;if(n!=null){n.threadKeys.push(e.key);return}}if(p)return;let x=TIn(n,f);x&&(x.threadKeys.push(e.key),f!='''
    r'''=d&&s?.onDiscoverThreadWorkspaceRootHint?.(e.conversationId,x.path))}'''
)

CONDITION_3930 = b'if(Ch(d,a,s?.worktreesRootsByHostId?.[c])||p&&NIn(d,_)){let r=PIn('
GUARDED_3930 = (b'if(!(p&&(jFn(m,c,d)??DIn((m??[]).filter(e=>e.hostId===c),d,s?.canonicalProjectPathsByHostId)))'
                b'&&(Ch(d,a,s?.worktreesRootsByHostId?.[c])||p&&NIn(d,_))){let r=PIn(')
SKIP_3930 = b'n.summary!=null&&!Ch(n.cwd,i,a?.[e])||'
REMOTE_SKIP_3930 = b'n.summary!=null&&e===t&&!Ch(n.cwd,i,a?.[e])||'
CALL_3930, ORDERED_CALL_3930 = order_call(b'PIn', b'gj', b'Kf', b'NIn')
VERSION_3930 = '26.930.31730'  # package.json version of the 26.930.3930 desktop
SOURCE_3930 = (KIN_3930 + ';' + UIN_3930).encode()
PATCHED_3930 = (SOURCE_3930.replace(SKIP_3930, REMOTE_SKIP_3930).replace(CONDITION_3930, GUARDED_3930)
                .replace(CALL_3930, ORDERED_CALL_3930))


class Grouping3930Tests(unittest.TestCase):
    def test_3930_gets_each_patch_once(self):
        self.assertEqual(apply_grouping(SOURCE_3930), PATCHED_3930)
        self.assertTrue(project_grouping_patched(PATCHED_3930))
        self.assertFalse(project_grouping_patched(SOURCE_3930))
        self.assertEqual(apply_grouping(PATCHED_3930), PATCHED_3930)
        # A copy built with the revision 96 guard only gains the order and the lookup.
        self.assertEqual(apply_grouping(SOURCE_3930.replace(CONDITION_3930, GUARDED_3930)), PATCHED_3930)
        # Each build's collector follows only its own grouping.
        self.assertEqual(renderer_summary_dir_patches((KIN_3930 + ';' + AIN_4958).encode()), {})
        self.assertEqual(renderer_summary_dir_patches((GIN_4958 + ';' + UIN_3930).encode()), {})
        with self.assertRaises(ValueError):
            renderer_summary_dir_patches((KIN_3930 + ';' + KIN_3930 + ';' + UIN_3930).encode())

    def test_26_930_3930_copies_are_patched_not_refused(self):
        for label in COPIES:
            with self.subTest(label):
                data = patch_copy(label, SOURCE_3930, VERSION_3930)
                self.assertEqual(data.count(PATCHED_3930), 1)
                with self.assertRaisesRegex(ValueError, 'SSH worktree'):
                    patch_copy(label, KIN_3930.encode(), VERSION_3930)


# Helpers the 26.930.4958 functions import from app-shared, reduced to what
# these paths need (same reductions as STUBS): yh path key, op normalized root,
# ig base name, $r/Fi primary host, ORe and Yu Codex worktree roots (Yu also
# recognizes ~/.codex/worktrees/... like the native test). No SSH host is the
# same machine as another (no connections), no origin URLs, no duplicate roots.
STUBS_4958 = r'''
const yh = e => { const t = e.replace(/\\/g, `/`).toLowerCase(), r = t.match(/^\/?([a-z]):(?:\/(.*))?$/);
  return r ? (r[2] ? `/mnt/${r[1]}/${r[2]}` : `/mnt/${r[1]}`) : t; };
const op = e => { const t = yh(e.trim()).replace(/\/+/g, `/`); return t === `/` ? t : t.replace(/\/+$/, ``); };
const ig = e => { const t = e.replace(/\\/g, `/`).replace(/\/+$/, ``); return t.split(`/`).at(-1) ?? t; };
const $r = `local`, Fi = e => e === $r;
const ORe = e => `${e.replace(/[\\/]+$/, ``)}/worktrees`;
const Yu = (e, t, n) => { if (!e) return !1; const r = yh(e), own = /^(.*\/(?:\.codex|\.codex-workspaces(?:\/instances\/[^/]+)?|OpenAI\/Codex\/workspaces(?:\/instances\/[^/]+)?)\/worktrees)(?:\/|$)/i.exec(e.replace(/\\/g, `/`))?.[1];
  return [n?.trim(), t == null ? null : ORe(t), own].some(x => { if (!x) return !1; const t = yh(x).replace(/\/+$/, ``);
    return r === t || r.startsWith(`${t}/`); }); };
const Wge = {safeParse: () => ({success: !1})};
const Ne = () => !1, oFn = () => null, To = e => e?.[0] ?? null, IUe = () => !1, DSe = () => !1;
const EIn = {default: Object.fromEntries}, OIn = {default: items => [...new Set(items)]};
const DIn = {default: (items, key) => { const out = {}; for (const item of items) (out[key(item)] ??= []).push(item); return out; }};
const SIn = () => { throw Error(`pending worktree`); }, CIn = () => { throw Error(`cloud task`); };
'''

# The 26.930.4958 sidebar atom: git info for the project roots, then for the
# thread folders the collector adds; each host answers for the folders it is
# asked about (c.git is what git would report there). Then the grouping.
RUNNER_4958 = r'''
const results = CASES.map(c => {
  const assignments = c.assignments, projectless = new Set(c.projectless);
  const roots = gIn([], $r, c.localRoots, c.remoteProjects);
  const asked = _In(gIn(c.items, $r, c.localRoots, c.remoteProjects, c.codexHome, {}, {
    localProjects: c.localProjects, threadProjectAssignments: assignments, projectlessThreadIds: projectless}), roots);
  const origins = {};
  for (const {hostId, dirs} of [...roots, ...asked]) for (const dir of dirs) {
    const found = (c.git[hostId] ?? []).find(o => yh(o.dir) === yh(dir));
    if (found) (origins[hostId] ??= []).push(found);
  }
  const groups = rIn(c.items, [], c.groups, Object.values(origins).flat(), c.codexHome, {
    canonicalProjectPathsByHostId: c.canonical ?? void 0, codexHomesByHostId: {}, worktreesRootsByHostId: {},
    gitOriginsByHostId: origins, primaryHostId: $r, remoteConnections: [], remoteProjects: c.remoteProjects,
    threadProjectAssignments: assignments, projectlessThreadIds: projectless, threadWorkspaceRootHints: {},
    onDiscoverThreadWorkspaceRootHint: () => {}});
  const placed = {};
  for (const group of groups) for (const key of group.threadKeys) placed[key] = group.projectId;
  return {placed: Object.fromEntries(c.items.map(item => [item.key, placed[item.key] ?? null])),
          asked: Object.fromEntries(asked.map(({hostId, dirs}) => [hostId, dirs]))};
});
console.log(JSON.stringify(results));
'''

# What git reports for each folder (dir) on each host when asked. FEATURE is a
# linked worktree of parent that is no declared project.
GIT = {
    HOST: [*DECLARED, origin('/srv/projects/parent', '/srv/projects/parent'), STUDIO_CWD, STUDIO_SRC_CWD,
           FEATURE_CWD, origin(FEATURE + '/src', FEATURE),
           origin('/home/dev/.codex/worktrees/ab12/parent', '/home/dev/.codex/worktrees/ab12/parent')],
    'local': list(LOCAL_ORIGINS),
}


def row(key, cwd, host=HOST, loaded=False, **extra):
    """A sidebar row; a thread that is not loaded in this window has only its summary."""
    return dict(kind='local', key=key, conversationId='thread-' + key, hostId=host, cwd=cwd, **extra,
                **({} if loaded else {'summary': {'title': key}}))


def sidebar(order, items, canonical=CANONICAL, local_groups=(), assignments=None, projectless=()):
    projects = sorted(order, key=lambda project: project['id'])  # declaration order, not the sidebar order
    groups = [*local_groups, *(dict(groupId=p['id'], projectId=p['id'], projectKind='remote', hostId=p['hostId'],
                                    hostDisplayName=None, label=p['label'], path=p['remotePath'], gitRepos=[],
                                    isCodexWorktree=False) for p in order)]
    return dict(groups=groups, remoteProjects=projects, items=items, codexHome=CODEX_HOME, git=GIT,
                localRoots=[group['path'] for group in local_groups],
                localProjects={group['projectId']: {'id': group['projectId']} for group in local_groups},
                canonical=canonical, assignments=assignments or {}, projectless=list(projectless))


@unittest.skipUnless(shutil.which('node'), 'Node.js required for renderer behavior')
class RemoteWorktreeGrouping4958BehaviorTests(unittest.TestCase):
    def run_sidebar(self, collector, grouping, cases):
        program = '\n'.join([STUBS_4958, *HELPERS_4958.values(), collector, 'const ' + grouping + ';',
                             RUNNER_4958.replace('CASES', json.dumps(cases))])
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / 'grouping.cjs'
            script.write_text(program, encoding='utf-8')
            result = subprocess.run(['node', str(script)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def variants(self, cases):
        collector, grouping = patched_4958()
        _, unordered = patched_4958(order=False)
        return {'native': self.run_sidebar(GIN_4958, AIN_4958, cases),
                'collector only': self.run_sidebar(collector, AIN_4958, cases),
                'unordered': self.run_sidebar(collector, unordered, cases),
                'patched': self.run_sidebar(collector, grouping, cases)}

    def test_unloaded_worktree_threads_join_the_repo_project(self):
        unloaded = [row('f', FEATURE), row('s', FEATURE + '/src')]
        cases = {'symlinked declaration': sidebar((PARENT,), unloaded),
                 'physical declaration': sidebar((PARENT_P,), unloaded, PHYSICAL_CANONICAL),
                 'opened thread': sidebar((PARENT,), [row('f', FEATURE, loaded=True), row('s', FEATURE + '/src')]),
                 # With a declared worktree project beside the repository, in both sidebar orders.
                 'symlinked, parent first': sidebar(FORWARD, unloaded),
                 'symlinked, studio first': sidebar(REVERSE, unloaded),
                 'physical, parent first': sidebar(PHYSICAL_FORWARD, unloaded, PHYSICAL_CANONICAL),
                 'physical, studio first': sidebar(PHYSICAL_REVERSE, unloaded, PHYSICAL_CANONICAL)}
        found = self.variants(list(cases.values()))
        for index, label in enumerate(cases):
            native, unordered, patched = (found[name][index] for name in ('native', 'unordered', 'patched'))
            with self.subTest(label):
                self.assertEqual(patched['placed'], {'f': 'parent', 's': 'parent'})
                self.assertEqual(patched['asked'], {HOST: [FEATURE, FEATURE + '/src']})
                if label == 'opened thread':
                    # Natively only the opened thread's folder is asked about.
                    self.assertEqual(native['placed'], {'f': 'parent', 's': None})
                else:
                    self.assertEqual(native['placed'], {'f': None, 's': None})
                    self.assertEqual(native['asked'], {})
                # Without the order the remap takes the first project in this sidebar.
                first = 'studio' if label.endswith('studio first') else 'parent'
                self.assertEqual(unordered['placed'], {'f': first, 's': first})

    def test_threads_inside_a_declared_worktree_stay_with_it_in_any_order(self):
        # A subfolder of the declared studio worktree, and its root while the
        # host's canonical path table is missing (the guard cannot tell then).
        subfolder, root = [row('x', STUDIO_SRC)], [row('a', '/srv/projects/studio'), row('b', '/srv/projects/studio')]
        cases = {'subfolder, symlinked, parent first': (sidebar(FORWARD, subfolder), 'parent'),
                 'subfolder, symlinked, studio first': (sidebar(REVERSE, subfolder), 'studio'),
                 'subfolder, physical, parent first': (sidebar(PHYSICAL_FORWARD, subfolder, PHYSICAL_CANONICAL), 'studio'),
                 'subfolder, physical, studio first': (sidebar(PHYSICAL_REVERSE, subfolder, PHYSICAL_CANONICAL), 'studio'),
                 'root without canonical paths, parent first': (sidebar(FORWARD, root, None), 'parent'),
                 'root without canonical paths, studio first': (sidebar(REVERSE, root, None), 'studio')}
        found = self.variants([scenario for scenario, _ in cases.values()])
        for index, (label, (scenario, unordered)) in enumerate(cases.items()):
            keys = [item['key'] for item in scenario['items']]
            with self.subTest(label):
                self.assertEqual(found['patched'][index]['placed'], dict.fromkeys(keys, 'studio'))
                self.assertEqual(found['unordered'][index]['placed'], dict.fromkeys(keys, unordered))
                # Natively these summary rows are not asked about and stay hidden.
                self.assertEqual(found['native'][index]['placed'], dict.fromkeys(keys))

    def test_declared_worktree_root_keeps_its_threads_in_any_order(self):
        unloaded = [row('a', '/srv/projects/studio'), row('b', '/srv/projects/studio'), row('p', '/srv/projects/parent')]
        opened = [*unloaded, row('c', '/srv/projects/studio', loaded=True)]
        cases = {'parent first': sidebar(FORWARD, unloaded), 'studio first': sidebar(REVERSE, unloaded),
                 'parent first, one opened': sidebar(FORWARD, opened),
                 'studio first, one opened': sidebar(REVERSE, opened)}
        found = self.variants(list(cases.values()))
        for index, label in enumerate(cases):
            keys = [item['key'] for item in cases[label]['items']]
            studio = {key: 'parent' if key == 'p' else 'studio' for key in keys}
            moved = {key: 'parent' for key in keys}
            with self.subTest(label):
                self.assertEqual(found['patched'][index]['placed'], studio)
                self.assertEqual(found['unordered'][index]['placed'], studio)
                parent_first = label.startswith('parent first')
                # Native 4958 has no remote-root guard: an opened thread moves its folder.
                self.assertEqual(found['native'][index]['placed'],
                                 moved if parent_first and 'opened' in label else studio)
                # The collector alone would move every unloaded thread as well.
                self.assertEqual(found['collector only'][index]['placed'], moved if parent_first else studio)

    def test_local_assigned_and_projectless_rows_are_unchanged(self):
        assigned = {'thread-assigned': dict(projectKind='remote', projectId='studio', hostId=HOST)}
        local_rows = [row('root', 'C:\\work\\app', None), row('declared-worktree', 'C:/work/app-wt', None),
                      row('codex-worktree', CODEX_HOME + '/worktrees/ab12/app', 'local'),
                      row('undeclared-worktree', 'C:/work/app-other', None), row('subdirectory', 'C:/work/app/src', None)]
        local_rows += [row(item['key'] + '-opened', item['cwd'], item['hostId'], loaded=True) for item in local_rows]
        remote_rows = [row('assigned', FEATURE), row('pinned', FEATURE + '/src'),
                       row('chat', '/srv/scratch', workspaceKind='projectless'), row('home', '~'),
                       row('other-host', FEATURE, OTHER_HOST), row('codex', '/home/dev/.codex/worktrees/ab12/parent')]
        case = sidebar(FORWARD, local_rows + remote_rows, local_groups=LOCAL_GROUPS, assignments=assigned,
                       projectless=['thread-pinned'])
        found = self.variants([case])
        native, patched = found['native'][0], found['patched'][0]
        self.assertEqual(patched, native)
        self.assertEqual(found['unordered'][0], native)
        self.assertEqual({key: native['placed'][key] for key in LOCAL_PLACEMENT}, LOCAL_PLACEMENT)
        self.assertEqual({item['key']: native['placed'][item['key']] for item in remote_rows},
                         {'assigned': 'studio', 'pinned': None, 'chat': None, 'home': None, 'other-host': None,
                          'codex': 'parent'})
        self.assertEqual(native['asked'][HOST], ['/home/dev/.codex/worktrees/ab12/parent'])


def app_initial_chunks(pattern, variable):
    """app-initial chunks on this machine: managed copies matching pattern
    (read-only; before or after these patches) and an extracted chunk named by
    the environment variable."""
    found = {}
    extracted = os.environ.get(variable)
    if extracted:
        found['extracted chunk'] = Path(extracted).read_bytes()
    for archive in managed_archives(pattern):
        renderers = read_entries(archive, lambda name: name.startswith('webview/assets/app-initial') and name.endswith('.js'))
        found.update({archive.parent.parent.name + ' ' + name: data for name, data in renderers.items()})
    return found


def renderers_4958():
    return app_initial_chunks('26.930.4958.*', 'CODEX_DESKTOP_RENDERER_4958')


def grouping_forms(source, condition, guarded, call, ordered):
    """A grouping function as built: native, with the revision 96 guard (and the
    collector of revision 119 part 3), or with the guard and the order."""
    source = source.encode() if isinstance(source, str) else source
    guarded_source = source.replace(condition, guarded)
    return source, guarded_source, guarded_source.replace(call, ordered)


class ManagedRendererChecks:
    """Checks a managed (or extracted) 26.930 app-initial chunk against verbatim sources."""

    def check(self, renderers, collector, skip, remote_skip, forms, helpers=None):
        for label, data in renderers.items():
            with self.subTest(label):
                self.assertEqual(data.count(collector) + data.count(collector.replace(skip, remote_skip)), 1)
                self.assertEqual(sum(data.count(form) for form in forms), 1)
                for name, source in (helpers or {}).items():
                    self.assertEqual(data.count(source.encode()), 1, name)
                if not project_grouping_patched(data):
                    # Built before (some of) these patches: the missing ones apply once.
                    once = apply_grouping(data)
                    self.assertTrue(project_grouping_patched(once))
                    self.assertEqual(once.count(forms[-1]), 1)
                    self.assertEqual(once.count(collector.replace(skip, remote_skip)), 1)
                    self.assertEqual(apply_grouping(once), once)


class ManagedRenderer4958Tests(ManagedRendererChecks, unittest.TestCase):
    def test_26_930_4958_renderer_matches_the_verified_sources(self):
        renderers = renderers_4958()
        if not renderers:
            self.skipTest('No 26.930.4958 renderer on this machine (no managed copy; set CODEX_DESKTOP_RENDERER_4958 '
                          'to an extracted app-initial chunk to check one).')
        self.check(renderers, GIN_4958.encode(), SKIP_4958, REMOTE_SKIP_4958,
                   grouping_forms(AIN_4958, CONDITION_4958, GUARDED_4958, CALL_4958, ORDERED_CALL_4958), HELPERS_4958)


class ManagedRenderer7945Tests(ManagedRendererChecks, unittest.TestCase):
    def test_26_930_7945_renderer_matches_the_4958_sources(self):
        renderers = app_initial_chunks('26.930.7945.*', 'CODEX_DESKTOP_RENDERER_7945')
        if not renderers:
            self.skipTest('No 26.930.7945 renderer on this machine (no managed copy; set CODEX_DESKTOP_RENDERER_7945 '
                          'to an extracted app-initial chunk to check one).')
        self.check(renderers, GIN_4958.encode(), SKIP_4958, REMOTE_SKIP_4958,
                   grouping_forms(AIN_4958, CONDITION_4958, GUARDED_4958, CALL_4958, ORDERED_CALL_4958), HELPERS_4958)


class ManagedRenderer3930Tests(ManagedRendererChecks, unittest.TestCase):
    def test_26_930_3930_renderer_matches_the_verified_sources(self):
        renderers = app_initial_chunks('26.930.3930.*', 'CODEX_DESKTOP_RENDERER_3930')
        if not renderers:
            self.skipTest('No 26.930.3930 renderer on this machine (no managed copy; set CODEX_DESKTOP_RENDERER_3930 '
                          'to an extracted app-initial chunk to check one).')
        self.check(renderers, KIN_3930.encode(), SKIP_3930, REMOTE_SKIP_3930,
                   grouping_forms(UIN_3930, CONDITION_3930, GUARDED_3930, CALL_3930, ORDERED_CALL_3930))


if __name__ == '__main__':
    unittest.main()
