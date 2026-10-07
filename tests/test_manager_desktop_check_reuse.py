"""A launch wave reuses one recent desktop copy check without hiding changes."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.instances import Instances
from manager_core.store import Store


class DesktopCheckReuseTests(unittest.TestCase):
    HASHED = ('ChatGPT.exe', 'chrome.dll', 'resources/app.asar')

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        installed = self.root / 'installed'
        (installed / 'resources').mkdir(parents=True)
        (installed / 'resources/app.asar').write_bytes(b'installed archive')
        (installed / 'ChatGPT.exe').write_bytes(b'installed program')
        self.installed = dict(executable=str(installed / 'ChatGPT.exe'), Version='26.900.1.0')
        self.copy = self.root / 'artifacts/managed-desktop/26.900.1.0-fixture'
        for name, data in {'ChatGPT.exe': b'program', 'chrome.dll': b'library',
                           'resources/app.asar': b'patched archive', 'locales/en-US.pak': b'resource'}.items():
            (self.copy / name).parent.mkdir(parents=True, exist_ok=True)
            (self.copy / name).write_bytes(data)
        self.write_marker()
        self.app = dict(self.installed, executable=str(self.copy / 'ChatGPT.exe'),
                        desktop_isolation_revision='fixture', desktop_compatibility_notice='')
        self.instances = Instances(self.root, Store(self.root), None)
        self.prepare = self.enterContext(patch('manager_core.desktop_bundle.prepare',
                                               side_effect=lambda root, installed: dict(self.app)))

    def write_marker(self, **extra):
        files = {path.relative_to(self.copy).as_posix(): dict(size=path.stat().st_size, modified=path.stat().st_mtime_ns)
                 for path in self.copy.rglob('*') if path.is_file() and path.name != 'manager-desktop.json'}
        hashes = {name: hashlib.sha256((self.copy / name).read_bytes()).hexdigest() for name in self.HASHED}
        (self.copy / 'manager-desktop.json').write_text(json.dumps(dict(source={}, files=files, hashes=hashes, **extra)),
                                                        encoding='utf8')

    def check(self):
        return self.instances._prepare_desktop(dict(self.installed))

    def rewrite(self, path, data):
        before = path.stat()
        time.sleep(.05)  # a later clock tick for the content-change time
        path.write_bytes(data)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

    def test_launches_of_one_wave_share_one_full_check(self):
        self.assertEqual(self.check(), self.app)
        with patch('pathlib.Path.lstat', autospec=True, side_effect=Path.lstat) as lstat:
            self.assertEqual(self.check(), self.app)
        self.assertEqual(self.prepare.call_count, 1)
        checked = {Path(call.args[0]).relative_to(self.copy).as_posix() for call in lstat.call_args_list}
        self.assertEqual(checked, set(self.HASHED))  # not the copy's other files

    def test_changed_hashed_file_is_found_by_its_digest_while_reused(self):
        self.check()
        self.rewrite(self.copy / 'chrome.dll', b'LIBRARY')  # same size and mtime
        self.check()
        self.assertEqual(self.prepare.call_count, 2)

    def test_other_files_are_checked_again_once_the_reuse_window_ends(self):
        self.check()
        (self.copy / 'locales/en-US.pak').unlink()
        self.check()  # inside the window, only the hashed files are checked
        self.assertEqual(self.prepare.call_count, 1)
        with patch.object(Instances, 'DESKTOP_REUSE_SECONDS', 0):
            self.check()
        self.assertEqual(self.prepare.call_count, 2)

    def test_marker_installed_package_or_adapters_change_forces_a_full_check(self):
        self.check()
        self.write_marker(republished=True)
        self.check()
        self.assertEqual(self.prepare.call_count, 2)
        archive = Path(self.installed['executable']).parent / 'resources/app.asar'
        os.utime(archive, ns=(archive.stat().st_atime_ns, archive.stat().st_mtime_ns + 1_000_000))  # an update
        self.check()
        self.assertEqual(self.prepare.call_count, 3)
        with patch('manager_core.desktop_bundle.adapter_identity', return_value=dict(revision='other')):
            self.check()
        self.assertEqual(self.prepare.call_count, 4)
        self.installed['DisplayName'] = 'changed package metadata'
        self.check()
        self.assertEqual(self.prepare.call_count, 5)

    def test_fallback_copy_and_failed_check_are_never_reused(self):
        self.app['desktop_compatibility_notice'] = 'fixture fallback'
        self.check()
        self.check()
        self.assertEqual(self.prepare.call_count, 2)
        self.app['desktop_compatibility_notice'] = ''
        self.check()
        self.write_marker(republished=True)
        self.prepare.side_effect = OSError('fixture failure')
        with self.assertRaises(OSError):
            self.check()
        self.write_marker()
        self.prepare.side_effect = lambda root, installed: dict(self.app)
        self.check()
        self.assertEqual(self.prepare.call_count, 5)

    def test_missing_marker_is_never_reused(self):
        (self.copy / 'manager-desktop.json').unlink()
        self.check()
        self.check()
        self.assertEqual(self.prepare.call_count, 2)


if __name__ == '__main__':
    unittest.main()
