"""Exercise exact-version Browser publication without launching an app or browser."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core import browser_bundle as bundle
from manager_core import desktop_publication as publication
from manager_core.store import Store
from repair_browser_plugins import repair


class BrowserBundleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.executable = self.root / 'desktop/ChatGPT.exe'
        self.source = self.executable.parent / 'resources/plugins/openai-bundled/plugins/browser'
        self.home = self.root / 'profile'
        self.parent = self.home / 'plugins/cache/openai-bundled/browser'
        self.version = '26.900.12345'
        self.target = self.parent / self.version
        self.files = {name: ('fixture ' + name).encode() for name in bundle._REQUIRED}
        self.files['.codex-plugin/plugin.json'] = json.dumps(dict(name='browser', version=self.version)).encode()
        self.files['node_modules/dependency/index.js'] = b'export const fixture = true;'
        for name, value in self.files.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)

    def ensure(self):
        return bundle.ensure(self.home, self.executable)

    def assert_bundle(self):
        for name, value in self.files.items():
            self.assertEqual((self.target / name).read_bytes(), value)

    def test_old_profile_gets_exact_selected_desktop_and_keeps_old_assets_and_account_data(self):
        old = self.parent / '26.100.1/scripts/browser-service.mjs'
        old.parent.mkdir(parents=True)
        old.write_bytes(b'live old version')
        auth = self.home / 'auth.json'
        auth.write_bytes(b'private-fixture-not-a-real-token')
        config = self.home / 'config.toml'
        config.write_bytes(b'user preferences')
        data = self.home / 'plugins/data/browser/session-fixture'
        data.parent.mkdir(parents=True)
        data.write_bytes(b'untouched session')
        self.assertEqual(self.ensure()['state'], 'prepared')
        self.assert_bundle()
        self.assertEqual(old.read_bytes(), b'live old version')
        self.assertEqual(auth.read_bytes(), b'private-fixture-not-a-real-token')
        self.assertEqual(config.read_bytes(), b'user preferences')
        self.assertEqual(data.read_bytes(), b'untouched session')

    def test_healthy_bundle_is_not_rewritten(self):
        self.ensure()
        before = {name: (self.target / name).stat().st_mtime_ns for name in self.files}
        with patch.object(bundle.shutil, 'copyfile', side_effect=AssertionError('no copy')):
            self.assertEqual(self.ensure()['state'], 'ready')
        self.assertEqual(before, {name: (self.target / name).stat().st_mtime_ns for name in self.files})

    def test_repairs_missing_and_corrupted_assets_preserving_healthy_files(self):
        self.ensure()
        (self.target / 'scripts/browser-service.mjs').unlink()
        (self.target / 'node_modules/dependency/index.js').write_bytes(b'corrupt')
        client = self.target / 'scripts/browser-client.mjs'
        before = client.stat().st_mtime_ns
        result = self.ensure()
        self.assertEqual((result['state'], result['changed_files']), ('repaired', 2))
        self.assertEqual(client.stat().st_mtime_ns, before)
        self.assert_bundle()

    def test_copy_failure_never_publishes_partial_new_version_and_retry_works(self):
        real_copy = bundle.shutil.copyfile
        def fail(source, target):
            if Path(source).name == 'browser-service.mjs':
                raise OSError('fixture interrupted')
            return real_copy(source, target)
        with patch.object(bundle.shutil, 'copyfile', side_effect=fail):
            with self.assertRaises(OSError):
                self.ensure()
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.parent.glob('.manager-stage-*')), [])
        self.ensure()
        self.assert_bundle()

    def test_corrupt_staging_and_changed_source_are_not_published(self):
        real_copy = bundle.shutil.copyfile
        def corrupt(source, target):
            real_copy(source, target)
            if Path(source).name == 'browser-service.mjs':
                Path(target).write_bytes(b'partial')
        with patch.object(bundle.shutil, 'copyfile', side_effect=corrupt):
            with self.assertRaises(OSError):
                self.ensure()
        self.assertFalse(self.target.exists())
        def change(source, target):
            real_copy(source, target)
            if Path(source).name == 'browser-service.mjs':
                Path(source).write_bytes(b'changed during copy')
        with patch.object(bundle.shutil, 'copyfile', side_effect=change):
            with self.assertRaises(OSError):
                self.ensure()
        self.assertFalse(self.target.exists())

    def test_missing_package_entrypoint_fails_before_touching_profile(self):
        (self.source / 'scripts/browser-service.mjs').unlink()
        with self.assertRaises(ValueError):
            self.ensure()
        self.assertFalse(self.home.exists())

    def test_invalid_manifest_and_traversal_version_never_touch_profile(self):
        for value in ([], dict(name='browser', version='../../escape'), dict(name='other', version='1.2.3')):
            with self.subTest(value=value):
                (self.source / '.codex-plugin/plugin.json').write_text(json.dumps(value), encoding='utf-8')
                with self.assertRaises(ValueError):
                    self.ensure()
                self.assertFalse(self.home.exists())

    def test_desktop_without_browser_leaves_home_unchanged(self):
        self.assertEqual(bundle.ensure(self.home, self.root / 'older/ChatGPT.exe')['state'], 'not_bundled')
        self.assertFalse(self.home.exists())

    def test_concurrent_requests_publish_one_complete_bundle(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.ensure(), range(4)))
        self.assertEqual(sum(r['state'] == 'prepared' for r in results), 1)
        self.assertEqual(sum(r['state'] == 'ready' for r in results), 3)
        self.assert_bundle()

    def test_interrupted_existing_repair_is_retryable_and_preserves_good_files(self):
        self.ensure()
        service = self.target / 'scripts/browser-service.mjs'
        service.unlink()
        with patch.object(bundle.os, 'replace', side_effect=OSError('fixture interrupted')):
            with self.assertRaises(OSError):
                self.ensure()
        self.assertFalse(service.exists())
        self.assertEqual((self.target / 'scripts/browser-client.mjs').read_bytes(), self.files['scripts/browser-client.mjs'])
        self.ensure()
        self.assert_bundle()

    def test_repair_rejects_directory_collision_before_changing_any_file(self):
        self.ensure()
        service = self.target / 'scripts/browser-service.mjs'
        service.unlink()
        service.mkdir()
        client = self.target / 'scripts/browser-client.mjs'
        client.write_bytes(b'old client')
        with self.assertRaises(ValueError):
            self.ensure()
        self.assertEqual(client.read_bytes(), b'old client')

    def test_long_profile_path_can_be_prepared(self):
        self.home = self.root.joinpath(*(['long-profile-' + 'a' * 60] * 4))
        def cleanup_long_tree():
            owned = bundle._inside(self.root, self.root / self.home.relative_to(self.root).parts[0])
            if owned.exists():
                shutil.rmtree(owned)
        self.addCleanup(cleanup_long_tree)
        result = self.ensure()
        target = bundle._plain(self.home / 'plugins/cache/openai-bundled/browser' / self.version)
        self.assertEqual(result['state'], 'prepared')
        self.assertEqual((target / 'scripts/browser-service.mjs').read_bytes(), self.files['scripts/browser-service.mjs'])

    def test_selecting_running_profile_repairs_its_own_desktop_without_relaunch(self):
        from control_center import ControlCenter
        center = ControlCenter.__new__(ControlCenter)
        center.store = Store(self.root)
        profile = center.store.add_profile('fixture')
        center.instances = Mock()
        center.instances.observe.return_value = dict(status='running', executable_path=str(self.executable))
        result = center._open_profile_locally(profile['id'])
        target = Path(profile['home']) / 'plugins/cache/openai-bundled/browser' / self.version
        self.assertEqual(result['state'], 'existing')
        self.assertEqual((target / 'scripts/browser-service.mjs').read_bytes(), self.files['scripts/browser-service.mjs'])
        center.instances.show.assert_not_called()

    def test_linked_destination_is_rejected_without_changing_outside_file(self):
        self.ensure()
        outside = self.root / 'outside.js'
        outside.write_bytes(b'untouched')
        target = self.target / 'scripts/browser-service.mjs'
        target.unlink()
        try:
            target.symlink_to(outside)
        except OSError:
            self.skipTest('symlink creation is not available')
        with self.assertRaises(ValueError):
            self.ensure()
        self.assertEqual(outside.read_bytes(), b'untouched')

    def settled(self):
        """Treat every content stamp as older than the one-second reuse guard."""
        return patch.object(publication, '_now_ns', side_effect=lambda: time.time_ns() + 2_000_000_000)

    def remembered(self):
        self.executable.write_bytes(b'desktop')
        self.assertEqual(self.ensure()['state'], 'prepared')
        self.assertEqual(self.ensure()['state'], 'ready')  # verified once, then remembered

    def test_verified_bundle_is_remembered_until_a_published_file_changes(self):
        with self.settled():
            self.remembered()
            with patch.object(bundle, '_inventory', side_effect=AssertionError('rescanned')):
                self.assertEqual(self.ensure(), dict(state='ready', version=self.version,
                                                     files=len(self.files), changed_files=0))
            service = self.target / 'scripts/browser-service.mjs'
            before = service.stat()
            time.sleep(.05)  # a later clock tick for the content-change time
            service.write_bytes(b'x' * before.st_size)  # same size, mtime restored below
            os.utime(service, ns=(before.st_atime_ns, before.st_mtime_ns))
            self.assertEqual(self.ensure()['state'], 'repaired')
        self.assert_bundle()

    def test_removed_file_after_verification_is_repaired(self):
        with self.settled():
            self.remembered()
            (self.target / 'node_modules/dependency/index.js').unlink()
            result = self.ensure()
        self.assertEqual((result['state'], result['changed_files']), ('repaired', 1))
        self.assert_bundle()

    def test_memory_is_keyed_to_the_desktop_executable_and_profile_home(self):
        with self.settled():
            self.remembered()
            with patch.object(bundle, '_inventory', wraps=bundle._inventory) as inventory:
                self.executable.write_bytes(b'updated desktop')
                self.assertEqual(self.ensure()['state'], 'ready')
                self.assertTrue(inventory.called)
                inventory.reset_mock()
                self.ensure()
                inventory.assert_not_called()
                self.home = self.root / 'other-profile'
                self.assertEqual(self.ensure()['state'], 'prepared')
                self.assertTrue(inventory.called)

    def test_fresh_stamps_are_never_remembered(self):
        self.executable.write_bytes(b'desktop')
        self.ensure()
        with patch.object(publication, '_now_ns', return_value=0), \
             patch.object(bundle, '_inventory', wraps=bundle._inventory) as inventory:
            self.ensure()
            inventory.reset_mock()
            self.assertEqual(self.ensure()['state'], 'ready')
            self.assertTrue(inventory.called)

    def test_junction_added_after_verification_is_rejected_not_remembered(self):
        if os.name != 'nt':
            self.skipTest('junctions are Windows-only')
        import _winapi
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'keep.js').write_bytes(b'untouched')
        with self.settled():
            self.remembered()
            time.sleep(.05)  # a later clock tick for the folder mtime
            _winapi.CreateJunction(str(outside), str(self.target / 'scripts/linked'))
            with self.assertRaisesRegex(ValueError, '링크'):
                self.ensure()
        self.assertEqual((outside / 'keep.js').read_bytes(), b'untouched')

    def test_added_or_removed_folder_entry_forces_the_full_check(self):
        with self.settled():
            self.remembered()
            extra = self.target / 'node_modules/dependency/extra.js'
            for change in (lambda: extra.write_bytes(b'unowned'), extra.unlink):
                time.sleep(.05)  # a later clock tick for the folder mtime
                change()
                with patch.object(bundle, '_inventory', wraps=bundle._inventory) as inventory:
                    self.assertEqual(self.ensure()['state'], 'ready')
                    self.assertTrue(inventory.called)
                    inventory.reset_mock()
                    self.assertEqual(self.ensure()['state'], 'ready')  # remembered again
                    inventory.assert_not_called()

    def test_recently_changed_folder_is_not_remembered(self):
        with self.settled():
            self.executable.write_bytes(b'desktop')
            self.ensure()
            future = time.time_ns() + 10_000_000_000
            os.utime(self.target / 'scripts', ns=(future, future))
            with patch.object(bundle, '_inventory', wraps=bundle._inventory) as inventory:
                self.ensure()
                inventory.reset_mock()
                self.assertEqual(self.ensure()['state'], 'ready')
                self.assertTrue(inventory.called)

    def test_browser_scan_keeps_the_shared_desktop_digests(self):
        desktop = self.executable.parent / 'resources/app.asar'
        desktop.write_bytes(b'desktop archive')
        publication._hashes.clear()
        bundle._digests.clear()
        self.addCleanup(publication._hashes.clear)
        with self.settled():
            publication._hash(desktop)
            shared = list(publication._hashes)
            self.ensure()
            self.ensure()
        self.assertEqual(list(publication._hashes), shared)
        self.assertGreaterEqual(len(bundle._digests), 2 * len(self.files))

    def test_recovery_skips_reused_pid_and_uses_actual_desktop_for_valid_identity(self):
        store = Store(self.root)
        profile = store.add_profile('fixture')
        executable = self.root / 'artifacts/managed-desktop/fixture/ChatGPT.exe'
        identity = dict(process_id=123, process_created=456, executable_path=str(executable))
        store.mutate(lambda state: store.profile(profile['id'], state).update(identity))
        with patch('repair_browser_plugins.process_identity', return_value={**identity, 'process_created':789}), \
                patch('repair_browser_plugins.ensure') as ensure:
            self.assertEqual(repair(self.root)['profiles'][0]['state'], 'not_running')
            ensure.assert_not_called()
        with patch('repair_browser_plugins.process_identity', return_value=identity), \
                patch('repair_browser_plugins.ensure', return_value=dict(state='ready')) as ensure:
            self.assertEqual(repair(self.root)['processes_restarted'], 0)
            ensure.assert_called_once_with(Path(profile['home']), executable)


if __name__ == '__main__':
    unittest.main()
