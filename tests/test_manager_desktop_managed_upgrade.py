"""Pinned managed-cache upgrade, independent of any installed desktop."""
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core import desktop_bundle as bundle, desktop_managed_upgrade as upgrade
from manager_core import desktop_publication as publication
import test_manager_desktop_reasoning as reasoning_fixtures


def write_archive(path, chunks):
    header = {'files': {}}
    offset = 0
    for name, data in chunks:
        node = header
        parts = name.split('/')
        for part in parts[:-1]:
            node = node['files'].setdefault(part, {'files': {}})
        node['files'][parts[-1]] = dict(offset=str(offset), size=len(data), integrity=dict(blockSize=32))
        offset += len(data)
    data = json.dumps(header).encode()
    payload = struct.pack('<I', len(data)) + data + b'\0' * (-len(data) % 4)
    path.write_bytes(struct.pack('<III', 4, len(payload) + 4, len(payload)) + payload + b''.join(v for _, v in chunks))


def read_entries(path):
    result = {}
    with path.open('rb') as stream:
        header, base = bundle.read_header(stream)
        for name, item in bundle._entries(header):
            stream.seek(base + int(item['offset']))
            result[name] = (item, stream.read(item['size']))
    return result


class ManagedUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.parent = self.root / 'artifacts/managed-desktop'
        self.source = self.parent / upgrade._BASELINE_DIRECTORY
        (self.source / 'resources').mkdir(parents=True)
        (self.source / 'ChatGPT.exe').write_bytes(b'unchanged-executable')
        (self.source / 'chrome.dll').write_bytes(b'dL7pKGdnNz796PbbjQWNKmHXBZaB9tsX\x01\x09101100011')
        original = reasoning_fixtures.DesktopReasoningTests().fixture(b'vw')
        self.picker = original.replace(b'let e=o?', b'let e=o||s?').replace(b'vw(e)&&i.has(e)', b'vw(e)&&(s||i.has(e))')
        self.picker += (b';e.PRO=`pro`,e.PROLITE=`prolite`;'
                        b'WIt=[pg.FREE,pg.GO,pg.PLUS,pg.PRO,pg.PROLITE,pg.FREE_WORKSPACE];'
                        b'Zzt=[`free`,`go`,`plus`,`prolite`,`pro`];'
                        b'BRt=Es(`free.go.plus.pro.prolite.unknown`.split(`.`));')
        self.composer = reasoning_fixtures.DesktopReasoningTests().composer()
        write_archive(self.source / 'resources/app.asar', [
            ('before.bin', b'UNCHANGED\x00BEFORE'),
            ('webview/assets/app-initial-fixture.js', self.picker),
            ('middle.bin', b'UNCHANGED\x00MIDDLE'),
            ('webview/assets/app-primary-fixture.js', self.composer),
            ('after.bin', b'UNCHANGED\x00AFTER')])
        identity = bundle.adapter_identity()
        identity['patch'] = upgrade._BASELINE_PATCH_SHA256
        identity['adapters'].pop('desktop_managed_upgrade.py')
        identity['adapters']['desktop_reasoning_ui.py'] = upgrade._BASELINE_REASONING_SHA256
        self.marker = dict(source=dict(version='26.917.9434.0', source='removed-official-package', size=123,
            modified=456, **identity), patch=dict(module='unchanged-isolation-module'),
            files=publication._inventory(self.source),
            hashes={name: publication._hash(self.source / name) for name in ('ChatGPT.exe', 'chrome.dll', 'resources/app.asar')})
        self._pin_marker()

    def _pin_marker(self):
        path = self.source / upgrade._MARKER
        path.write_text(json.dumps(self.marker), encoding='utf8')
        for name, value in [('_BASELINE_MARKER_SHA256', hashlib.sha256(path.read_bytes()).hexdigest()),
                            ('_BASELINE_ARCHIVE_SHA256', self.marker['hashes']['resources/app.asar'])]:
            mock = patch.object(upgrade, name, value)
            mock.start()
            self.addCleanup(mock.stop)

    def test_upgrade_preserves_source_and_unrelated_assets_and_validates_fallback(self):
        before = {name: (self.source / name).read_bytes() for name in self.marker['files']}
        target, value = upgrade.upgrade(self.root)
        self.assertNotEqual(target, self.source)
        self.assertEqual(value['source']['adapters'], bundle.adapter_identity()['adapters'])
        self.assertIsNotNone(publication.validated(target, upgrade._MARKER, value['source']))
        self.assertEqual(publication._fallback(self.parent, upgrade._MARKER, value['source'])[0], target)
        self.assertEqual({name: (self.source / name).read_bytes() for name in before}, before)
        self.assertEqual((target / 'ChatGPT.exe').read_bytes(), before['ChatGPT.exe'])
        self.assertEqual((target / 'chrome.dll').read_bytes(), before['chrome.dll'])
        old, updated = read_entries(self.source / 'resources/app.asar'), read_entries(target / 'resources/app.asar')
        for name in ('before.bin', 'middle.bin', 'after.bin'):
            self.assertEqual(updated[name][1], old[name][1])
        for name in ('webview/assets/app-initial-fixture.js', 'webview/assets/app-primary-fixture.js'):
            item, content = updated[name]
            self.assertIn(b'ultracode', content)
            self.assertEqual(item['integrity']['hash'], hashlib.sha256(content).hexdigest())
            self.assertEqual(item['integrity']['blocks'], [hashlib.sha256(content[i:i+32]).hexdigest() for i in range(0, len(content), 32)])
        self.assertEqual(len(value['upgrade']['entries']), 2)
        self.assertIn(b'`pro`,`promax`]', updated['webview/assets/app-initial-fixture.js'][1])
        self.assertEqual(upgrade.upgrade(self.root)[0], target)
        installed = self.root / 'installed-latest'
        (installed / 'resources').mkdir(parents=True)
        latest_fuse = b'dL7pKGdnNz796PbbjQWNKmHXBZaB9tsX\x01\x09010011001'
        (installed / 'chrome.dll').write_bytes(latest_fuse)
        (installed / 'ChatGPT.exe').write_bytes(b'installed-latest')
        (installed / 'resources/app.asar').write_bytes(b'installed-protected-archive')
        result = bundle.prepare(self.root, dict(executable=str(installed / 'ChatGPT.exe'), Version='26.924.2738.0'))
        self.assertEqual(result['Version'], '26.917.9434.0')
        self.assertEqual(Path(result['executable']).parent, target)
        self.assertIn('26.924.2738.0', result['desktop_compatibility_notice'])
        self.assertEqual((installed / 'chrome.dll').read_bytes(), latest_fuse)
        self.assertEqual((installed / 'resources/app.asar').read_bytes(), b'installed-protected-archive')

    def test_other_adapter_delta_is_rejected_before_copy(self):
        current = bundle.adapter_identity()
        current['adapters']['desktop_record_sync.cjs'] = '0' * 64
        with patch.object(bundle, 'adapter_identity', return_value=current), self.assertRaisesRegex(ValueError, 'non-reasoning'):
            upgrade.upgrade(self.root)
        self.assertEqual([p.name for p in self.parent.iterdir() if p.is_dir()], [self.source.name])

    def test_missing_or_duplicate_plan_anchor_is_rejected(self):
        for data in (self.picker.replace(b'Zzt=', b'changed='), self.picker + self.picker):
            with self.subTest(data=len(data)), self.assertRaisesRegex(ValueError, 'anchor is not unique'):
                upgrade._upgrade_plan_support(data)

    def test_unrecognized_marker_is_rejected(self):
        (self.source / upgrade._MARKER).write_text('{}', encoding='utf8')
        with self.assertRaisesRegex(ValueError, 'audited baseline'):
            upgrade.upgrade(self.root)

    def test_unrecognized_baseline_patcher_is_rejected_even_with_other_adapters_equal(self):
        self.marker['source']['patch'] = '0' * 64
        self._pin_marker()
        with self.assertRaisesRegex(ValueError, 'code identity'):
            upgrade.upgrade(self.root)

    def test_corrupt_archive_is_rejected(self):
        path = self.source / 'resources/app.asar'
        info = path.stat()
        content = path.read_bytes()
        path.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        with self.assertRaisesRegex(ValueError, 'failed validation'):
            upgrade.upgrade(self.root)

    def test_integrity_enforced_source_is_still_rejected(self):
        path = self.source / 'chrome.dll'
        path.write_bytes(b'dL7pKGdnNz796PbbjQWNKmHXBZaB9tsX\x01\x09010011001')
        self.marker['files']['chrome.dll'] = dict(size=path.stat().st_size, modified=path.stat().st_mtime_ns)
        self.marker['hashes']['chrome.dll'] = publication._hash(path)
        self._pin_marker()
        with self.assertRaisesRegex(ValueError, '지원하지'):
            upgrade.upgrade(self.root)

    def test_unrecognized_picker_does_not_publish_and_cleans_owned_stage(self):
        with patch('manager_core.desktop_reasoning_ui.upgrade_managed', side_effect=ValueError('unrecognized picker')):
            with self.assertRaisesRegex(ValueError, 'unrecognized picker'):
                upgrade.upgrade(self.root)
        self.assertEqual([p.name for p in self.parent.iterdir() if p.is_dir()], [self.source.name])


if __name__ == '__main__':
    unittest.main()
