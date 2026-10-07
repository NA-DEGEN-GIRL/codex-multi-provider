"""Exercise exact-version Browser publication without launching an app or browser."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
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

    def assert_no_staging(self):
        for folder in (self.parent, self.home / 'plugins/cache'):
            self.assertEqual(list(folder.glob('.manager-stage-*')), [])

    def join_background(self):
        for thread in threading.enumerate():
            if thread.name == 'browser-bundle-repair':
                thread.join(10)
                self.assertFalse(thread.is_alive())

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
        self.assert_no_staging()
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
        self.join_background()
        target = Path(profile['home']) / 'plugins/cache/openai-bundled/browser' / self.version
        self.assertEqual(result['state'], 'existing')
        self.assertEqual((target / 'scripts/browser-service.mjs').read_bytes(), self.files['scripts/browser-service.mjs'])
        center.instances.show.assert_not_called()
        args, kwargs = center.instances.metrics.record.call_args
        self.assertEqual((args[0], args[1], args[3], kwargs), (profile['id'], 'browser_bundle_background', True, dict(error=None)))

    def test_selecting_running_profile_never_waits_for_or_fails_on_its_repair(self):
        from control_center import ControlCenter
        center = ControlCenter.__new__(ControlCenter)
        center.store = Store(self.root)
        profile = center.store.add_profile('fixture')
        center.instances = Mock()
        center.instances.observe.return_value = dict(status='running', executable_path=str(self.executable))
        started, release = threading.Event(), threading.Event()
        def failing(home, executable):
            started.set()
            release.wait(10)
            raise ValueError('fixture failure at ' + str(home))
        with patch.object(bundle, 'ensure', side_effect=failing):
            self.assertEqual(center._open_profile_locally(profile['id'])['state'], 'existing')
            self.assertTrue(started.wait(10))  # still running after the request returned
            release.set()
            self.join_background()
        args, kwargs = center.instances.metrics.record.call_args
        self.assertEqual((args[1], args[3], kwargs), ('browser_bundle_background', False, dict(error='ValueError')))

    def test_showing_a_running_profile_does_not_wait_for_its_browser_repair(self):
        from manager_core.instances import Instances
        store = Store(self.root)
        profile = store.add_profile('fixture')
        instances = Instances(self.root, store, None)
        active = dict(status='running', window_handle=123, executable_path=str(self.executable))
        started, release = threading.Event(), threading.Event()
        def slow(home, executable):
            started.set()
            release.wait(10)
            return dict(state='ready')
        with patch.object(instances, 'observe', return_value=active), \
                patch.object(bundle, 'ensure', side_effect=slow) as ensure:
            self.assertEqual(instances.show(profile['id'], reopen_existing=False)['state'], 'existing')
            self.assertTrue(started.wait(10))
            release.set()
            self.join_background()
        ensure.assert_called_once_with(profile['home'], str(self.executable))

    def test_one_background_job_per_home_reruns_once_for_requests_while_it_runs(self):
        started, release, calls = threading.Event(), threading.Event(), []
        def slow(home, executable):
            calls.append(home)
            started.set()
            release.wait(10)
            return dict(state='repaired', changed_files=1)  # recorded in the metrics
        metrics = Mock()
        other = self.root / 'other-profile'
        with patch.object(bundle, 'ensure', side_effect=slow):
            first = bundle.ensure_later(self.home, self.executable, metrics=metrics, profile_id='first')
            self.assertIsNotNone(first)
            self.assertTrue(started.wait(10))
            for profile_id in ('queued', 'newest'):
                self.assertIsNone(bundle.ensure_later(self.home, self.executable, metrics=metrics, profile_id=profile_id))
            # Another home has its own job.
            second = bundle.ensure_later(other, self.executable, metrics=metrics, profile_id='other')
            self.assertIsNotNone(second)
            release.set()
            first.join(10)
            second.join(10)
        self.assertEqual(sorted(map(str, calls)), sorted(map(str, [self.home, other, self.home])))
        self.assertEqual(sorted(call.args[0] for call in metrics.record.call_args_list), ['first', 'newest', 'other'])
        self.assertFalse({os.path.normcase(os.path.abspath(path)) for path in (self.home, other)} & set(bundle._jobs))

    def test_desktop_without_browser_starts_no_background_job(self):
        with patch.object(bundle, 'ensure', side_effect=AssertionError('not bundled')):
            self.assertIsNone(bundle.ensure_later(self.home, self.root / 'older/ChatGPT.exe'))
        self.assertFalse(self.home.exists())

    def test_background_pass_is_recorded_only_if_it_failed_changed_files_or_was_slow(self):
        self.ensure()
        metrics = Mock()
        def run():
            thread = bundle.ensure_later(self.home, self.executable, metrics=metrics, profile_id='fixture')
            thread.join(10)
            self.assertFalse(thread.is_alive())
        with patch.object(bundle, '_SLOW_PASS_SECONDS', 60):
            run()  # a quick pass that changed nothing, as on most selections
            metrics.record.assert_not_called()
            (self.target / 'scripts/browser-service.mjs').unlink()
            run()
            self.assertEqual(metrics.record.call_count, 1)  # the repair
        with patch.object(bundle, '_SLOW_PASS_SECONDS', 0):
            run()
        self.assertEqual(metrics.record.call_count, 2)  # a slow pass, though nothing changed
        self.assertEqual([call.args[1] for call in metrics.record.call_args_list], ['browser_bundle_background'] * 2)
        self.assert_bundle()

    def test_launch_settles_the_homes_background_job_and_drops_its_queued_pass(self):
        started, release, calls = threading.Event(), threading.Event(), []
        def slow(home, executable):
            calls.append(executable)
            started.set()
            release.wait(10)
            return dict(state='ready')
        with patch.object(bundle, 'ensure', side_effect=slow):
            thread = bundle.ensure_later(self.home, self.executable)
            self.assertTrue(started.wait(10))
            self.assertIsNone(bundle.ensure_later(self.home, self.executable, profile_id='queued'))
            self.assertFalse(bundle.settle(self.home, timeout=.1))  # still holds the lock
            timer = threading.Timer(.2, release.set)
            timer.start()
            self.addCleanup(timer.cancel)
            self.assertTrue(bundle.settle(self.home, timeout=10))
            self.assertNotIn(os.path.normcase(os.path.abspath(self.home)), bundle._jobs)
            thread.join(10)
        self.assertEqual(len(calls), 1)  # the launch prepares in place instead
        self.assertTrue(bundle.settle(self.home, timeout=0))  # no job: no wait

    def test_launch_waits_for_a_background_check_holding_its_browser_lock(self):
        from manager_core.instances import Instances
        store = Store(self.root)
        profile = store.add_profile('fixture')
        home = Path(profile['home'])
        parent = home / 'plugins/cache/openai-bundled/browser'
        parent.mkdir(parents=True)
        instances = Instances(self.root, store, None)
        entered, release, seen, real = threading.Event(), threading.Event(), [], bundle.ensure
        def tracked(home, executable):
            if threading.current_thread().name == 'browser-bundle-repair':
                with bundle._locked(bundle._plain(parent)):
                    entered.set()
                    release.wait(10)  # a full check or repair holding the lock
            else:
                seen.append(os.path.normcase(os.path.abspath(home)) in bundle._jobs)
            return real(home, executable)
        class Stop(Exception):
            pass
        app = dict(executable=str(self.executable), Version='26.900.1.0', desktop_isolation_revision='fixture')
        with patch.object(bundle, 'ensure', side_effect=tracked), \
                patch.object(instances, 'observe', return_value=dict(status='stopped')), \
                patch.object(instances, 'prepare', return_value={}), \
                patch.object(instances, 'installed_app', return_value=dict(app)), \
                patch.object(instances, 'environment', side_effect=Stop), \
                patch('manager_core.desktop_bundle.prepare', return_value=dict(app)), \
                patch('manager_core.runtime_build.resolve', return_value=dict(capabilities={})), \
                patch('manager_core.login_health.require'):
            self.assertIsNotNone(bundle.ensure_later(home, self.executable))
            self.assertTrue(entered.wait(10))
            timer = threading.Timer(.3, release.set)
            timer.start()
            self.addCleanup(timer.cancel)
            with self.assertRaises(Stop):  # stopped right after the Browser phase
                instances._show(profile['id'])
            self.join_background()
        self.assertEqual(seen, [False])  # the launch's own check started after the job ended
        self.assertTrue((parent / self.version / 'scripts/browser-service.mjs').is_file())

    def test_job_without_a_thread_is_ended_for_the_next_request_and_launch(self):
        with patch.object(threading.Thread, 'start', side_effect=RuntimeError("can't start new thread")):
            self.assertIsNone(bundle.ensure_later(self.home, self.executable))
        self.assertNotIn(os.path.normcase(os.path.abspath(self.home)), bundle._jobs)
        self.assertTrue(bundle.settle(self.home, timeout=0))
        thread = bundle.ensure_later(self.home, self.executable)
        self.assertIsNotNone(thread)
        thread.join(10)
        self.assert_bundle()

    def test_stale_staging_folders_are_swept_under_the_browser_lock(self):
        cache = self.home / 'plugins/cache'
        stale = [cache / ('.manager-stage-' + 'a' * 32), self.parent / ('.manager-stage-' + 'b' * 32)]
        for folder in stale:  # an interrupted job, and an older manager's stage in the plugin folder
            (folder / 'scripts').mkdir(parents=True)
            (folder / 'scripts/browser-service.mjs').write_bytes(b'partial')
        kept = [cache / '.manager-stage-not-ours', self.parent / '26.100.1']
        for folder in kept:
            folder.mkdir(parents=True)
            (folder / 'keep').write_bytes(b'kept')
        self.assertEqual(self.ensure()['state'], 'prepared')
        self.assertFalse(any(folder.exists() for folder in stale))
        self.assertTrue(all((folder / 'keep').read_bytes() == b'kept' for folder in kept))
        self.assert_bundle()

    def test_stale_staging_folder_with_a_link_inside_is_left_alone(self):
        if os.name != 'nt':
            self.skipTest('junctions are Windows-only')
        import _winapi
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'keep.js').write_bytes(b'untouched')
        stale = self.home / 'plugins/cache' / ('.manager-stage-' + 'c' * 32)
        stale.mkdir(parents=True)
        _winapi.CreateJunction(str(outside), str(stale / 'linked'))
        self.assertEqual(self.ensure()['state'], 'prepared')  # never blocks the call
        self.assertTrue((stale / 'linked').exists())
        self.assertEqual((outside / 'keep.js').read_bytes(), b'untouched')
        os.rmdir(stale / 'linked')  # the junction only, before the fixture cleanup

    def test_listing_a_just_changed_folder_rejects_a_link(self):
        if os.name != 'nt':
            self.skipTest('junctions are Windows-only')
        import _winapi
        self.ensure()
        folders = {Path(): None, Path('scripts'): None}
        self.assertTrue(bundle._listed(self.target, Path('scripts'), folders))
        outside = self.root / 'outside'
        outside.mkdir()
        _winapi.CreateJunction(str(outside), str(self.target / 'scripts/linked'))
        self.assertFalse(bundle._listed(self.target, Path('scripts'), folders))
        os.rmdir(self.target / 'scripts/linked')

    def test_repair_stages_only_changed_files_outside_the_plugin_folder(self):
        copied, real_copy = [], bundle.shutil.copyfile
        def copy(source, destination):
            copied.append(Path(destination))
            return real_copy(source, destination)
        with patch.object(bundle.shutil, 'copyfile', side_effect=copy):
            self.assertEqual(self.ensure()['state'], 'prepared')
            self.assertEqual(len(copied), len(self.files))  # a new version is staged complete
            copied.clear()
            (self.target / 'scripts/browser-service.mjs').unlink()
            result = self.ensure()
        self.assertEqual((result['state'], result['changed_files']), ('repaired', 1))
        self.assertEqual(len(copied), 1)
        stage = copied[0].parents[1]
        self.assertTrue(stage.name.startswith('.manager-stage-'))
        self.assertEqual(stage.parent, bundle._plain(self.home / 'plugins/cache'))
        self.assert_no_staging()
        self.assert_bundle()

    def test_file_lost_after_staging_is_not_replaced_from_a_partial_stage(self):
        self.ensure()
        service, client = self.target / 'scripts/browser-service.mjs', self.target / 'scripts/browser-client.mjs'
        service.unlink()
        real_copy = bundle.shutil.copyfile
        def copy(source, destination):
            real_copy(source, destination)
            client.unlink()  # after the first check, so it was not staged
        with patch.object(bundle.shutil, 'copyfile', side_effect=copy):
            with self.assertRaises(OSError):
                self.ensure()
        self.assertFalse(service.exists())
        self.assert_no_staging()
        self.assertEqual(self.ensure()['changed_files'], 2)
        self.assert_bundle()

    def test_version_removed_during_a_repair_is_not_published_from_a_partial_stage(self):
        self.ensure()
        (self.target / 'scripts/browser-service.mjs').unlink()
        real_copy = bundle.shutil.copyfile
        def copy(source, destination):
            real_copy(source, destination)
            shutil.rmtree(self.target)
        with patch.object(bundle.shutil, 'copyfile', side_effect=copy):
            with self.assertRaises(OSError):
                self.ensure()
        self.assertFalse(self.target.exists())
        self.assert_no_staging()
        self.assertEqual(self.ensure()['state'], 'prepared')
        self.assert_bundle()

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

    def repaired_just_now(self):
        """Prepare the bundle, then repair one file in a later clock tick.

        From then on, stamps changed after the preparation count as too recent
        to prove a later edit; the earlier files count as settled.
        """
        self.executable.write_bytes(b'desktop')
        self.assertEqual(self.ensure()['state'], 'prepared')
        self.settled_before = time.time_ns()
        time.sleep(.05)  # a later clock tick for the repaired file and its folder
        def eligible(stamp, now_ns=None):
            changed = publication._change_time_ns(stamp)
            return changed is not None and changed < self.settled_before
        recent = patch.object(bundle, '_cache_eligible', side_effect=eligible)
        recent.start()
        self.addCleanup(recent.stop)
        (self.target / 'scripts/browser-service.mjs').unlink()
        self.assertEqual(self.ensure()['state'], 'repaired')

    def test_repair_is_remembered_and_only_what_it_just_changed_is_read_again(self):
        self.repaired_just_now()
        with patch.object(bundle, '_inventory', side_effect=AssertionError('full check')), \
                patch.object(bundle, '_hash', wraps=bundle._hash) as hashed:
            self.assertEqual(self.ensure(), dict(state='ready', version=self.version,
                                                 files=len(self.files), changed_files=0))
        self.assertEqual([Path(call.args[0]).name for call in hashed.call_args_list], ['browser-service.mjs'])
        self.settled_before = time.time_ns() + 10_000_000_000  # every stamp has settled
        with self.settled(), patch.object(bundle, '_inventory', side_effect=AssertionError('full check')):
            with patch.object(bundle, '_hash', wraps=bundle._hash) as hashed:
                self.assertEqual(self.ensure()['state'], 'ready')  # proven once more, now settled
                self.assertEqual(hashed.call_count, 1)
            with patch.object(bundle, '_hash', side_effect=AssertionError('read again')):
                self.assertEqual(self.ensure()['state'], 'ready')
        self.assert_bundle()

    def test_same_tick_rewrite_of_a_repaired_file_is_found_by_its_content(self):
        self.repaired_just_now()
        service = self.target / 'scripts/browser-service.mjs'
        with service.open('rb') as stream:
            frozen = bundle._content_stamp(stream)
        before = service.stat()
        service.write_bytes(b'x' * before.st_size)
        os.utime(service, ns=(before.st_atime_ns, before.st_mtime_ns))
        real, suffix = bundle._content_stamp, os.path.normcase(os.path.join('scripts', 'browser-service.mjs'))
        def stamp(stream):  # a rewrite inside one clock tick keeps every stamp field
            return frozen if os.path.normcase(stream.name).endswith(suffix) else real(stream)
        with patch.object(bundle, '_content_stamp', side_effect=stamp):
            self.assertEqual(self.ensure()['state'], 'repaired')
        self.assert_bundle()

    def test_entry_added_to_a_just_changed_folder_is_found_by_listing(self):
        self.repaired_just_now()
        scripts = self.target / 'scripts'
        before = scripts.stat()
        (scripts / 'added').mkdir()
        os.utime(scripts, ns=(before.st_atime_ns, before.st_mtime_ns))  # as if inside one clock tick
        self.assertEqual((scripts.stat().st_ino, scripts.stat().st_mtime_ns), (before.st_ino, before.st_mtime_ns))
        with patch.object(bundle, '_inventory', wraps=bundle._inventory) as inventory:
            self.assertEqual(self.ensure()['state'], 'ready')
            self.assertTrue(inventory.called)

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
