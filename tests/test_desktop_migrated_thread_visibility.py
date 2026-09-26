"""An archived source must not hide the active destination with the same ID."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.original_sync_bundle import renderer_host_identity_patches

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
# The replacement reads the catalog-enabled atom, so its declaration is required.
ENABLED = b'AT=rf($,!1)'
ENABLED_917 = b'Fw=Go(X,!1)'
# filter, catalog + enabled declaration, (selected host, managers, archived IDs, catalog enabled, catalog host)
VARIANTS = {
    '26.915': (FILTER, ENABLED + b',' + CATALOG, ('RE', 'WE', 'n5n', 'AT', 'QZn')),
    '26.917': (FILTER_917, ENABLED_917 + b',' + CATALOG_917, ('oE', 'pE', 'KRn', 'Fw', 'Ckn')),
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
        for filter_, catalog, enabled in ((FILTER, CATALOG, ENABLED), (FILTER_917, CATALOG_917, ENABLED_917)):
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
