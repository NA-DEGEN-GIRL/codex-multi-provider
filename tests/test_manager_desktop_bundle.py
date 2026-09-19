import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core import desktop_bundle as bundle
from manager_core.original_sync_bundle import PATCHES, RENDERER_PATCHES


def archive(path, source=None, notification=None):
    body = b'function s9(){' + (source if source is not None else bundle._ORIGINAL) + b'return "posix";}'
    notice = notification if notification is not None else (bundle._NOTIFICATION_CLICK + b'originalCallback();})' +
        bundle._NOTIFICATION_SHOW + bundle._WINDOW_MESSAGE)
    chunks = [('.vite/build/before.bin', b'FIRST'), ('.vite/build/main.js', body),
              ('.vite/build/notifications.js', notice), ('.vite/build/after.bin', b'AFTER'),
              ('.vite/build/browser-runtime.js', b'Qr({' + bundle._BROWSER_RUNTIME + b');'),
              ('.vite/build/context.js', b'' +
               (b'' if source is not None and all(pattern in source for pattern in PATCHES) else b';'.join(PATCHES))),
              ('webview/assets/app-initial-fixture.js', bundle._CONTEXT_RENDERER + b';' + b';'.join(RENDERER_PATCHES))]
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


if __name__ == '__main__': unittest.main()
