"""Shutdown admission tests use fake launchers and never create native apps."""
from contextlib import contextmanager
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from control_center import ControlCenter
from manager_core.instances import Instances


class FakeInstances(Instances):
    def __init__(self, root):
        super().__init__(root, Mock(directory=root / 'work/control-center'), Mock(), embed_windows=True)
        self.calls = []
        self.launch = lambda _: dict(state='launched')

    def _show(self, profile_id, *, reopen_existing=True):
        self.calls.append((profile_id, reopen_existing))
        return self.launch(profile_id)

    def finish_show(self, result):
        return result


class LaunchDrainTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.instances = FakeInstances(Path(temporary.name))
        self.native = self.enterContext(patch('manager_core.instances.subprocess.Popen',
            side_effect=AssertionError('test must not launch a native app')))
        self.broker = self.enterContext(patch('manager_core.rust_service.launch',
            side_effect=AssertionError('test must not launch through the service')))

    def start_show(self):
        results, errors = [], []
        def run():
            try:
                results.append(self.instances.show('fixture', reopen_existing=False))
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=run)
        worker.start()
        return worker, results, errors

    def test_stop_tracks_admitted_launch_without_blocking_and_rejects_new_work(self):
        entered, release, responsive = threading.Event(), threading.Event(), threading.Event()
        def launch(_):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('test launch was not released')
            return dict(state='launched', profile=dict(status='running', window_handle=123))
        self.instances.launch = launch
        worker, results, errors = self.start_show()
        controls = threading.Thread(target=lambda: (self.instances.stop_launches(), responsive.set()))
        try:
            self.assertTrue(entered.wait(2))
            controls.start()
            self.assertTrue(responsive.wait(1), 'shutdown waited for launch completion')
            self.assertEqual(self.instances.launch_status(), dict(stopping=True, active=1))
            with self.assertRaises(RuntimeError):
                self.instances.show('late')
            self.assertEqual(self.instances.calls, [('fixture', False)])
            self.assertFalse(release.is_set())
        finally:
            release.set()
            worker.join(2)
            if controls.ident is not None:
                controls.join(2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(controls.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results[0]['profile']['window_handle'], 123)
        self.assertEqual(self.instances.launch_status(), dict(stopping=True, active=0))

    def test_launch_waiting_for_admission_remains_in_drain_count(self):
        entered, release = threading.Event(), threading.Event()
        @contextmanager
        def admission(_):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('test admission was not released')
            yield
        self.instances.launch_admission = admission
        self.instances.launch = lambda _: self.instances._require_launch_open()
        worker, results, errors = self.start_show()
        try:
            self.assertTrue(entered.wait(2))
            self.assertEqual(self.instances.stop_launches(), dict(stopping=True, active=1))
            self.assertEqual(self.instances.calls, [])
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)
        self.assertEqual(self.instances.launch_status(), dict(stopping=True, active=0))

    def test_failure_in_admission_or_launcher_always_finishes_drain(self):
        @contextmanager
        def fail_admission(_):
            raise ValueError('admission rejected')
            yield
        self.instances.launch_admission = fail_admission
        with self.assertRaisesRegex(ValueError, 'admission rejected'):
            self.instances.show('fixture')
        self.assertEqual(self.instances.launch_status()['active'], 0)
        self.assertEqual(self.instances.calls, [])
        self.instances.launch_admission = None
        self.instances.launch = Mock(side_effect=ValueError('launch failed'))
        with self.assertRaisesRegex(ValueError, 'launch failed'):
            self.instances.show('fixture')
        self.assertEqual(self.instances.launch_status()['active'], 0)

    def test_failed_close_can_resume_launch_admission(self):
        self.instances.stop_launches()
        with self.assertRaises(RuntimeError):
            self.instances.show('fixture')
        self.instances.resume_launches()
        self.assertEqual(self.instances.show('fixture', reopen_existing=False), dict(state='launched'))
        self.assertEqual(self.instances.launch_status(), dict(stopping=False, active=0))
        self.assertEqual(self.instances.calls, [('fixture', False)])
        snapshot = self.instances.launch_status()
        snapshot['active'] = 99
        self.assertEqual(self.instances.launch_status()['active'], 0)

    def test_running_profile_lookup_remains_read_only_during_drain(self):
        profile = dict(id='fixture', generation='current')
        self.instances.store.profile.return_value = profile
        self.instances.store.read.return_value = {}
        self.instances.observe = Mock(return_value=dict(status='running', window_handle=123))
        center = ControlCenter.__new__(ControlCenter)
        center.store, center.instances = self.instances.store, self.instances
        center.remote_maintenance = Mock()
        self.instances.stop_launches()
        result = center._open_profile_locally('fixture')
        self.assertEqual(result['state'], 'existing')
        self.assertEqual(result['profile']['window_handle'], 123)
        self.assertEqual(self.instances.calls, [])
        center.remote_maintenance.pending_on_open.assert_not_called()
        self.native.assert_not_called()
        self.broker.assert_not_called()

    def test_stop_during_reopen_preparation_prevents_native_launch(self):
        self.instances._show = Instances._show.__get__(self.instances)
        self.instances.store.profile.return_value = dict(id='fixture', ui_home='unused', home=str(self.instances.root))
        self.instances.observe = Mock(return_value=dict(status='running', executable_path='unused'))
        environment = dict(fixture='temporary')
        def prepare_environment(_):
            self.instances.stop_launches()
            return environment
        self.instances.environment = prepare_environment
        with self.assertRaises(RuntimeError):
            self.instances.show('fixture')
        self.native.assert_not_called()
        self.broker.assert_not_called()
        self.assertEqual(environment, {})
        self.assertEqual(self.instances.launch_status(), dict(stopping=True, active=0))

    def test_stop_during_new_window_preparation_prevents_native_launch(self):
        self.instances._show = Instances._show.__get__(self.instances)
        self.instances.store.profile.return_value = dict(id='fixture', home=str(self.instances.root))
        self.instances.observe = Mock(return_value=dict(status='not_started'))
        self.instances.prepare = Mock(return_value={})
        environment = dict(CODEX_CLI_PATH='unused')
        def prepare_environment(_):
            self.instances.stop_launches()
            return environment
        self.instances.environment = prepare_environment
        with patch('manager_core.login_health.require'), \
             patch('desktop_launch.find_app', return_value={}), \
             patch('manager_core.desktop_bundle.prepare', return_value={'executable': 'unused'}), \
             patch('manager_core.release_code.runtime_revision', return_value='fixture'), \
             patch('manager_core.app_catalog_cache.prepare', return_value={}):
            with self.assertRaises(RuntimeError):
                self.instances.show('fixture')
        self.native.assert_not_called()
        self.broker.assert_not_called()
        self.assertEqual(environment, {})
        self.assertEqual(self.instances.launch_status(), dict(stopping=True, active=0))


if __name__ == '__main__':
    unittest.main()
