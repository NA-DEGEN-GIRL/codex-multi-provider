"""Service-wide package lookup: reused only while the installed package is unchanged."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import desktop_launch
from manager_core.instances import Instances


class PackageLookupCacheTests(unittest.TestCase):
    def setUp(self):
        desktop_launch.forget_app()
        self.addCleanup(desktop_launch.forget_app)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.location = self.root / 'OpenAI.Codex_1.2.3.0_x64__abcdefghijklm'
        self.executable = self.location / 'app/ChatGPT.exe'
        self.archive = self.location / 'app/resources/app.asar'
        self.archive.parent.mkdir(parents=True)
        self.executable.write_bytes(b'desktop')
        self.archive.write_bytes(b'archive')
        self.app = dict(Name='OpenAI.Codex', Version='1.2.3.0', InstallLocation=str(self.location),
                        executable=str(self.executable))
        self.registered = ('OpenAI.Codex_1.2.3.0_x64__abcdefghijklm',)
        registration = patch.object(desktop_launch, '_registered_packages', side_effect=lambda app: self.registered)
        registration.start()
        self.addCleanup(registration.stop)

    def lookup(self, **options):
        return patch('desktop_launch.find_app', return_value=dict(self.app), **options)

    def test_lookup_is_reused_across_callers_and_each_gets_a_copy(self):
        with self.lookup() as discover:
            first = desktop_launch.cached_app()
            first['Version'] = 'caller change'
            self.assertEqual(desktop_launch.cached_app(), self.app)
            self.assertEqual(discover.call_count, 1)

    def test_changed_archive_or_missing_executable_runs_the_lookup_again(self):
        with self.lookup() as discover:
            desktop_launch.cached_app()
            self.archive.write_bytes(b'updated archive')
            desktop_launch.cached_app()
            self.assertEqual(discover.call_count, 2)
            self.executable.unlink()
            discover.side_effect = RuntimeError('package removed')
            with self.assertRaisesRegex(RuntimeError, 'removed'):
                desktop_launch.cached_app()
            self.assertEqual(discover.call_count, 3)

    def test_registered_version_change_runs_the_lookup_again(self):
        # An update can leave the old version's folder in place for a while.
        with self.lookup() as discover:
            desktop_launch.cached_app()
            self.registered = ('OpenAI.Codex_1.2.4.0_x64__abcdefghijklm',)
            desktop_launch.cached_app()
            self.assertEqual(discover.call_count, 2)

    def test_lookup_is_refreshed_after_the_age_limit_or_when_forgotten(self):
        with self.lookup() as discover, patch('desktop_launch.time.monotonic', return_value=1000) as clock:
            desktop_launch.cached_app()
            clock.return_value = 1000 + desktop_launch.APP_CACHE_SECONDS - 1
            desktop_launch.cached_app()
            self.assertEqual(discover.call_count, 1)
            clock.return_value = 1000 + desktop_launch.APP_CACHE_SECONDS
            desktop_launch.cached_app()
            self.assertEqual(discover.call_count, 2)
            desktop_launch.forget_app()
            desktop_launch.cached_app()
            self.assertEqual(discover.call_count, 3)

    def test_package_without_archive_is_never_cached(self):
        self.archive.unlink()
        with self.lookup() as discover:
            desktop_launch.cached_app()
            desktop_launch.cached_app()
            self.assertEqual(discover.call_count, 2)

    def test_launch_services_share_one_lookup(self):
        first = Instances(self.root, Mock(directory=self.root / 'state'), Mock())
        second = Instances(self.root, Mock(directory=self.root / 'state'), Mock())
        with self.lookup() as discover:
            self.assertEqual(first.installed_app(), self.app)
            self.assertEqual(second.installed_app(), self.app)
            self.assertEqual(discover.call_count, 1)


class RegisteredPackageTests(unittest.TestCase):
    def app(self, folder, name='OpenAI.Codex', version='1.2.3.0'):
        return dict(Name=name, Version=version, InstallLocation=str(Path('C:/Program Files/WindowsApps') / folder))

    def test_only_standard_package_folders_are_queried(self):
        with patch.object(desktop_launch, '_package_query', side_effect=AssertionError('queried')):
            self.assertIsNone(desktop_launch._registered_packages(dict(InstallLocation='C:/dev/codex')))
            self.assertIsNone(desktop_launch._registered_packages(
                self.app('Other.App_1.2.3.0_x64__abcdefghijklm')))
            self.assertIsNone(desktop_launch._registered_packages(
                self.app('OpenAI.Codex_9.9.9.0_x64__abcdefghijklm')))

    @unittest.skipUnless(os.name == 'nt', 'package registration is Windows-only')
    def test_unregistered_family_reports_no_packages(self):
        self.assertEqual(desktop_launch._registered_packages(
            self.app('OpenAI.Codex_1.2.3.0_x64__zzzzzzzzzzzz0')), ())


if __name__ == '__main__':
    unittest.main()
