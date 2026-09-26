"""A launch that fails after spawning stops its own desktop; fixture brokers only, no apps."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.instances import Instances
from manager_core.store import Store


@unittest.skipUnless(os.name == 'nt', 'Windows process broker')
class LaunchAbortTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name))
        self.profile = self.store.add_profile('fixture')
        self.instances = Instances(Path(temporary.name), self.store, None)
        self.instances.observe = Mock(return_value=dict(status='not_started'))
        self.instances.prepare = Mock(return_value={})
        self.instances.environment = Mock(side_effect=lambda _: dict(CODEX_CLI_PATH='unused'))
        self.instances.installed_app = Mock(return_value=dict(executable='installed.exe', Version='1'))
        self.identity = dict(process_id=4321, process_created=99, executable_path='fixture.exe')
        self.replies = {'process.launch': self.identity, 'process.identity': self.identity,
                        'process.abort_launch': dict(state='stopped')}
        self.requests = []

        def request(command, **args):
            self.requests.append((command, args))
            reply = self.replies[command]
            if isinstance(reply, BaseException):
                raise reply
            return None if reply is None else dict(reply)

        app = dict(executable='fixture.exe', Version='1', desktop_isolation_revision=1)
        for fixture in (
                patch('manager_core.login_health.require'),
                patch('manager_core.desktop_bundle.prepare', return_value=app),
                patch('manager_core.browser_bundle.ensure', return_value={}),
                patch('manager_core.release_code.runtime_revision', return_value='fixture'),
                patch('manager_core.app_catalog_cache.prepare', return_value={}),
                patch('manager_core.rust_service.enabled', return_value=True),
                patch('manager_core.rust_service.request', side_effect=request),
                patch('subprocess.Popen', side_effect=AssertionError('fixture must not launch an app'))):
            self.enterContext(fixture)

    def fail_save(self, error):
        original = self.store.mutate

        def mutate(operation):
            if operation.__name__ == 'save':
                raise error
            return original(operation)

        return patch.object(self.store, 'mutate', side_effect=mutate)

    def show(self):
        return self.instances.show(self.profile['id'], wait_for_window=False)

    def commands(self):
        return [command for command, _ in self.requests]

    def metrics(self):
        lines = self.instances.metrics.path.read_text(encoding='utf-8').splitlines()
        return {event['phase']: event for event in map(json.loads, lines)}

    def assert_unrecorded(self):
        saved = self.store.profile(self.profile['id'])
        self.assertIsNone(saved.get('process_id'))
        self.assertNotIn('generation', saved)
        self.assertNotIn(self.profile['id'], self.instances.processes)

    def assert_aborted_this_launch(self):
        self.assertEqual(self.commands(), ['process.launch', 'process.identity', 'process.abort_launch'])
        launched, aborted = self.requests[0][1], self.requests[2][1]
        self.assertEqual(aborted, dict(profile_id=self.profile['id'], generation=launched['generation'],
                                       process_id=4321, process_created=99))

    def test_failed_identity_save_stops_the_spawned_launch_and_keeps_the_error(self):
        with self.fail_save(RuntimeError('fixture store busy')), \
                self.assertRaisesRegex(RuntimeError, 'fixture store busy'):
            self.show()
        self.assert_aborted_this_launch()
        self.assert_unrecorded()
        events = self.metrics()
        self.assertEqual((events['prepare_and_spawn']['success'], events['prepare_and_spawn']['error']),
                         (False, 'RuntimeError'))
        self.assertTrue(events['launch_abort']['success'])
        self.assertNotIn('fixture store busy', self.instances.metrics.path.read_text(encoding='utf-8'))

    def test_broker_failure_right_after_spawn_stops_the_launch(self):
        self.replies['process.identity'] = RuntimeError('Rust 관리 서비스에 연결하지 못했습니다.')
        with self.assertRaisesRegex(RuntimeError, '연결하지 못했습니다'):
            self.show()
        self.assert_aborted_this_launch()
        self.assert_unrecorded()

    def test_exited_launch_is_still_aborted_by_its_own_identity(self):
        self.replies['process.identity'] = None
        with self.assertRaisesRegex(RuntimeError, '인스턴스 시작을 확인하지 못했습니다'):
            self.show()
        self.assert_aborted_this_launch()
        self.assert_unrecorded()

    def test_failed_abort_never_replaces_the_launch_error(self):
        self.replies['process.abort_launch'] = RuntimeError('fixture abort refused')
        with self.fail_save(ValueError('fixture profile changed')), \
                self.assertRaisesRegex(ValueError, 'fixture profile changed'):
            self.show()
        self.assert_aborted_this_launch()
        events = self.metrics()
        self.assertEqual((events['launch_abort']['success'], events['launch_abort']['error']),
                         (False, 'RuntimeError'))
        self.assertEqual(events['prepare_and_spawn']['error'], 'ValueError')

    def test_saved_launch_is_not_aborted(self):
        result = self.show()
        self.assertEqual(result['state'], 'launched')
        self.assertEqual(self.commands(), ['process.launch', 'process.identity'])
        saved = self.store.profile(self.profile['id'])
        self.assertEqual((saved['process_id'], saved['generation']),
                         (4321, self.requests[0][1]['generation']))
        self.assertNotIn('launch_abort', self.metrics())

    def test_direct_launch_kills_only_its_own_process(self):
        process = Mock(pid=4321)
        with patch('manager_core.rust_service.enabled', return_value=False), \
                patch('manager_core.instances.subprocess.Popen', return_value=process), \
                patch('manager_core.instances.process_identity', return_value=self.identity), \
                self.fail_save(RuntimeError('fixture store busy')), \
                self.assertRaisesRegex(RuntimeError, 'fixture store busy'):
            self.show()
        process.kill.assert_called_once_with()
        self.assertEqual(self.requests, [])
        self.assert_unrecorded()


if __name__ == '__main__':
    unittest.main()
