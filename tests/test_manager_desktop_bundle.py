import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core import desktop_bundle as bundle
from manager_core.original_sync_bundle import PATCHES, RENDERER_PATCHES


def archive(path, source=None, notification=None, composer=None, links=b'', version=None):
    body = b'function s9(){' + (source if source is not None else bundle._ORIGINAL) + b'return "posix";}' + links
    notice = notification if notification is not None else (bundle._NOTIFICATION_CLICK + b'originalCallback();})' +
        bundle._NOTIFICATION_SHOW + bundle._WINDOW_MESSAGE)
    chunks = [('.vite/build/before.bin', b'FIRST'), ('.vite/build/main.js', body),
              ('.vite/build/notifications.js', notice), ('.vite/build/after.bin', b'AFTER'),
              ('.vite/build/browser-runtime.js', b'Qr({' + bundle._BROWSER_RUNTIME + b');'),
              ('.vite/build/context.js', b'' +
               (b'' if source is not None and all(pattern in source for pattern in PATCHES) else b';'.join(PATCHES))),
              ('webview/assets/app-initial-fixture.js', bundle._CONTEXT_RENDERER + b';' + b';'.join(RENDERER_PATCHES))]
    if composer is not None:
        chunks.append(('webview/assets/app-primary-fixture.js', composer))
    if version is not None:
        chunks.append(('package.json', json.dumps({'version': version}).encode()))
    tree = {'files': {}}
    offset = 0
    for name, data in chunks:
        node = tree
        parts = name.split('/')
        for part in parts[:-1]: node = node['files'].setdefault(part, {'files': {}})
        entry = {'offset': str(offset), 'size': len(data)}
        if name.endswith('/main.js'): entry['integrity'] = {'blockSize': 32}
        node['files'][parts[-1]] = entry
        offset += len(data)
    header = json.dumps(tree).encode()
    payload = struct.pack('<I', len(header)) + header + b'\0' * (-len(header) % 4)
    path.write_bytes(struct.pack('<III', 4, len(payload) + 4, len(payload)) + payload + b''.join(data for _, data in chunks))
    return body


def entry(path, name):
    with Path(path).open('rb') as stream:
        header, base = bundle.read_header(stream)
        item = dict(bundle._entries(header))[name]
        stream.seek(base + int(item['offset']))
        return stream.read(item['size'])


# 26.917 renamed the toast l -> d (l became the sound setting) and moved show()
# into a closure that also plays the bundled sound after optional staging.
CLICK_917 = bundle._NOTIFICATION_CLICK.replace(b'l.on', b'd.on')
NOTICES = {'26.915': bundle._NOTIFICATION_CLICK + b'originalCallback();})' + bundle._NOTIFICATION_SHOW + bundle._WINDOW_MESSAGE,
           '26.917': CLICK_917 + b'originalCallback();})' + bundle._NOTIFICATION_STAGED_SHOW + b'}' + bundle._WINDOW_MESSAGE}

# 26.930.7945 main bundle, verbatim (3930 and 4958 are identical here): the
# deep-link thread lookup of navigateToRoute and the end of runMainAppStartup.
LOOKUP_930 = b'readThread:t.kind===`localConversation`?(e,t)=>c(r??`local`).readThread(e,t):void 0'
FLUSH_930 = b'await We.deepLinks.flushPendingDeepLinks(),I(`pending deep links flushed`,B),I(`startup complete`,e)}'
# 26.917 keeps the flush (bound to Le, M, L) and has no such lookup wrapper.
FLUSH_917 = b'await Le.deepLinks.flushPendingDeepLinks(),M(`pending deep links flushed`,L),M(`startup complete`,e)}'
LINKS_930 = b'let k={navigateToRoute:async(e,t)=>XJe({' + LOOKUP_930 + b'})};async function NQe(){' + FLUSH_930
VERSION_7945 = '26.930.61225'  # package.json version of the 26.930.7945 desktop
# A starting desktop with a queued link for an SSH host it has not registered
# yet: the shape of navigateToRoute, its lookup (XJe) and the connection
# registry, with the anchors above verbatim. Prints the startup outcome.
STARTUP_PROGRAM = b'''const out={phases:[],navigated:[],warnings:[]};
const y7={warning:m=>out.warnings.push(m)};
const registry={connections:new Map([[`local`,{readThread:async()=>({thread:{}})}]]),
 getMaybeConnection(e){return this.connections.get(e)??null},
 getConnection(e){let t=this.getMaybeConnection(e);if(!t)throw Error(`Connection for host ID ${e} not found`);return t}};
const c=e=>registry.getConnection(e),D=(e,t)=>out.navigated.push(t);
async function XJe({window:e,route:t,readThread:s,navigateToRoute:u}){switch(t.kind){case`other`:throw Error(`unrelated link failure`);
case`localConversation`:{if(s==null||await s(t.conversationId,{includeTurns:!1}).catch(e=>(y7.warning(`local_conversation_deep_link_lookup_failed`,{sensitive:{error:e}}),null))==null)return;u(e,`/local/${t.conversationId}`);return}}}
const navigateToRoute=async(e,t)=>{let r=t.hostId;return XJe({window:e,route:t,LOOKUP,navigateToRoute:D})};
const pending=[JSON.parse(process.argv[2])];
const We={deepLinks:{flushPendingDeepLinks:async()=>{for(const t of pending.splice(0))await navigateToRoute({},t)}}};
async function NQe(){let e=0,B=0;const I=t=>out.phases.push(t);B=Date.now(),FLUSH
NQe().then(()=>console.log(JSON.stringify({started:true,...out})),t=>console.log(JSON.stringify({started:false,error:t.message,...out})));
'''.replace(b'LOOKUP', LOOKUP_930).replace(b'FLUSH', FLUSH_930)


class DesktopBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'installed'
        (self.source / 'resources').mkdir(parents=True)
        (self.source / 'ChatGPT.exe').write_bytes(b'unmodified-executable')
        (self.source / 'chrome.dll').write_bytes(b'dL7pKGdnNz796PbbjQWNKmHXBZaB9tsX\x01\x09101100011')
        self.path = self.source / 'resources/app.asar'
        self.body = archive(self.path)
        self.app = dict(executable=str(self.source / 'ChatGPT.exe'), Version='26.908.4834.0')

    def test_rebuilt_archive_preserves_other_entries_and_recomputes_integrity(self):
        target = self.root / 'patched.asar'
        bundle.patch_archive(self.path, target)
        with target.open('rb') as stream:
            header, base = bundle.read_header(stream)
            entries = dict(bundle._entries(header))
            data = {}
            for name, item in entries.items():
                stream.seek(base + int(item['offset']))
                data[name] = stream.read(item['size'])
        self.assertEqual(data['.vite/build/before.bin'], b'FIRST')
        self.assertEqual(data['.vite/build/after.bin'], b'AFTER')
        sync_module = next(value for value in data.values() if b'const files=globalThis.__codexSignalFiles.create' in value)
        self.assertLess(sync_module.index(b'globalThis.__codexSignalFiles={create'),
                        sync_module.index(b'const files=globalThis.__codexSignalFiles.create'))
        self.assertIn(bundle._NOTIFICATION_REPLACEMENT, data['.vite/build/notifications.js'])
        self.assertIn(b'originalCallback();', data['.vite/build/notifications.js'])
        self.assertIn(bundle._BROWSER_RUNTIME_REPLACEMENT, data['.vite/build/browser-runtime.js'])
        changed = data['.vite/build/main.js']
        adapter = b'\n'.join(Path(bundle.__file__).with_name(name).read_bytes() for name in
            ('desktop_network_policy.cjs', 'desktop_window_host.cjs', 'desktop_window_health.cjs'))
        self.assertEqual(changed, adapter + b'\n' + self.body.replace(bundle._ORIGINAL, bundle._REPLACEMENT))
        integrity = entries['.vite/build/main.js']['integrity']
        self.assertEqual(integrity['hash'], hashlib.sha256(changed).hexdigest())
        self.assertEqual(integrity['blocks'], [hashlib.sha256(changed[i:i+32]).hexdigest() for i in range(0, len(changed), 32)])
        notice_integrity = entries['.vite/build/notifications.js']['integrity']
        self.assertEqual(notice_integrity['hash'], hashlib.sha256(data['.vite/build/notifications.js']).hexdigest())

    def test_composer_effort_patch_is_published_with_verified_archive_offsets(self):
        from test_manager_desktop_reasoning import DesktopReasoningTests
        composer = DesktopReasoningTests().composer()
        archive(self.path, composer=composer)
        original = self.path.read_bytes()
        target = self.root / 'patched.asar'
        bundle.patch_archive(self.path, target)
        patched = entry(target, 'webview/assets/app-primary-fixture.js')
        self.assertIn(b'defaultMessage:`Ultracode`', patched)
        self.assertIn(b'(fne(e)||e===`ultracode`)&&t.some', patched)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(entry(target, '.vite/build/after.bin'), b'AFTER')

    def test_unknown_renderer_does_not_publish_half_working_sync(self):
        self.path.write_bytes(self.path.read_bytes().replace(
            b'this.requestClient=n;let y=this.settings.restricted;',
            b'this.requestClient=n;let z=this.settings.restricted;'))
        with self.assertRaises(ValueError):bundle.patch_archive(self.path,self.root/'patched.asar')
        self.assertFalse((self.root/'patched.asar').exists())

    def test_verified_renderer_binding_from_new_package_keeps_task_context_hook(self):
        # Same route/effect arguments, only the bundled React identifier changed.
        self.path.write_bytes(self.path.read_bytes().replace(b't7.', b'L9.'))
        target = self.root / 'patched.asar'
        bundle.patch_archive(self.path, target)
        with target.open('rb') as stream:
            header, base = bundle.read_header(stream)
            item = dict(bundle._entries(header))['webview/assets/app-initial-fixture.js']
            stream.seek(base + int(item['offset']))
            content = stream.read(item['size'])
        self.assertIn(bundle._CONTEXT_RENDERER_REPLACEMENT.replace(b't7.', b'L9.'), content)
        self.assertNotIn(bundle._CONTEXT_RENDERER_REPLACEMENT, content)

    def test_unknown_notification_callback_fails_before_publication(self):
        archive(self.path, notification=b'changed notification callback')
        with self.assertRaises(ValueError): bundle.patch_archive(self.path, self.root / 'patched.asar')
        self.assertFalse((self.root / 'patched.asar').exists())

    def test_each_verified_notification_shape_gets_both_hooks_once(self):
        for version, notice in NOTICES.items():
            with self.subTest(version):
                archive(self.path, notification=notice)
                target = self.root / (version + '.asar')
                bundle.patch_archive(self.path, target)
                content = entry(target, '.vite/build/notifications.js')
                for variants in (bundle._NOTIFICATION_CLICK_VARIANTS, bundle._NOTIFICATION_SHOW_VARIANTS):
                    (before,) = [key for key in variants if key in notice]
                    self.assertEqual(content.count(variants[before]), 1)
                    for other in variants:
                        if other != before: self.assertNotIn(variants[other], content)
                self.assertEqual(content.count(b'globalThis.__codexManagerNotificationClick?.(e,t);'), 1)
                self.assertEqual(content.count(b'globalThis.__codexManagerNotificationShow(e,t,'), 1)
                self.assertIn(b'originalCallback();', content)

    def test_notification_shapes_fail_closed_before_publication(self):
        for variants in (bundle._NOTIFICATION_CLICK_VARIANTS, bundle._NOTIFICATION_SHOW_VARIANTS):
            keys = list(variants)
            self.assertEqual(bundle._matching_variant(keys[1], variants), keys[1])
            with self.assertRaises(ValueError): bundle._matching_variant(b';'.join(keys), variants)
            with self.assertRaises(ValueError): bundle._matching_variant(keys[1] * 2, variants)
        show, tail = bundle._NOTIFICATION_STAGED_SHOW, b'originalCallback();})'
        cases = {
            'show missing': CLICK_917 + tail + bundle._WINDOW_MESSAGE,
            'both show shapes': CLICK_917 + tail + show + bundle._NOTIFICATION_SHOW + bundle._WINDOW_MESSAGE,
            'repeated show': CLICK_917 + tail + show + show + bundle._WINDOW_MESSAGE,
            'both click shapes': CLICK_917 + bundle._NOTIFICATION_CLICK + tail + show + bundle._WINDOW_MESSAGE,
            'repeated click': CLICK_917 + CLICK_917 + tail + show + bundle._WINDOW_MESSAGE,
            'changed destroyed guard': CLICK_917 + tail + show.replace(b'if(t.isDestroyed()){this.removeNotification(e.id);return}', b'') + bundle._WINDOW_MESSAGE,
            'changed sound choice': CLICK_917 + tail + show.replace(b'l!==`none`&&', b'') + bundle._WINDOW_MESSAGE,
            'window message missing': CLICK_917 + tail + show,
        }
        for label, notice in cases.items():
            with self.subTest(label):
                archive(self.path, notification=notice)
                with self.assertRaises(ValueError): bundle.patch_archive(self.path, self.root / 'patched.asar')
                self.assertFalse((self.root / 'patched.asar').exists())

    @unittest.skipUnless(shutil.which('node'), 'Node.js required for notification behavior')
    def test_accepted_workspace_toast_replaces_whole_native_presentation_once(self):
        # present(e=notice, t=webContents, toast, sound) holds each version's exact native tail.
        shapes = {'26.915': (b'present(e,t,l){', bundle._NOTIFICATION_SHOW, b'{}'),
                  '26.917': (b'present(e,t,d,l){this.notifications.set(e.id,{notification:d});', bundle._NOTIFICATION_STAGED_SHOW,
                             b"log.push('sound:'+e)}")}
        sources = {version: {'native': (head + key + tail).decode(),
                             'patched': (head + bundle._NOTIFICATION_SHOW_VARIANTS[key] + tail).decode()}
                   for version, (head, key, tail) in shapes.items()}
        scenarios = {'26.915': [{'platform': 'win32'}],
                     '26.917': [{'platform': 'win32', 'sound': 'default'}, {'platform': 'win32', 'sound': 'classic'},
                                {'platform': 'win32', 'sound': 'none'}, {'platform': 'win32', 'sound': {'fileName': 'custom.aiff'}},
                                {'platform': 'darwin', 'sound': 'default', 'staged': True},
                                {'platform': 'darwin', 'sound': 'default', 'staged': True, 'after': 'replace'},
                                {'platform': 'darwin', 'sound': 'classic', 'staged': True, 'after': 'destroy'}]}
        program = r'''
const sources = SOURCES, scenarios = SCENARIOS, results = {};
(async () => {
  for (const [version, shape] of Object.entries(sources)) for (const [index, scenario] of scenarios[version].entries())
    for (const mode of ['native', 'patched', 'accept', 'decline']) {
      const log = [], calls = [];
      const Manager = new Function('log', 'return class{constructor(o){this.options={platform:o.platform};' +
        'this.staged=o.staged?Promise.resolve():void 0;this.notifications=new Map}' +
        "emitCompletedThreadsChanged(){log.push('emit')}stageNotificationSoundIfNeeded(l){log.push('stage:'+l);return this.staged}" +
        "removeNotification(id){log.push('remove:'+id)}" + shape[mode === 'native' ? 'native' : 'patched'] + '}')(log);
      const e = {id: 'n1'}, t = {isDestroyed: () => scenario.after === 'destroy'}, toast = {show: () => log.push('show')};
      if (mode === 'accept' || mode === 'decline') globalThis.__codexManagerNotificationShow = async (notice, contents, fallback) => {
        calls.push(notice === e && contents === t); if (mode === 'decline') fallback(); };
      else delete globalThis.__codexManagerNotificationShow;
      const manager = new Manager(scenario);
      manager.present(e, t, toast, scenario.sound);
      if (scenario.after === 'replace') manager.notifications.set('n1', {notification: {}});
      await new Promise(resolve => setImmediate(resolve));
      results[version + '/' + index + '/' + mode] = {log, calls};
    }
  console.log(JSON.stringify(results));
})().catch(error => { console.error(error); process.exitCode = 1; });
'''.replace('SOURCES', json.dumps(sources)).replace('SCENARIOS', json.dumps(scenarios))
        result = subprocess.run(['node', '-e', program], capture_output=True, text=True, check=True)
        results = json.loads(result.stdout)
        expected_native = {'26.915/0': ['emit', 'show'],
            '26.917/0': ['emit', 'stage:default', 'show', 'sound:default'], '26.917/1': ['emit', 'stage:classic', 'show', 'sound:classic'],
            '26.917/2': ['emit', 'show'], '26.917/3': ['emit', 'show', 'sound:default'], '26.917/4': ['emit', 'stage:default', 'show'],
            '26.917/5': ['emit', 'stage:default'], '26.917/6': ['emit', 'stage:classic', 'remove:n1']}
        for case, native in expected_native.items():
            with self.subTest(case):
                self.assertEqual(results[case + '/native'], {'log': native, 'calls': []})
                self.assertEqual(results[case + '/patched'], {'log': native, 'calls': []})
                offered = [True] if 'show' in native else []
                self.assertEqual(results[case + '/decline'], {'log': native, 'calls': offered})
                self.assertEqual(results[case + '/accept'], {'calls': offered,
                    'log': [step for step in native if step != 'show' and not step.startswith('sound:')]})

    @unittest.skipUnless(shutil.which('node'), 'Node.js required for notification behavior')
    def test_late_decline_never_shows_a_replaced_or_destroyed_26_917_toast(self):
        # The hook answers after its pipe wait; by then the toast may be stale.
        source = (b'present(e,t,d,l){this.notifications.set(e.id,{notification:d});'
                  + bundle._NOTIFICATION_SHOW_VARIANTS[bundle._NOTIFICATION_STAGED_SHOW] + b"log.push('sound:'+e)}").decode()
        program = r'''
const results = {};
(async () => {
  for (const change of ['none', 'replace', 'destroy']) {
    const log = [];
    const Manager = new Function('log', 'return class{constructor(){this.options={platform:"win32"};this.notifications=new Map}' +
      "emitCompletedThreadsChanged(){log.push('emit')}stageNotificationSoundIfNeeded(l){log.push('stage:'+l)}" +
      "removeNotification(id){log.push('remove:'+id)}" + SOURCE + '}')(log);
    let destroyed = false;
    const e = {id: 'n1'}, t = {isDestroyed: () => destroyed}, manager = new Manager();
    globalThis.__codexManagerNotificationShow = async (notice, contents, fallback) => {
      await new Promise(resolve => setImmediate(resolve));
      if (change === 'replace') manager.notifications.set('n1', {notification: {}});
      if (change === 'destroy') destroyed = true;
      fallback();
    };
    manager.present(e, t, {show: () => log.push('show')}, 'default');
    await new Promise(resolve => setTimeout(resolve, 5));
    results[change] = log;
  }
  console.log(JSON.stringify(results));
})().catch(error => { console.error(error); process.exitCode = 1; });
'''.replace('SOURCE', json.dumps(source))
        result = subprocess.run(['node', '-e', program], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), {
            'none': ['emit', 'stage:default', 'show', 'sound:default'],
            'replace': ['emit', 'stage:default'],
            'destroy': ['emit', 'stage:default', 'remove:n1']})

    def test_unknown_browser_runtime_fails_before_publication(self):
        self.path.write_bytes(self.path.read_bytes().replace(bundle._BROWSER_RUNTIME, b'x' * len(bundle._BROWSER_RUNTIME)))
        with self.assertRaises(ValueError): bundle.patch_archive(self.path, self.root / 'patched.asar')
        self.assertFalse((self.root / 'patched.asar').exists())

    def test_private_copy_is_cached_and_never_changes_installed_assets(self):
        before = {p.relative_to(self.source): p.read_bytes() for p in self.source.rglob('*') if p.is_file()}
        first = bundle.prepare(self.root, self.app)
        with patch.object(bundle.shutil, 'copytree', side_effect=AssertionError('recopy')):
            self.assertEqual(bundle.prepare(self.root, self.app), first)
        self.assertNotEqual(first['executable'], self.app['executable'])
        for name, value in before.items():
            self.assertEqual((self.source / name).read_bytes(), value)
        self.assertEqual(Path(first['executable']).read_bytes(), before[Path('ChatGPT.exe')])
        self.assertEqual((Path(first['executable']).parent / 'chrome.dll').read_bytes(), before[Path('chrome.dll')])

    def test_unsupported_archive_and_integrity_enforcement_never_fall_back_to_global_pipe(self):
        archive(self.path, b'new unrecognized implementation')
        with self.assertRaises(ValueError): bundle.prepare(self.root, self.app)
        self.assertFalse(list((self.root / 'artifacts/managed-desktop').glob('*/manager-desktop.json')))
        (self.source / 'chrome.dll').write_bytes(b'dL7pKGdnNz796PbbjQWNKmHXBZaB9tsX\x01\x09101110011')
        with self.assertRaises(ValueError): bundle.check_archive_support(self.source)

    def enforce_integrity(self, digest=None):
        (self.source / 'chrome.dll').write_bytes(b'dL7pKGdnNz796PbbjQWNKmHXBZaB9tsX\x01\x09010011001')
        record = (b'[{"file":"resources\\\\app.asar","alg":"SHA256","value":"'
                  + (digest or bundle.header_digest(self.path)).encode() + b'"}]PADDINGX')
        (self.source / 'ChatGPT.exe').write_bytes(b'MZ executable ' + record + b' tail')

    def test_integrity_enforced_copy_keeps_validation_and_carries_its_own_archive_digest(self):
        self.enforce_integrity()
        installed = {p.relative_to(self.source): p.read_bytes() for p in self.source.rglob('*') if p.is_file()}
        bundle.check_archive_support(self.source)
        result = bundle.prepare(self.root, self.app)
        copy = Path(result['executable']).parent
        sealed = bundle.header_digest(copy / 'resources/app.asar')
        self.assertNotEqual(sealed, bundle.header_digest(self.path))
        executable = (copy / 'ChatGPT.exe').read_bytes()
        self.assertEqual(len(executable), len(installed[Path('ChatGPT.exe')]))
        self.assertEqual(executable, installed[Path('ChatGPT.exe')].replace(
            bundle.header_digest(self.path).encode(), sealed.encode()))
        marker = json.loads((copy / 'manager-desktop.json').read_text(encoding='utf-8'))
        self.assertEqual(marker['integrity']['sealed'], sealed)
        self.assertEqual(marker['hashes']['ChatGPT.exe'], hashlib.sha256(executable).hexdigest())
        # The fuse itself is untouched, and the installed package is unchanged.
        self.assertEqual((copy / 'chrome.dll').read_bytes(), installed[Path('chrome.dll')])
        self.assertEqual({p.relative_to(self.source): p.read_bytes() for p in self.source.rglob('*') if p.is_file()}, installed)

    def test_integrity_record_that_does_not_match_the_installed_archive_is_refused(self):
        self.enforce_integrity('0' * 64)
        with self.assertRaisesRegex(ValueError, '무결성'):
            bundle.check_archive_support(self.source)
        self.enforce_integrity()
        executable = (self.source / 'ChatGPT.exe').read_bytes()
        (self.source / 'ChatGPT.exe').write_bytes(executable + executable)  # two records
        with self.assertRaisesRegex(ValueError, '무결성'):
            bundle.check_archive_support(self.source)

    def test_names_are_distinct_and_cannot_escape_namespace(self):
        a, b = str(uuid4()), str(uuid4())
        self.assertNotEqual(bundle.pipe_name(a), bundle.pipe_name(b))
        self.assertEqual(bundle.pipe_name(a.upper()), bundle.pipe_name(a))
        for value in ('codex-ipc', '../original', '', '123'):
            with self.assertRaises(ValueError): bundle.pipe_name(value)

    def test_verified_new_minifier_bindings_and_ambiguity(self):
        from manager_core.original_sync_bundle import patches_for
        content = b';'.join(PATCHES).replace(b'()=>c===', b'()=>l===')
        self.assertIsNotNone(patches_for(content))
        self.assertIsNone(patches_for(content + b';' + b';'.join(PATCHES)))
        self.assertIsNotNone(patches_for(content.replace(b'if(s.type', b'if(c.type')))
        for variants in (bundle._PIPE_VARIANTS, bundle._RENDERER_VARIANTS):
            keys = list(variants)
            self.assertEqual(bundle._matching_variant(keys[1], variants), keys[1])
            with self.assertRaises(ValueError): bundle._matching_variant(b';'.join(keys), variants)
            with self.assertRaises(ValueError): bundle._matching_variant(keys[1]*2, variants)

    @unittest.skipUnless(shutil.which('node'), 'Node.js required for pipe isolation behavior')
    def test_new_path_import_binding_preserves_strict_profile_pipe_namespace(self):
        original = bundle._ORIGINAL.replace(b'return i.join', b'return o.join')
        self.assertEqual(original, bundle._matching_variant(original, bundle._PIPE_VARIANTS))
        archive(self.path, source=original)
        target = self.root / 'renamed-path-import.asar'
        bundle.patch_archive(self.path, target)
        replacement = bundle._PIPE_VARIANTS[original]
        self.assertIn(replacement, entry(target, '.vite/build/main.js'))
        program = '''const build=new Function('process','o',SOURCE), path=require('node:path').win32;
const p='codex-manager-00000000-0000-4000-8000-000000000000';
const result=build({platform:'win32',env:{CODEX_MANAGER_DESKTOP_PIPE:p}},path);
let refused=0;for(const value of ['', 'codex-ipc', '../other', undefined]) {
 try {build({platform:'win32',env:{CODEX_MANAGER_DESKTOP_PIPE:value}},path)}catch {refused++}
}
console.log(JSON.stringify({result,refused}));'''.replace('SOURCE', json.dumps(replacement.decode()))
        result = subprocess.run([shutil.which('node'), '-e', program], capture_output=True,
                                text=True, check=True, timeout=10)
        parsed = json.loads(result.stdout)
        self.assertEqual('\\\\.\\pipe\\codex-manager-00000000-0000-4000-8000-000000000000', parsed['result'])
        self.assertEqual(4, parsed['refused'])

    def test_new_package_compatibility_failure_uses_verified_isolated_copy(self):
        first = bundle.prepare(self.root, self.app)
        archive(self.path, b'unknown new package implementation')
        newer = dict(self.app, Version='26.999.1.0')
        recovered = bundle.prepare(self.root, newer)
        self.assertEqual(recovered['executable'], first['executable'])
        self.assertEqual(recovered['Version'], self.app['Version'])
        self.assertIn('26.999.1.0', recovered['desktop_compatibility_notice'])
        self.assertFalse(list((self.root / 'artifacts/managed-desktop').glob('*.staging-*')))

    def test_fallback_rejects_changed_or_missing_program_files(self):
        first = bundle.prepare(self.root, self.app)
        (Path(first['executable']).parent / 'chrome.dll').unlink()
        archive(self.path, b'unknown new package implementation')
        with self.assertRaises(ValueError): bundle.prepare(self.root, dict(self.app, Version='26.999.1.0'))

    def test_partial_copy_is_discarded_and_next_attempt_recovers(self):
        with patch.object(bundle.shutil, 'copytree', side_effect=OSError('power loss')):
            with self.assertRaises(OSError): bundle.prepare(self.root, self.app)
        self.assertFalse(list((self.root / 'artifacts/managed-desktop').glob('*/manager-desktop.json')))
        self.assertTrue(Path(bundle.prepare(self.root, self.app)['executable']).is_file())

    def test_corrupt_published_cache_is_rebuilt_without_touching_source(self):
        first = bundle.prepare(self.root, self.app)
        target = Path(first['executable']).parent
        (target / 'resources/app.asar').write_bytes(b'incomplete')
        recovered = bundle.prepare(self.root, self.app)
        self.assertEqual(first['executable'], recovered['executable'])
        self.assertGreater((target / 'resources/app.asar').stat().st_size, 100)
        self.assertEqual((self.source / 'ChatGPT.exe').read_bytes(), b'unmodified-executable')

    def test_complete_staging_from_abrupt_shutdown_is_never_executed(self):
        first = bundle.prepare(self.root, self.app)
        target = Path(first['executable']).parent
        stage = target.with_name(target.name + '.staging-abrupt-shutdown')
        target.rename(stage)
        recovered = bundle.prepare(self.root, self.app)
        self.assertEqual(first['executable'], recovered['executable'])
        self.assertTrue(stage.is_dir())


