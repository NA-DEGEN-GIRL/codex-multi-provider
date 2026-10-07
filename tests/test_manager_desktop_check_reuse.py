"""A launch wave reuses one recent desktop copy check without hiding changes."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

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
        for name, data in {'ChatGPT.exe': b'program', 'chrome.dll': b'library', 'vulkan-1.dll': b'top-level library',
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
        self.assertEqual(checked, {*self.HASHED, 'vulkan-1.dll'})  # not the copy's deeper files
        self.assertLessEqual(Instances.DESKTOP_REUSE_SECONDS, 30)  # one launch wave, not later relaunches

    def test_lost_top_level_library_is_found_while_reused(self):
        self.check()
        (self.copy / 'vulkan-1.dll').unlink()  # e.g. quarantined by antivirus software
        self.check()
        self.assertEqual(self.prepare.call_count, 2)

    def test_hashed_file_replaced_by_a_link_is_never_reused(self):
        if os.name != 'nt':
            self.skipTest('junctions are Windows-only')
        import _winapi
        self.check()
        outside = self.root / 'outside'
        outside.mkdir()
        (self.copy / 'chrome.dll').unlink()
        _winapi.CreateJunction(str(outside), str(self.copy / 'chrome.dll'))
        self.check()
        self.assertEqual(self.prepare.call_count, 2)
        os.rmdir(self.copy / 'chrome.dll')

    def test_relaunch_of_a_profile_gets_a_check_that_started_after_it_asked(self):
        for profile_id in ('first', 'second'):  # first launches of one wave
            self.instances._join_desktop_wave(profile_id)
            self.check()
        self.assertEqual(self.prepare.call_count, 1)
        self.instances._join_desktop_wave('first')  # its desktop exited; it launches again
        self.check()
        self.assertEqual(self.prepare.call_count, 2)
        self.instances._join_desktop_wave('third')  # the new check serves the rest of the wave
        self.check()
        self.assertEqual(self.prepare.call_count, 2)

    def test_check_running_when_reuse_is_distrusted_is_not_reused(self):
        def distrusted_meanwhile(root, installed):
            self.instances._distrust_desktop_checks()  # e.g. another launch failed
            return dict(self.app)
        self.prepare.side_effect = distrusted_meanwhile
        self.check()
        self.prepare.side_effect = lambda root, installed: dict(self.app)
        self.check()
        self.check()
        self.assertEqual(self.prepare.call_count, 2)

    def test_launch_failing_after_the_desktop_check_ends_its_reuse(self):
        store = self.instances.store
        profiles = [store.add_profile(name)['id'] for name in ('first', 'second', 'third')]
        installed = self.enterContext(patch.object(self.instances, 'installed_app', return_value=dict(self.installed)))
        self.enterContext(patch.object(self.instances, 'observe', return_value=dict(status='stopped')))
        self.enterContext(patch.object(self.instances, 'prepare', return_value={}))
        self.enterContext(patch('manager_core.login_health.require'))
        failure = self.enterContext(patch.object(self.instances, '_launch_prepared',
                                                 return_value=dict(state='launched')))
        self.instances._show(profiles[0])
        self.instances._show(profiles[1])
        self.assertEqual(self.prepare.call_count, 1)  # one wave
        failure.side_effect = OSError('fixture spawn failure')
        with self.assertRaises(OSError):
            self.instances._show(profiles[2])
        self.assertEqual(self.prepare.call_count, 1)
        failure.side_effect = None
        self.instances._show(store.add_profile('fourth')['id'])
        self.assertEqual(self.prepare.call_count, 2)  # the failure ended the reuse
        self.assertTrue(installed.called)

    def test_desktop_exiting_before_its_window_ends_the_reuse(self):
        store = self.instances.store
        profile = store.add_profile('fixture')
        lifetime = dict(generation='fixture', process_id=4242, process_created=1, executable_path='fixture.exe')
        store.mutate(lambda data: store.profile(profile['id'], data).update(lifetime))
        process = Mock(pid=4242)
        process.poll.return_value = 1  # exited during startup
        self.instances.processes[profile['id']] = process
        self.check()
        with patch('manager_core.instances.main_window', return_value=None), \
                patch('manager_core.instances.process_identity', return_value=None):
            self.instances._finish_show(dict(state='launched', profile_id=profile['id'],
                                             profile={**store.profile(profile['id']), **lifetime}))
        self.check()
        self.assertEqual(self.prepare.call_count, 2)

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
