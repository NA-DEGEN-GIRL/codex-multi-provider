"""An archived source must not hide the active destination with the same ID."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from manager_core import original_sync_bundle
from manager_core.original_sync_bundle import renderer_host_identity_patches
from test_desktop_remote_project_grouping import managed_archives, read_entries

FILTER = (b'let n=RE(t,e);return n==null?t(WE).some(n=>t(n5n,n.getHostId()).includes(e)):'
          b't(n5n,n).includes(e)')
CATALOG = (b'QZn=uf($,(e,{get:t})=>{let n=null;for(let r of t(MT).entriesByKey.values())'
           b'if(r.sourceKind!==`chatgpt`&&r.threadId===e){if(n!=null&&n!==r.hostId)'
           b'return null;n=r.hostId}return n})')
# 26.917 renames every binding; both expressions are otherwise byte-identical.
FILTER_917 = (b'let n=oE(t,e);return n==null?t(pE).some(n=>t(KRn,n.getHostId()).includes(e)):'
              b't(KRn,n).includes(e)')
CATALOG_917 = (b'Ckn=ns(X,(e,{get:t})=>{let n=null;for(let r of t(Rw).entriesByKey.values())'
               b'if(r.sourceKind!==`chatgpt`&&r.threadId===e){if(n!=null&&n!==r.hostId)'
               b'return null;n=r.hostId}return n})')
# 26.930 (app-shared chunk), verbatim. 4958 swapped the catalog bindings:
# uFn is the initial catalog state there, dFn the enabled flag (3930's uFn).
FILTER_3930 = (b'let n=P3(t,e);return n==null?t(B3).some(n=>t(J3,n.getHostId()).includes(e)):'
               b't(J3,n).includes(e)')
CATALOG_3930 = (b'gFn=Kk(Q,(e,{get:t})=>{let n=null;for(let r of t(wZ).entriesByKey.values())'
                b'if(r.sourceKind!==`chatgpt`&&r.threadId===e){if(n!=null&&n!==r.hostId)'
                b'return null;n=r.hostId}return n})')
FILTER_4958 = (b'let n=F3(t,e);return n==null?t(V3).some(n=>t(Y3,n.getHostId()).includes(e)):'
               b't(Y3,n).includes(e)')
CATALOG_4958 = CATALOG_3930.replace(b'gFn=', b'_Fn=')
STATE_4958 = b'uFn={initialized:!1,entries:[],entriesByKey:new Map,entriesByConversationId:new Map}'
# The replacement reads the catalog-enabled atom, so its declaration is required.
ENABLED = b'AT=rf($,!1)'
ENABLED_917 = b'Fw=Go(X,!1)'
ENABLED_3930 = b'uFn=zk(Q,!1)'
ENABLED_4958 = b'dFn=zk(Q,!1)'
# filter, catalog + enabled declaration, (selected host, managers, archived IDs, catalog enabled, catalog host)
VARIANTS = {
    '26.915': (FILTER, ENABLED + b',' + CATALOG, ('RE', 'WE', 'n5n', 'AT', 'QZn')),
    '26.917': (FILTER_917, ENABLED_917 + b',' + CATALOG_917, ('oE', 'pE', 'KRn', 'Fw', 'Ckn')),
    '26.930.3930': (FILTER_3930, ENABLED_3930 + b',' + CATALOG_3930, ('P3', 'B3', 'J3', 'uFn', 'gFn')),
    '26.930.4958': (FILTER_4958, STATE_4958 + b',' + ENABLED_4958 + b',' + CATALOG_4958,
                    ('F3', 'V3', 'Y3', 'dFn', '_Fn')),
}


class MigratedThreadVisibilityTests(unittest.TestCase):
    def test_requires_verified_catalog_and_unique_filter(self):
        for version, (filter_, catalog, _) in VARIANTS.items():
            with self.subTest(version):
                self.assertEqual(renderer_host_identity_patches(filter_), {})
                self.assertEqual(renderer_host_identity_patches(catalog), {})
                with self.assertRaises(ValueError):
                    renderer_host_identity_patches(filter_ + filter_ + catalog)
                patches = renderer_host_identity_patches(filter_ + catalog)
                self.assertEqual(len(patches), 1)
                self.assertEqual(renderer_host_identity_patches(next(iter(patches.values())) + catalog), {})
        self.assertEqual(renderer_host_identity_patches(FILTER + ENABLED + CATALOG_917), {})
        self.assertEqual(renderer_host_identity_patches(FILTER_917 + ENABLED_917 + CATALOG), {})
        with self.assertRaises(ValueError):
            renderer_host_identity_patches(FILTER + ENABLED + CATALOG + FILTER_917 + ENABLED_917 + CATALOG_917)

    def test_requires_the_renderers_own_catalog_enabled_declaration(self):
        for filter_, catalog, enabled in ((FILTER, CATALOG, ENABLED), (FILTER_917, CATALOG_917, ENABLED_917),
                                          (FILTER_3930, CATALOG_3930, ENABLED_3930),
                                          (FILTER_4958, CATALOG_4958, ENABLED_4958)):
            with self.subTest(enabled):
                self.assertEqual(renderer_host_identity_patches(filter_ + catalog), {})
                # Another binding that merely ends with the same name is not it.
                self.assertEqual(renderer_host_identity_patches(filter_ + b'x' + enabled + b',' + catalog), {})
                self.assertEqual(renderer_host_identity_patches(filter_ + enabled + b',' + enabled + b',' + catalog), {})
                self.assertEqual(len(renderer_host_identity_patches(filter_ + b';' + enabled + b',' + catalog)), 1)

    def test_26_917_prefers_catalog_host_before_any_host_fallback(self):
        self.assertEqual(renderer_host_identity_patches(FILTER_917 + ENABLED_917 + CATALOG_917), {FILTER_917: (
            b'let n=oE(t,e)??(t(Fw)?t(Ckn,e):null);return n==null?t(pE).some(n=>t(KRn,n.getHostId())'
            b'.includes(e)):t(KRn,n).includes(e)')})

    def test_26_930_4958_reads_its_own_catalog_enabled_flag(self):
        self.assertEqual(renderer_host_identity_patches(FILTER_4958 + b';' + VARIANTS['26.930.4958'][1]), {FILTER_4958: (
            b'let n=F3(t,e)??(t(dFn)?t(_Fn,e):null);return n==null?t(V3).some(n=>t(Y3,n.getHostId())'
            b'.includes(e)):t(Y3,n).includes(e)')})
        # Without its own flag declaration there is no patch.
        self.assertEqual(renderer_host_identity_patches(FILTER_4958 + b';' + STATE_4958 + b',' + CATALOG_4958), {})
        # 3930's flag name (uFn) is the 4958 catalog state, not a boolean atom: no patch.
        with mock.patch.object(original_sync_bundle, '_HOST_IDENTITY_VARIANTS', (
                (b'F3', b'V3', b'Y3', b'uFn', b'_Fn', b'wZ', b'Kk', b'Q', b'zk'),)):
            self.assertEqual(renderer_host_identity_patches(FILTER_4958 + b';' + VARIANTS['26.930.4958'][1]), {})

    def test_managed_26_930_copies_hold_one_verified_filter(self):
        archives = [(build, archive) for build in ('26.930.3930', '26.930.4958')
                    for archive in managed_archives(build + '.*')]
        if not archives:
            self.skipTest('No managed 26.930.3930 or 26.930.4958 desktop copy on this machine.')
        for build, archive in archives:
            filter_, catalog, _ = VARIANTS[build]
            after, = renderer_host_identity_patches(filter_ + b';' + catalog).values()
            with self.subTest(archive.parent.parent.name):
                shared = read_entries(archive, lambda name: name.startswith('webview/assets/app-shared')
                                      and name.endswith('.js'))
                self.assertEqual(len(shared), 1)
                data, = shared.values()
                # Built before or after this patch: the verified filter once either way.
                self.assertEqual(data.count(filter_) + data.count(after), 1)
                self.assertEqual(renderer_host_identity_patches(data), {filter_: after} if filter_ in data else {})

    @unittest.skipUnless(shutil.which('node'), 'Node.js required for renderer behavior')
    def test_archive_visibility_uses_active_host_without_overriding_explicit_selection(self):
        cases = [
            # selection, catalog enabled, unique active host, archived hosts, hidden
            [None, True, 'local', ['ssh'], False],
            [None, True, 'ssh', ['local'], False],
            ['ssh', True, 'local', ['ssh'], True],
            ['local', True, 'ssh', ['ssh'], False],
            [None, True, 'local', ['local'], True],
            [None, True, 'ssh', ['ssh'], True],
            [None, True, None, ['ssh'], True],
            [None, False, 'local', ['ssh'], True],
            [None, True, None, [], False],
        ]
        program = '''
const cases = CASES;
const MANAGERS = 'managers', ARCHIVED = 'archived', ENABLED = 'enabled', UNIQUE = 'catalogHost';
for (const [selected, enabled, active, archived, expected] of cases) {
  const e = 'same-task-id', SELECTED = () => selected;
  const t = (atom, host) => atom === MANAGERS ? ['local', 'ssh'].map(h => ({getHostId: () => h}))
    : atom === ARCHIVED ? (archived.includes(host) ? [e] : [])
    : atom === ENABLED ? enabled : atom === UNIQUE ? active : undefined;
  const actual = (() => { FILTER })();
  if (actual !== expected) throw Error(JSON.stringify({selected, enabled, active, archived, expected, actual}));
}
console.log(cases.length);
'''.replace('CASES', json.dumps(cases))
        for version, (filter_, catalog, names) in VARIANTS.items():
            with self.subTest(version):
                patched = renderer_host_identity_patches(filter_ + catalog)[filter_].decode()
                source = program
                for token, name in zip(('SELECTED', 'MANAGERS', 'ARCHIVED', 'ENABLED', 'UNIQUE'), names):
                    source = source.replace(token, name)
                result = subprocess.run(['node', '-e', source.replace('FILTER', patched)],
                                        capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout.strip(), str(len(cases)))


if __name__ == '__main__':
    unittest.main()