class StartupLinkGuardTests(unittest.TestCase):
    """A queued thread link of an unregistered SSH host must not end startup."""

    LOOKUP_ASYNC = LOOKUP_930.replace(b'?(e,t)=>', b'?async(e,t)=>')
    FLUSH_GUARDED = FLUSH_930.replace(b'flushPendingDeepLinks()', b'flushPendingDeepLinks()' + bundle._DEEP_LINK_FLUSH_GUARD)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    @staticmethod
    def apply(modules, version):
        patches = bundle.deep_link_guard_patches(list(modules.items()), version)
        result = dict(modules)
        for name, replacements in patches.items():
            for before, after in replacements.items():
                result[name] = result[name].replace(before, after)
        return result

    def test_both_anchors_are_patched_once_across_main_modules(self):
        modules = {'.vite/build/bootstrap.js': b'boot();', '.vite/build/main.js': LINKS_930}
        for version in (VERSION_7945, '26.930.31730', None, '26.917.71314'):
            with self.subTest(version):
                patched = self.apply(modules, version)
                self.assertEqual(patched['.vite/build/bootstrap.js'], b'boot();')
                self.assertEqual(patched['.vite/build/main.js'].count(self.LOOKUP_ASYNC), 1)
                self.assertEqual(patched['.vite/build/main.js'].count(self.FLUSH_GUARDED), 1)
                self.assertNotIn(LOOKUP_930, patched['.vite/build/main.js'])
                # Patching an already patched module changes nothing.
                self.assertEqual(bundle.deep_link_guard_patches(list(patched.items()), version), {})
        # Renamed bindings keep matching; the flush of 26.917 is guarded too.
        renamed = LINKS_930.replace(b'We.deepLinks', b'Xe.deepLinks').replace(b'c(r??', b'g(n??')
        self.assertEqual(self.apply({'m.js': renamed}, VERSION_7945)['m.js'].count(b'?async(e,t)=>g(n??'), 1)
        old = self.apply({'m.js': FLUSH_917}, '26.917.71314')['m.js']
        self.assertEqual(old, FLUSH_917.replace(b'flushPendingDeepLinks()', b'flushPendingDeepLinks()' + bundle._DEEP_LINK_FLUSH_GUARD))

    def test_missing_anchor_is_refused_from_26_930_and_skipped_before(self):
        cases = {'no lookup': LINKS_930.replace(LOOKUP_930, b'readThread:void 0'),
                 'no flush': LINKS_930.replace(FLUSH_930, b'}'),
                 'changed lookup': LINKS_930.replace(b'.readThread(e,t):void 0', b'.readThread(t,e):void 0'),
                 'flush not in the startup sequence': LINKS_930.replace(b'flushPendingDeepLinks(),', b'flushPendingDeepLinks();')}
        for label, data in cases.items():
            for version in (VERSION_7945, '26.930.41038', '26.1015.1'):
                with self.subTest(label, version=version), self.assertRaisesRegex(ValueError, '작업 링크'):
                    bundle.deep_link_guard_patches([('main.js', data)], version)
            for version in (None, '26.917.71314', 'unknown'):
                with self.subTest(label, version=version):
                    bundle.deep_link_guard_patches([('main.js', data)], version)

    def test_ambiguous_anchor_is_refused_for_every_version(self):
        for version in (VERSION_7945, None):
            for modules in ([('a.js', LINKS_930), ('b.js', LINKS_930)], [('a.js', LINKS_930 + FLUSH_930)],
                            [('a.js', LINKS_930), ('b.js', self.FLUSH_GUARDED)]):
                with self.subTest(version=version, modules=[name for name, _ in modules]), \
                        self.assertRaisesRegex(ValueError, 'Ambiguous desktop startup link'):
                    bundle.deep_link_guard_patches(modules, version)

    # This fixture has no SSH worktree grouping; that 26.930 requirement has its own tests.
    @patch('manager_core.original_sync_bundle.require_project_grouping')
    def test_managed_copy_carries_the_guard_and_refuses_26_930_without_it(self, _grouping):
        source, target = self.root / 'source.asar', self.root / 'patched.asar'
        archive(source, links=LINKS_930, version=VERSION_7945)
        bundle.patch_archive(source, target)
        main = entry(target, '.vite/build/main.js')
        self.assertEqual(main.count(self.LOOKUP_ASYNC), 1)
        self.assertEqual(main.count(self.FLUSH_GUARDED), 1)
        self.assertIn(bundle._REPLACEMENT, main)  # with the profile pipe patch of the same module
        target.unlink()
        archive(source, links=LINKS_930.replace(LOOKUP_930, b'readThread:void 0'), version=VERSION_7945)
        with self.assertRaisesRegex(ValueError, '작업 링크'):
            bundle.patch_archive(source, target)
        self.assertFalse(target.exists())
        # Fixtures without a package version keep the native code (older builds).
        archive(source)
        bundle.patch_archive(source, target)
        self.assertNotIn(b'flushPendingDeepLinks', entry(target, '.vite/build/main.js'))

    @unittest.skipUnless(shutil.which('node'), 'Node.js required for startup link behavior')
    def test_queued_link_of_an_unregistered_ssh_host_no_longer_ends_startup(self):
        def run(program, route):
            path = self.root / 'startup.cjs'
            path.write_bytes(program)
            result = subprocess.run([shutil.which('node'), str(path), json.dumps(route)],
                                    capture_output=True, text=True, check=True, timeout=20)
            return json.loads(result.stdout)
        remote = {'kind': 'localConversation', 'conversationId': 'x', 'hostId': 'remote-ssh-discovered:remote-dev'}
        # Native 26.930: the incident ("<app> failed to start", Quit only).
        native = run(STARTUP_PROGRAM, remote)
        self.assertFalse(native['started'])
        self.assertEqual(native['error'], 'Connection for host ID remote-ssh-discovered:remote-dev not found')
        self.assertEqual(native['warnings'], [])
        patched = self.apply({'main.js': STARTUP_PROGRAM}, VERSION_7945)['main.js']
        guarded = run(patched, remote)
        self.assertTrue(guarded['started'], guarded)
        self.assertEqual(guarded['phases'], ['pending deep links flushed', 'startup complete'])
        self.assertEqual(guarded['warnings'], ['local_conversation_deep_link_lookup_failed'])
        self.assertEqual(guarded['navigated'], [])  # stays on the current (local) view
        # A registered host still navigates, and any other link failure stays non-fatal.
        self.assertEqual(run(patched, {**remote, 'hostId': 'local'})['navigated'], ['/local/x'])
        other = run(patched, {'kind': 'other'})
        self.assertTrue(other['started'])
        self.assertFalse(run(STARTUP_PROGRAM, {'kind': 'other'})['started'])


if __name__ == '__main__': unittest.main()
