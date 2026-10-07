"""Managed desktop registration for the Codex Chrome extension's native host."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core import desktop_bundle as bundle
from manager_core import desktop_chrome_host as chrome
from manager_core.original_sync_bundle import PATCHES, RENDERER_PATCHES

# Exact 26.917 text: the bundled marketplace add (main), the native-host sync
# entry and the v2 writer's entry list (src). Each marker names its module.
RA_917 = (b'(await e.appServerConnection.addMarketplace({source:e.materializedMarketplace.marketplaceRoot},'
          b'...e.beforeSendRequest?[e.beforeSendRequest]:[])).marketplaceName')
G2_917 = b'async function g2(e){let t=[...new Set([...e.extensionIds,...WS(e.nativeHostName)])]'
Y2_917 = b'entries:[...r.entries.filter(t=>!w2(t,e.resource)),a]'
MAIN_917 = (b'async function Ra(e){let t;try{t=' + RA_917 + b'}catch(t){if(Pa.warning(`bundled_plugins_marketplace_add_failed`,'
            b'{}),e.throwOnReconcileFailure)throw t;return null}return t}')
PATH_917 = b'let s=require("node:path");'
SRC_917 = (PATH_917 + b's=e.a(s);var U0=`chrome-native-hosts-v2.json`;' + G2_917 + b',n=z2();await y2(n)}'
           b'async function y2(e){let r=await x2(e),a=b2(e.resource);await B2({' + Y2_917 + b'})}')
REF_ERROR = '--ref is only supported for git marketplace sources'

# 26.930 moved the native-host code to the bootstrap module. Exact 26.930.7945
# anchors: its node:path binding, the sync entry AF and the v2 writer's entry
# list. RF is now the same-install test; the LF beside it compares fields (it
# was IF in 4958) and is no anchor. The connecting text is shortened.
PATH_930 = b'let g=require("node:path");'
AF_7945 = b'async function AF(e){let t=[...new Set([...e.extensionIds,...r.Wr(e.nativeHostName)])]'
RF_7945 = b'entries:[...r.entries.filter(t=>!RF(t,e.resource)),a]'
BOOT_7945 = (PATH_930 + b'g=e.a(g);var rF=`chrome-native-hosts-v2.json`,iF=[0,100,200,400,800];' + AF_7945 +
             b',n=$F()}async function write(t,e,r,i){let a=PF(i!=null&&LF(i,e.resource)?i:e.resource);'
             b'await eI(t,Buffer.from(`${JSON.stringify({schemaVersion:aF,' + RF_7945 +
             b'.sort((e,t)=>VF(e).localeCompare(VF(t)))},null,2)}\\n`),n)}')
# The same module in 26.930.4958, which named these helpers kF, LF and IF.
KF_4958 = AF_7945.replace(b'AF(', b'kF(')
LF_4958 = RF_7945.replace(b'RF(', b'LF(')
BOOT_4958 = BOOT_7945.replace(b'LF(i,', b'IF(i,').replace(AF_7945, KF_4958).replace(RF_7945, LF_4958)


def after(before):
    replacement, = [patched for _, anchor, patched in chrome._PATCHES if anchor == before]
    return replacement


def build(path, chunks):
    tree, offset = {'files': {}}, 0
    for name, data in chunks:
        node = tree
        parts = name.split('/')
        for part in parts[:-1]:
            node = node['files'].setdefault(part, {'files': {}})
        node['files'][parts[-1]] = {'offset': str(offset), 'size': len(data)}
        offset += len(data)
    header = json.dumps(tree).encode()
    payload = struct.pack('<I', len(header)) + header + b'\0' * (-len(header) % 4)
    path.write_bytes(struct.pack('<III', 4, len(payload) + 4, len(payload)) + payload + b''.join(d for _, d in chunks))


def entries(path):
    with Path(path).open('rb') as stream:
        header, base = bundle.read_header(stream)
        found = {}
        for name, item in bundle._entries(header):
            stream.seek(base + int(item['offset']))
            found[name] = (item, stream.read(item['size']))
    return found


def managed_archive(path, main=MAIN_917, src=SRC_917):
    build(path, [
        ('.vite/build/main.js', b'function s9(){' + bundle._ORIGINAL + b'return "posix";}' + main),
        ('.vite/build/notifications.js', bundle._NOTIFICATION_CLICK + b'originalCallback();})' +
         bundle._NOTIFICATION_SHOW + bundle._WINDOW_MESSAGE),
        ('.vite/build/browser-runtime.js', b'Qr({' + bundle._BROWSER_RUNTIME + b');'),
        ('.vite/build/context.js', b';'.join(PATCHES)),
        ('.vite/build/src-fixture.js', src),
        ('webview/assets/app-initial-fixture.js', bundle._CONTEXT_RENDERER + b';' + b';'.join(RENDERER_PATCHES))])


class PatchTableTests(unittest.TestCase):
    def test_each_917_anchor_is_one_patch_kind(self):
        found = {**chrome.patches_for(MAIN_917), **chrome.patches_for(SRC_917)}
        self.assertEqual({kind for kind, _ in found.values()},
                         {'marketplace_add', 'native_host_gate', 'native_host_exclusive'})
        self.assertEqual(set(found), {RA_917, G2_917, Y2_917})
        for before, (_, replacement) in found.items():
            self.assertNotIn(before, replacement)

    def test_each_930_bootstrap_is_patched_with_its_own_names(self):
        for label, data, gate, exclusive in (('4958', BOOT_4958, KF_4958, LF_4958),
                                             ('7945', BOOT_7945, AF_7945, RF_7945)):
            with self.subTest(label):
                found = chrome.patches_for(data)
                self.assertEqual(found, {gate: ('native_host_gate', after(gate)),
                                         exclusive: ('native_host_exclusive', after(exclusive))})
                plan = chrome.Plan()
                plan.scan('bootstrap.js', 'item', data)
                changed = {}
                self.assertEqual(plan.apply(changed), ['native_host_exclusive', 'native_host_gate'])
                self.assertEqual(changed['bootstrap.js'],
                                 ('item', data.replace(gate, after(gate)).replace(exclusive, after(exclusive))))
                # The 26.930 filter calls the module's node:path binding g.
                self.assertIn(b'(0,g.resolve)', after(exclusive))

    def test_renamed_930_helpers_fail_closed(self):
        # 7945 still calls LF beside the entry list, as its field comparison:
        # the 4958 names must not match there.
        self.assertIn(b'LF(i,e.resource)', BOOT_7945)
        self.assertNotIn(LF_4958, BOOT_7945)
        for label, data in (('gate', BOOT_7945.replace(b'function AF(', b'function xF(')),
                            ('entry list', BOOT_7945.replace(b'!RF(t,', b'!xF(t,'))):
            with self.subTest(label):
                plan = chrome.Plan()
                plan.scan('bootstrap.js', {}, data)
                with self.assertRaisesRegex(ValueError, 'Chrome'):
                    plan.apply({})

    def test_duplicate_anchor_is_ambiguous(self):
        with self.assertRaises(ValueError):
            chrome.patches_for(SRC_917 + b';' + G2_917)

    def test_exclusive_filter_needs_its_path_binding(self):
        # The replacement calls the module's node:path binding, which the
        # anchor itself does not name; another binding must not be patched.
        for label, data in (('missing', SRC_917.replace(PATH_917, b'let c=require("node:path");')),
                            ('twice', SRC_917 + PATH_917)):
            with self.subTest(label), self.assertRaisesRegex(ValueError, 'path binding'):
                chrome.patches_for(data)

    def test_marker_without_its_anchor_fails_closed(self):
        for label, data in (('main', MAIN_917.replace(RA_917, RA_917.replace(b'source:', b'src:'))),
                            ('src', SRC_917.replace(Y2_917, Y2_917.replace(b'w2(', b'q2(')))):
            with self.subTest(label):
                plan = chrome.Plan()
                plan.scan('module.js', {}, data)
                with self.assertRaisesRegex(ValueError, 'Chrome'):
                    plan.apply({})

    def test_build_without_the_code_is_left_alone(self):
        plan = chrome.Plan()
        plan.scan('module.js', {}, b'function unrelated(){}')
        changed = {}
        self.assertEqual(plan.apply(changed), [])
        self.assertEqual(changed, {})

    def test_already_patched_module_is_not_patched_twice(self):
        patched = SRC_917.replace(G2_917, after(G2_917)).replace(Y2_917, after(Y2_917))
        plan = chrome.Plan()
        plan.scan('src.js', {}, patched)
        changed = {}
        self.assertEqual(plan.apply(changed), [])
        self.assertEqual(changed, {})

    def test_apply_builds_on_earlier_changes_of_the_same_module(self):
        plan = chrome.Plan()
        plan.scan('main.js', 'item', MAIN_917)
        changed = {'main.js': ('item', b'/*adapter*/\n' + MAIN_917)}
        self.assertEqual(plan.apply(changed), ['marketplace_add'])
        self.assertEqual(changed['main.js'], ('item', b'/*adapter*/\n' + MAIN_917.replace(RA_917, after(RA_917))))

    def test_same_anchor_in_two_modules_is_ambiguous(self):
        plan = chrome.Plan()
        plan.scan('src-a.js', 'a', SRC_917)
        plan.scan('src-b.js', 'b', SRC_917)
        changed = {}
        with self.assertRaises(ValueError):
            plan.apply(changed)
        self.assertEqual(changed, {})

    def test_earlier_change_that_duplicates_or_drops_the_anchor_fails_closed(self):
        for label, earlier in (('duplicated', MAIN_917 + b';' + RA_917), ('dropped', b'/*rewritten*/')):
            with self.subTest(label):
                plan = chrome.Plan()
                plan.scan('main.js', 'item', MAIN_917)
                with self.assertRaises(ValueError):
                    plan.apply({'main.js': ('item', earlier)})


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_managed_copy_gets_all_three_changes_with_fresh_integrity(self):
        source, target = self.root / 'app.asar', self.root / 'patched.asar'
        managed_archive(source)
        result = bundle.patch_archive(source, target)
        self.assertEqual(result['chrome_native_host'], ['marketplace_add', 'native_host_exclusive', 'native_host_gate'])
        found = entries(target)
        item, main = found['.vite/build/main.js']
        self.assertEqual(main.count(after(RA_917)), 1)
        self.assertNotIn(RA_917, main)
        self.assertIn(bundle._REPLACEMENT, main)  # Earlier pipe change kept.
        self.assertEqual(item['integrity']['hash'], hashlib.sha256(main).hexdigest())
        item, src = found['.vite/build/src-fixture.js']
        self.assertEqual(src, SRC_917.replace(G2_917, after(G2_917)).replace(Y2_917, after(Y2_917)))
        self.assertEqual(item['integrity']['hash'], hashlib.sha256(src).hexdigest())

    def test_26_930_7945_bootstrap_gets_both_native_host_changes(self):
        source, target = self.root / 'app.asar', self.root / 'patched.asar'
        managed_archive(source, src=BOOT_7945)
        result = bundle.patch_archive(source, target)
        self.assertEqual(result['chrome_native_host'], ['marketplace_add', 'native_host_exclusive', 'native_host_gate'])
        item, src = entries(target)['.vite/build/src-fixture.js']
        self.assertEqual(src, BOOT_7945.replace(AF_7945, after(AF_7945)).replace(RF_7945, after(RF_7945)))
        self.assertEqual(item['integrity']['hash'], hashlib.sha256(src).hexdigest())

    def test_unknown_native_host_shape_blocks_publication(self):
        source, target = self.root / 'app.asar', self.root / 'patched.asar'
        managed_archive(source, src=SRC_917.replace(b'WS(', b'XS('))
        with self.assertRaises(ValueError):
            bundle.patch_archive(source, target)
        self.assertFalse(target.exists())


@unittest.skipUnless(shutil.which('node'), 'Node.js required for desktop behavior')
class DesktopBehaviorTests(unittest.TestCase):
    def node(self, program, cwd=None, **environment):
        env = {key: value for key, value in os.environ.items()
               if key not in ('CODEX_MANAGER_ROOT', chrome.ENV)}
        env.update(environment)
        result = subprocess.run(['node', '-e', program], capture_output=True, text=True, check=True, env=env, cwd=cwd)
        return json.loads(result.stdout)

    def test_bundled_marketplace_add_retries_a_hash_root_only_after_the_ref_error(self):
        program = r'''
const add = new Function('return async function(e){return ' + AFTER + '}')();
const cases = CASES, results = {};
(async () => {
  for (const [label, [root, mode, hook]] of Object.entries(cases)) {
    const calls = [];
    const connection = {addMarketplace(request, ...rest) {
      calls.push([request.source, rest.length]);
      if (mode === 'other') throw new Error('boom');
      if ((mode === 'ref' && !request.source.endsWith('#')) || mode === 'always-ref')
        throw new Error(REF);
      return Promise.resolve({marketplaceName: 'openai-bundled'});
    }};
    try {
      const name = await add({appServerConnection: connection, beforeSendRequest: hook ? () => {} : undefined,
                              materializedMarketplace: {marketplaceRoot: root}});
      results[label] = {name, calls};
    } catch (error) { results[label] = {error: error.message, calls}; }
  }
  console.log(JSON.stringify(results));
})().catch(error => { console.error(error); process.exitCode = 1; });
'''.replace('AFTER', json.dumps(after(RA_917).decode())).replace('REF', json.dumps(REF_ERROR)).replace('CASES', json.dumps({
            'plain': ['C:\\home\\m', 'ok', False], 'hash-accepted': ['D:\\#P\\m', 'ok', True],
            'hash-ref': ['D:\\#P\\m', 'ref', True], 'plain-ref': ['C:\\home\\m', 'ref', False],
            'hash-other': ['D:\\#P\\m', 'other', False], 'hash-ref-twice': ['D:\\#P\\m', 'always-ref', False]}))
        self.assertEqual(self.node(program), {
            'plain': {'name': 'openai-bundled', 'calls': [['C:\\home\\m', 0]]},
            'hash-accepted': {'name': 'openai-bundled', 'calls': [['D:\\#P\\m', 1]]},
            'hash-ref': {'name': 'openai-bundled', 'calls': [['D:\\#P\\m', 1], ['D:\\#P\\m#', 1]]},
            'plain-ref': {'error': REF_ERROR, 'calls': [['C:\\home\\m', 0]]},
            'hash-other': {'error': 'boom', 'calls': [['D:\\#P\\m', 0]]},
            'hash-ref-twice': {'error': REF_ERROR, 'calls': [['D:\\#P\\m', 0], ['D:\\#P\\m#', 0]]}})

    def test_only_the_marked_desktop_syncs_its_native_host(self):
        # 26.917 calls its extension-id helper WS; 26.930 calls r.Wr of a required module.
        for label, gate, binding, helper in (('26.917', G2_917, 'WS', "() => ['store-id']"),
                                             ('26.930.7945', AF_7945, 'r', "({Wr: () => ['store-id']})")):
            program = r'''
const log = [];
const sync = new Function(BINDING, 'log', 'return ' + GATE + ';log.push(t)}')(HELPER, log);
(async () => {
  await sync({extensionIds: ['a', 'store-id'], nativeHostName: 'com.openai.codexextension'});
  console.log(JSON.stringify(log));
})().catch(error => { console.error(error); process.exitCode = 1; });
'''.replace('GATE', json.dumps(after(gate).decode())).replace('BINDING', json.dumps(binding)).replace('HELPER', helper)
            with self.subTest(label):
                self.assertEqual(self.node(program), [])
                self.assertEqual(self.node(program, **{chrome.ENV: '0'}), [])
                self.assertEqual(self.node(program, **{chrome.ENV: '1'}), [['a', 'store-id']])

    def test_registration_replaces_entries_of_every_managed_copy_of_this_root(self):
        for label, anchor, path, same_install in (('26.917', Y2_917, 's', 'w2'), ('26.930.7945', RF_7945, 'g', 'RF')):
            with self.subTest(label):
                self.check_registration(anchor, path, same_install)

    def check_registration(self, anchor, path, same_install):
        program = r'''
const s = require('path').win32, w2 = (t, e) => t?.entryId === e.entryId;
const write = new Function(PATH, SAME_INSTALL, 'return (r,e,a)=>({' + FILTER + '})')(s, w2);
const at = resourcesPath => ({entryId: resourcesPath, paths: {resourcesPath}});
const entries = [
  at('C:\\Program Files\\WindowsApps\\OpenAI.Codex_1_x64__id\\app\\resources'),
  at('D:\\#Root\\mgr\\artifacts\\managed-desktop\\26.917.1-aaaa\\resources'),
  at('d:\\#root\\MGR\\artifacts\\Managed-Desktop\\26.915.1-bbbb\\resources'),
  at('D:\\#Root\\mgr\\artifacts\\managed-desktop-old\\x\\resources'),
  at('E:\\other\\artifacts\\managed-desktop\\26.917.1-cccc\\resources'),
  {entryId: 'no-paths'}, 'not-an-object', {entryId: 'mine', paths: {resourcesPath: 'C:\\elsewhere'}}];
const result = write({entries}, {resource: {entryId: 'mine'}}, {entryId: 'mine', current: true}).entries;
console.log(JSON.stringify(result.map(entry => typeof entry === 'string' ? entry : entry.entryId + (entry.current ? '*' : ''))));
'''.replace('FILTER', json.dumps(after(anchor).decode())).replace('PATH', json.dumps(path)).replace(
            'SAME_INSTALL', json.dumps(same_install))
        official = 'C:\\Program Files\\WindowsApps\\OpenAI.Codex_1_x64__id\\app\\resources'
        sibling = 'D:\\#Root\\mgr\\artifacts\\managed-desktop-old\\x\\resources'
        other_root = 'E:\\other\\artifacts\\managed-desktop\\26.917.1-cccc\\resources'
        self.assertEqual(self.node(program, CODEX_MANAGER_ROOT='D:\\#Root\\mgr\\'),
                         [official, sibling, other_root, 'no-paths', 'not-an-object', 'mine*'])
        self.assertEqual(self.node(program), [
            official, 'D:\\#Root\\mgr\\artifacts\\managed-desktop\\26.917.1-aaaa\\resources',
            'd:\\#root\\MGR\\artifacts\\Managed-Desktop\\26.915.1-bbbb\\resources',
            sibling, other_root, 'no-paths', 'not-an-object', 'mine*'])

    def test_empty_manager_root_never_resolves_against_the_working_directory(self):
        program = r'''
const s = require('path').win32, w2 = (t, e) => t?.entryId === e.entryId;
const write = new Function('s', 'w2', 'return (r,e,a)=>({' + FILTER + '})')(s, w2);
const here = s.join(process.cwd(), 'artifacts', 'managed-desktop', 'x', 'resources');
const result = write({entries: [{entryId: 'here', paths: {resourcesPath: here}}]}, {resource: {entryId: 'mine'}},
                     {entryId: 'mine'}).entries;
console.log(JSON.stringify(result.map(entry => entry.entryId)));
'''.replace('FILTER', json.dumps(after(Y2_917).decode()))
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(self.node(program, cwd=temporary, CODEX_MANAGER_ROOT=temporary), ['mine'])
            self.assertEqual(self.node(program, cwd=temporary, CODEX_MANAGER_ROOT=''), ['here', 'mine'])


class OwnerTests(unittest.TestCase):
    def test_first_own_login_profile_in_list_order_owns_chrome(self):
        profiles = [dict(id='api', auth_mode='external'), dict(id='borrowed', auth_mode='source'),
                    dict(id='removed', auth_mode='native', removed_at='x'),
                    dict(id='pending', auth_mode='native', native_login_pending=True),
                    dict(id='viewer', auth_mode='native', view_only=True),
                    dict(id='missing', auth_mode='native', account_missing=True),
                    'corrupt', dict(id='first', auth_mode='native'), dict(id='second', auth_mode='native')]
        self.assertEqual(chrome.owner(profiles), 'first')
        self.assertIsNone(chrome.owner(profiles[:7]))
        self.assertIsNone(chrome.owner(None))

    def test_only_the_owner_launch_environment_is_marked(self):
        from manager_core.instances import Instances
        from manager_core.store import Store
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = Store(root)
            first, second, api = (store.add_profile(alias)['id'] for alias in ('01', '02', '03'))
            store.mutate(lambda data: [profile.update(auth_mode='external' if profile['id'] == api else 'native',
                                                      runtime_channel='packaged') for profile in data['profiles']])
            instances = Instances(root, store, None)
            runtime = root / 'codex.exe'
            runtime.write_bytes(b'fixture')
            with patch.object(Instances, 'installed_app', return_value={}), \
                    patch('manager_core.login_probe.verification_runtime', return_value=runtime), \
                    patch.dict(os.environ, {chrome.ENV: '1'}):
                marked = {pid: instances.environment(store.profile(pid)).get(chrome.ENV) for pid in (first, second, api)}
                self.assertEqual(marked, {first: '1', second: None, api: None})
                store.move_profile(second, first, 'before')
                self.assertEqual(instances.environment(store.profile(second)).get(chrome.ENV), '1')
                self.assertIsNone(instances.environment(store.profile(first)).get(chrome.ENV))


if __name__ == '__main__':
    unittest.main()
