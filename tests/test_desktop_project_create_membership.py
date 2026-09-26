"""Tasks in a new project's folder appear in the window that created it too.

Reported case: one profile created a local project over a folder that already
had older CLI tasks. Every other profile listed them under the new project at
once. The creating window showed none of them.

The 26.917 main process runs t8e before a local project gains folders (create,
add folder, edit folders). It marks every unassigned task in those folders as
projectless, so the project starts empty. That mark lives only in the creating
window's legacy state. Native membership has no projectless value, and peers
get only the shared project declaration, so they group the same tasks by
folder. The renderer then drops projectless tasks from the project's catalog
page (JUr adds them to excludeThreadIds, and the sidebar grouping returns early
for them).

The main patch routes the marks through the membership adapter, which drops
them in shared profiles. Explicit assignments to an existing project still go
through the native write. The functions below are verbatim 26.917 sources. The
guard test checks them against a managed 26.917 copy when one exists here.
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from manager_core import desktop_bundle, original_sync_bundle
from manager_core.original_sync_bundle import main_projectless_pin_patches, patches_for

# Main process (.vite/build/main-*.js): the pre-folder-change assignment and its
# project root index.
T8E_917 = (
    r'''async function t8e({affectedRootPaths:e,excludedThreadIds:t,getGitOriginUrl:r,globalState:i,host:a,listThreads:o,matchRootsByCanonicalHostIdentity:s,readThread:c,threadProjectAssignments:l}){'''
    r'''let u=new Set(e);if(u.size===0)return;let d=jr(i),f=Array.from(new Set([...Fr(i).roots,...u])),p=n.zi(a),m=await Kn(f,a,p),h=s?new Set(Object.values(m)):null,'''
    r'''g=h==null?null:e=>m[e]??(h.has(e)?e:n._l(e)),_=g??n._l,v=Gn(Array.from(u),a,p),y=new Set([...v,...v.flatMap(e=>{let t=m[e];return t==null?[]:[t]})]),'''
    r'''b=n8e(d,m,a,p),x=n.ca(i.get(n.iu.THREAD_PROJECT_ASSIGNMENTS)),S=new Set(i.get(n.iu.PROJECTLESS_THREAD_IDS)??[]),C=new Set(Array.from(y,_)),w=new Map;'''
    r'''for(let e of b.values()){let t=(0,re.basename)(n._l(e.rootPath)),r=w.get(t);r===void 0?w.set(t,e):r?.projectId!==e.projectId&&w.set(t,null)}'''
    r'''let T=new Map(Object.entries(i.get(n.iu.THREAD_WORKSPACE_ROOT_HINTS)??{}).filter(([e,n])=>C.has(_(n))&&x[e]==null&&!t.has(e)&&!S.has(e)));'''
    r'''for(let e of(0,dp.default)(Array.from(T.keys()),e8e)){let t=await Promise.all(e.map(async e=>{let t;try{t=await c(e)}catch(n){'''
    r'''if(!(n instanceof Error)||n.message!==`thread not loaded: ${e}`)throw n;t=null}if(t==null){T.delete(e);return}'''
    r'''let i=w.get((0,re.basename)(n._l(t.cwd))),a=T.get(e),o=a==null?void 0:b.get(n._l(a));if(i!=null&&o?.projectId!==i.projectId){'''
    r'''let[n,a]=await Promise.all([t.gitInfo?.originUrl==null?r(t.cwd):Promise.resolve(t.gitInfo.originUrl),r(i.rootPath)]);'''
    r'''if(n!=null&&n===a){T.delete(e);return}}return t.cwd}));for(let e of t)e!=null&&y.add(e)}let E={},D=[],O=null;'''
    r'''do{let e=await o({archived:!1,cursor:O,cwd:Array.from(y),limit:100,sourceKinds:n.pa,useStateDbOnly:!0});'''
    r'''for(let r of e.data){if(x[r.id]!=null||t.has(r.id)||S.has(r.id)||!C.has(_(r.cwd))&&!T.has(r.id))continue;'''
    r'''let e=T.get(r.id)??r.cwd,i=b.get(n._l(e));if(i==null||g!=null&&g(i.rootPath)!==g(e)){S.add(r.id),D.push(r.id);continue}'''
    r'''let a={projectKind:`local`,projectId:i.projectId};x[r.id]=a,E[n.$u(r.id)]=a}O=e.nextCursor}while(O!=null);'''
    r'''await l.assignIfUnassigned([...Object.entries(E).flatMap(([e,t])=>t==null?[]:[{threadId:n.$u(e),assignment:t,projectless:!1}]),'''
    r'''...D.map(e=>({threadId:n.$u(e),assignment:null,projectless:!0}))])}'''
)
N8E_917 = (
    r'''function n8e(e,t,r,i){let a=new Map,o=new Map;for(let s of Object.values(e)){let e=Gn(s.rootPaths,r,i);for(let r of e){let i=n._l(r),c=t[r];'''
    r'''if(c!=null){let e=n._l(c),t=o.get(e);t===void 0?o.set(e,i):t!==i&&o.set(e,null)}'''
    r'''let l={createdAt:s.createdAt,projectId:s.id,rootPath:r,rootPaths:e},u=a.get(i);(u==null||n.Ko(l,u))&&a.set(i,l)}}'''
    r'''for(let[e,t]of o){if(t==null||a.has(e))continue;let n=a.get(t);n!=null&&a.set(e,n)}return a}'''
)
# Renderer (app-initial): the project's catalog page scope and its helpers.
JUR_917 = (
    r'''function JUr({projectId:e,projectKind:t,hostIds:n,cwdValues:r,projectlessThreadIds:i,threadProjectAssignments:a,threadWorkspaceRootHints:o,sortKey:s,manualThreadIds:c,separatelyLoadedThreadIds:l}){'''
    r'''let u=YUr(r),d=new Set(u.map(Ar)),f=new Set,p=new Set(i??[]);for(let[e,t]of Object.entries(o??{}))d.has(Ar(t))&&f.add(e);'''
    r'''for(let[n,r]of Object.entries(a??{}))r.projectKind===t&&r.projectId===e?(f.add(n),p.delete(n)):(f.delete(n),p.add(n));'''
    r'''for(let e of l??[])f.delete(e),p.add(e);return{key:`project:${e}`,hostIds:n,sortKey:s,manualThreadIds:c,'''
    r'''filter:{cwdValues:u,cwdPrefixes:u.map(ZUr),includeThreadIds:Array.from(f).sort(),excludeThreadIds:Array.from(p).sort()}}}'''
)
RENDERER_HELPERS_917 = (
    r'''function YUr(e){return Array.from(new Set(e.filter(Boolean))).sort()}''',
    r'''function ZUr(e){return e.endsWith(`/`)||e.endsWith(`\\`)?e:`${e}${e.includes(`\\`)?`\\`:`/`}`}''',
)

PINS = b'...D.map(e=>({threadId:n.$u(e),assignment:null,projectless:!0}))])'
ROUTED = (b'...(globalThis.__codexProjectMembership?.projectlessPins?.(D)??D)'
          b'.map(e=>({threadId:n.$u(e),assignment:null,projectless:!0}))])')


def patched(source):
    data = source.encode() if isinstance(source, str) else source
    for before, after in main_projectless_pin_patches(data).items():
        data = data.replace(before, after)
    return data


class ProjectlessPinPatchSelectionTests(unittest.TestCase):
    def test_verified_assignment_gets_one_route(self):
        source = (T8E_917 + N8E_917).encode()
        patches = main_projectless_pin_patches(b'let x=1;' + source + b';let y=2')
        self.assertEqual(len(patches), 1)
        (before, after), = patches.items()
        self.assertEqual(source.count(before), 1)
        # Only the projectless list changes; explicit assignments stay byte-identical.
        self.assertEqual(source.replace(before, after), source.replace(PINS, ROUTED))

    def test_26_915_names_are_accepted(self):
        # 26.915 (B6e) differs only in minified helper names.
        source = T8E_917.replace('n.$u(', 'n.Iu(').replace('n._l(', 'n.el(').encode()
        (before, after), = main_projectless_pin_patches(source).items()
        self.assertIn(b'projectlessPins?.(D)??D).map(e=>({threadId:n.Iu(e),assignment:null,projectless:!0}))', after)

    def test_missing_or_changed_expression_stays_native(self):
        source = T8E_917.encode()
        self.assertEqual(main_projectless_pin_patches(b''), {})
        for old, new in ((b'assignment:null,projectless:!0', b'assignment:null,projectless:!1'),
                         (b'...D.map(e=>({threadId:n.$u(e)', b'...D.map(e=>({threadId:n.Iu(e)'),
                         (b'assignIfUnassigned(', b'restoreMemberships(')):
            with self.subTest(old):
                mutated = source.replace(old, new)
                self.assertNotEqual(mutated, source)
                self.assertEqual(main_projectless_pin_patches(mutated), {})

    def test_ambiguous_expression_is_refused(self):
        source = T8E_917.encode()
        with self.assertRaises(ValueError):
            main_projectless_pin_patches(source + b';' + source)

    def test_patched_assignment_is_not_patched_again(self):
        once = patched(T8E_917)
        self.assertEqual(once.count(ROUTED), 1)
        self.assertEqual(main_projectless_pin_patches(once), {})
        self.assertEqual(patched(once), once)

    def test_sync_entry_points_carry_the_route(self):
        sync = b';'.join(original_sync_bundle.PATCHES)
        data = sync + b';' + T8E_917.encode()
        for before, after in patches_for(data).items():
            data = data.replace(before, after)
        self.assertEqual(data.count(ROUTED), 1)
        # A version without the expression keeps its verified sync entry points.
        self.assertEqual(patches_for(sync), original_sync_bundle.PATCHES)


# Main-process helpers t8e imports, reduced to local Windows paths: n._l is the
# desktop path key (no \\?\ prefix, forward slashes, lower case, drive letters as
# /mnt/x). No symlinked roots (Kn), no git origins, no root hints.
MAIN_STUBS = r'''
const key = p => { const t = p.replace(/^\\\\\?\\/, ``).replace(/\\/g, `/`).toLowerCase(), r = t.match(/^\/?([a-z]):(?:\/(.*))?$/);
  return r ? (r[2] ? `/mnt/${r[1]}/${r[2]}` : `/mnt/${r[1]}`) : t; };
const n = {iu: {THREAD_PROJECT_ASSIGNMENTS: `thread-project-assignments`, PROJECTLESS_THREAD_IDS: `projectless-thread-ids`,
  THREAD_WORKSPACE_ROOT_HINTS: `thread-workspace-root-hints`, LOCAL_PROJECTS: `local-projects`},
  zi: () => !1, _l: key, ca: e => ({...e ?? {}}), $u: e => e, pa: [], Ko: (e, t) => e.createdAt < t.createdAt};
const re = {basename: e => e.split(`/`).filter(Boolean).at(-1) ?? e};
const dp = {default: (e, t) => { const r = []; for (let n = 0; n < e.length; n += t) r.push(e.slice(n, n + t)); return r; }};
const e8e = 8, jr = e => e.get(n.iu.LOCAL_PROJECTS) ?? {}, Gn = e => e, Kn = async () => ({});
const Fr = e => ({roots: Object.values(jr(e)).flatMap(e => e.rootPaths)});
'''

# Renderer path key Ar (same reduction as n._l).
RENDERER_STUBS = r'''
const Ar = e => { const t = e.replace(/\\/g, `/`).toLowerCase(), r = t.match(/^\/?([a-z]):(?:\/(.*))?$/);
  return r ? (r[2] ? `/mnt/${r[1]}/${r[2]}` : `/mnt/${r[1]}`) : t; };
'''

# For each case: run t8e as the creating window's projects manager does, then
# apply its marks to that window's legacy state. Build the new project's
# catalog page for the creating window and for a peer (declaration only, no
# marks). Select catalog rows the way readPage's exact-cwd query does:
# cwd IN cwdValues AND thread_id NOT IN excludeThreadIds.
RUNNER = r'''
(async () => {
  const results = [];
  for (const c of CASES) {
    const state = {...c.state}, requests = [], written = [];
    const globalState = {get: k => state[k]};
    const listThreads = async params => {
      requests.push(params);
      const wanted = new Set(params.cwd.map(key));
      return {data: c.threads.filter(t => wanted.has(key(t.cwd))), nextCursor: null};
    };
    await t8e({affectedRootPaths: c.roots, excludedThreadIds: new Set, getGitOriginUrl: async () => null,
      globalState, host: {}, listThreads, matchRootsByCanonicalHostIdentity: !1,
      readThread: async () => null, threadProjectAssignments: {assignIfUnassigned: async e => { written.push(...e); }}});
    const marked = written.filter(e => e.projectless).map(e => e.threadId);
    const page = projectless => JUr({projectId: c.project, projectKind: `local`, hostIds: [`local`], cwdValues: c.projectRoots,
      projectlessThreadIds: projectless, threadProjectAssignments: {...state[`thread-project-assignments`],
        ...Object.fromEntries(written.filter(e => e.assignment).map(e => [e.threadId, e.assignment]))},
      threadWorkspaceRootHints: {}, sortKey: void 0, manualThreadIds: void 0, separatelyLoadedThreadIds: []});
    const shown = scope => c.catalog.filter(r => scope.filter.cwdValues.includes(r.cwd) &&
      !scope.filter.excludeThreadIds.includes(r.threadId)).map(r => r.threadId).sort();
    const before = state[`projectless-thread-ids`] ?? [];
    results.push({written, requests: requests.length,
      creator: shown(page([...before, ...marked])), peer: shown(page(before))});
  }
  console.log(JSON.stringify(results));
})().catch(e => { console.error(e); process.exitCode = 1; });
'''

FOLDER = 'D:\\dev\\new-app'  # the new project's folder, with older CLI tasks
NATIVE_FOLDER = '\\\\?\\' + FOLDER  # thread cwd as the native state DB stores it
APP = 'C:\\work\\app'
OLD = ['0190a000-0000-7000-8000-000000000001', '0190a000-0000-7000-8000-000000000002',
       '0190a000-0000-7000-8000-000000000003', '0190a000-0000-7000-8000-000000000004',
       '0190a000-0000-7000-8000-000000000005']
IN_CHATS = '11111111-1111-4111-8111-111111111111'  # moved to Chats by the user earlier
IN_APP = '22222222-2222-4222-8222-222222222222'  # explicitly in the app project
APP_TASK = '33333333-3333-4333-8333-333333333333'  # unassigned, in the app folder
FOLDER_ID, APP_ID = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'

THREADS = ([dict(id=t, cwd=NATIVE_FOLDER) for t in OLD] + [dict(id=IN_CHATS, cwd=NATIVE_FOLDER),
           dict(id=IN_APP, cwd=NATIVE_FOLDER), dict(id=APP_TASK, cwd=APP)])
# local_thread_catalog rows carry the normalized cwd.
CATALOG = [dict(threadId=t['id'], cwd=FOLDER if t['cwd'] == NATIVE_FOLDER else t['cwd']) for t in THREADS]
STATE = {'local-projects': {APP_ID: dict(id=APP_ID, name='app', rootPaths=[APP], createdAt=1)},
         'thread-project-assignments': {IN_APP: dict(projectKind='local', projectId=APP_ID)},
         'projectless-thread-ids': [IN_CHATS], 'thread-workspace-root-hints': {}}
CASES = [
    # createLocal: the new project's folder, before the project exists.
    dict(roots=[FOLDER], project=FOLDER_ID, projectRoots=[FOLDER], state=STATE, threads=THREADS, catalog=CATALOG),
    # editLocal: the app project gains the new folder (old and new roots).
    dict(roots=[APP, FOLDER], project=APP_ID, projectRoots=[APP, FOLDER], state=STATE, threads=THREADS, catalog=CATALOG),
]


@unittest.skipUnless(shutil.which('node'), 'Node.js required for desktop behavior')
class ProjectFolderMembershipBehaviorTests(unittest.TestCase):
    def run_cases(self, assignment, adapter):
        adapters = ''
        if adapter:
            adapters = '\n'.join((ROOT / 'scripts/manager_core' / name).read_text(encoding='utf-8')
                                 for name in ('desktop_signal_files.cjs', 'desktop_project_membership.cjs'))
        program = '\n'.join([adapters, MAIN_STUBS, RENDERER_STUBS, N8E_917, assignment, JUR_917,
                             *RENDERER_HELPERS_917, 'const CASES = ' + json.dumps(CASES) + ';', RUNNER])
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / 'project-folder.cjs'
            script.write_text(program, encoding='utf-8')
            env = {**os.environ, 'CODEX_RECORD_SIGNALS': str(Path(temporary) / 'signals')}
            result = subprocess.run(['node', str(script)], capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_native_creator_hides_the_folder_tasks_its_peers_show(self):
        create, edit = self.run_cases(T8E_917, adapter=True)
        self.assertEqual(create['requests'], 1)
        self.assertEqual([e for e in create['written'] if e['projectless']],
                         [dict(threadId=t, assignment=None, projectless=True) for t in OLD])
        self.assertEqual(create['creator'], [])
        self.assertEqual(create['peer'], sorted(OLD))
        self.assertEqual(edit['creator'], sorted([APP_TASK, IN_APP]))
        self.assertEqual(edit['peer'], sorted(OLD + [APP_TASK, IN_APP]))

    def test_shared_profiles_keep_folder_tasks_in_the_new_project(self):
        create, edit = self.run_cases(patched(T8E_917).decode(), adapter=True)
        self.assertEqual(create['written'], [])
        self.assertEqual(create['creator'], sorted(OLD))
        self.assertEqual(create['creator'], create['peer'])
        # An unassigned task already inside the edited project is still pinned to it natively.
        self.assertEqual(edit['written'], [dict(threadId=APP_TASK, assignment=dict(projectKind='local', projectId=APP_ID),
                                                projectless=False)])
        self.assertEqual(edit['creator'], sorted(OLD + [APP_TASK, IN_APP]))
        self.assertEqual(edit['creator'], edit['peer'])

    def test_without_the_adapter_the_route_is_native(self):
        native = self.run_cases(T8E_917, adapter=False)
        routed = self.run_cases(patched(T8E_917).decode(), adapter=False)
        self.assertEqual(routed, native)


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


class ProjectlessPinArchiveTests(unittest.TestCase):
    def test_managed_and_original_copies_route_the_marks(self):
        sync = b';'.join(original_sync_bundle.PATCHES) + b';' + (T8E_917 + N8E_917).encode()
        renderer = b';'.join(original_sync_bundle.RENDERER_PATCHES)
        copies = {
            'managed': (desktop_bundle.patch_archive, '.vite/build/context.js', [
                ('.vite/build/main.js', b'function s9(){' + desktop_bundle._ORIGINAL + b'return "posix";}'),
                ('.vite/build/notifications.js', desktop_bundle._NOTIFICATION_CLICK + b'originalCallback();})' +
                 desktop_bundle._NOTIFICATION_SHOW + desktop_bundle._WINDOW_MESSAGE),
                ('.vite/build/browser-runtime.js', b'Qr({' + desktop_bundle._BROWSER_RUNTIME + b');'),
                ('.vite/build/context.js', sync),
                ('webview/assets/app-initial-fixture.js', desktop_bundle._CONTEXT_RENDERER + b';' + renderer)]),
            'original': (original_sync_bundle.patch_archive, '.vite/build/main.js', [
                ('.vite/build/main.js', sync), ('webview/assets/app-initial-fixture.js', renderer)]),
        }
        with tempfile.TemporaryDirectory() as temporary:
            for label, (patch_archive, module, chunks) in copies.items():
                with self.subTest(label):
                    source, target = Path(temporary) / (label + '.asar'), Path(temporary) / (label + '-patched.asar')
                    write_archive(source, chunks)
                    patch_archive(source, target)
                    data = read_entries(target, lambda name: name == module)[module]
                    self.assertEqual(data.count(ROUTED), 1)
                    self.assertNotIn(PINS, data)
                    # The adapter that owns the route is bundled with the same module.
                    self.assertIn(b'projectlessPins:()=>[]', data)


class ManagedMainGuardTests(unittest.TestCase):
    def test_managed_copies_match_the_verified_sources(self):
        archives = sorted((ROOT / 'artifacts/managed-desktop').glob('26.91[57].*/resources/app.asar'))
        if not archives:
            self.skipTest('No managed 26.915/26.917 desktop copy on this machine.')
        for archive in archives:
            with self.subTest(archive.parent.parent.name):
                mains = read_entries(archive, lambda name: name.startswith('.vite/build/main-') and name.endswith('.js'))
                self.assertEqual(len(mains), 1)
                data, = mains.values()
                # Built before or after this patch: exactly one route point either way.
                self.assertEqual(len(main_projectless_pin_patches(data)) +
                                 data.count(b'__codexProjectMembership?.projectlessPins?.('), 1)
                # The expression sits in the module that received the sync patches
                # and the membership adapter, so patches_for routes it on a rebuild.
                self.assertIn(b'globalThis.__codexProjectMembership={', data)
                self.assertIn(b'globalThis.__codexLocalWorkspaceSync?.registerBackend(this);', data)
                if archive.parent.parent.name.startswith('26.917.'):
                    self.assertEqual(data.count(T8E_917.encode()) + data.count(patched(T8E_917)), 1)
                    self.assertEqual(data.count(N8E_917.encode()), 1)
                renderers = read_entries(archive, lambda name: name.startswith('webview/assets/app-initial')
                                         and name.endswith('.js'))
                if archive.parent.parent.name.startswith('26.917.'):
                    renderer, = renderers.values()
                    for source in (JUR_917, *RENDERER_HELPERS_917):
                        self.assertEqual(renderer.count(source.encode()), 1, source[:20])


if __name__ == '__main__':
    unittest.main()
