"""Simulated process/window delays: never launch a desktop or contact SSH."""
from contextlib import contextmanager
from pathlib import Path
import json
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.instances import Instances
from manager_core.launch_metrics import LaunchMetrics
from manager_core.store import Store


class LaunchConcurrencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name))
        self.instance = Instances(Path(temporary.name), self.store, None)
        self.profiles = [self.store.add_profile(str(i)) for i in range(2)]
        self.identity = dict(process_id=123, process_created=456, executable_path='fixture.exe')

    def result(self, profile, pid=123):
        identity = {**self.identity, 'process_id': pid}
        self.store.mutate(lambda data: self.store.profile(profile['id'], data).update(
            **identity, generation='fixture-generation', status='running'))
        self.instance.processes[profile['id']] = Mock(pid=pid)
        self.instance.processes[profile['id']].poll.return_value = None
        return dict(state='launched', profile_id=profile['id'],
                    profile=self.store.profile(profile['id']), preparation={})

    def test_window_wait_releases_global_fence_but_tracks_active_launch(self):
        admission = threading.Lock()
        @contextmanager
        def gate(_):
            with admission:
                yield
        self.instance.launch_admission = gate
        waiting, release, second_spawned = threading.Event(), threading.Event(), threading.Event()
        results, failures = [], []
        def spawn(profile_id, **_):
            profile = next(p for p in self.profiles if p['id'] == profile_id)
            if profile is self.profiles[1]:
                second_spawned.set()
            return self.result(profile, 123 if profile is self.profiles[0] else 124)
        def window(pid):
            if pid == 123:
                waiting.set()
                if not release.wait(3):
                    raise TimeoutError('fixture window not released')
            return pid+1000
        def run(profile):
            try:
                results.append(self.instance.show(profile['id']))
            except Exception as error:
                failures.append(error)
        threads = [threading.Thread(target=run, args=(p,)) for p in self.profiles]
        with patch.object(self.instance, '_show', side_effect=spawn), \
             patch.object(self.instance, 'observe', side_effect=lambda p: dict(status='running', window_handle=p.get('window_handle'))), \
             patch('manager_core.instances.main_window', side_effect=window), \
             patch('manager_core.instances.process_identity', side_effect=lambda pid: {**self.identity, 'process_id': pid}):
            try:
                threads[0].start()
                self.assertTrue(waiting.wait(2))
                self.assertEqual(self.instance.launch_status()['active'], 1)
                threads[1].start()
                self.assertTrue(second_spawned.wait(2), 'another profile waited for the first window')
                threads[1].join(2)
                self.assertFalse(threads[1].is_alive())
                self.assertFalse(release.is_set())
            finally:
                release.set()
                for thread in threads:
                    if thread.ident is not None:
                        thread.join(3)
        self.assertEqual(failures, [])
        self.assertEqual([r['state'] for r in results], ['launched', 'launched'])
        self.assertEqual(self.instance.launch_status()['active'], 0)

    def test_late_window_cannot_overwrite_replacement_generation(self):
        result = self.result(self.profiles[0])
        def window(_):
            self.store.mutate(lambda data: self.store.profile(result['profile_id'], data).update(
                generation='replacement', window_handle=999))
            return 111
        with patch('manager_core.instances.main_window', side_effect=window), \
             patch('manager_core.instances.process_identity', return_value=self.identity):
            self.assertEqual(self.instance.finish_show(result)['state'], 'superseded')
        self.assertEqual(self.store.profile(result['profile_id'])['window_handle'], 999)
        self.assertNotIn(result['profile_id'], self.instance.handles)

    def test_reused_pid_cannot_publish_window(self):
        result = self.result(self.profiles[0])
        with patch('manager_core.instances.main_window', return_value=111), \
             patch('manager_core.instances.process_identity', return_value={**self.identity, 'process_created': 999}), \
             patch.object(self.instance, 'observe', return_value={'status': 'not_started'}):
            self.instance.finish_show(result)
        self.assertFalse(self.store.profile(result['profile_id']).get('window_handle'))
        self.assertEqual(self.instance.handles, {})

    def test_stop_interrupts_window_poll_without_starting_another_process(self):
        result = self.result(self.profiles[0])
        self.instance.stop_launches()
        with patch('manager_core.instances.main_window') as window, \
             patch('manager_core.instances.process_identity', return_value=self.identity), \
             patch.object(self.instance, 'observe', return_value={'status': 'running'}):
            self.instance.finish_show(result)
        window.assert_not_called()

    def test_metrics_record_failure_without_error_text_and_rotate(self):
        metrics = LaunchMetrics(self.store.directory)
        with self.assertRaisesRegex(RuntimeError, 'private'):
            with metrics.phase(self.profiles[0]['id'], 'fixture'):
                raise RuntimeError('private diagnostic')
        event = json.loads(metrics.path.read_text())
        # A failure names only the exception class, never its message.
        self.assertEqual(set(event), {'at', 'profile_id', 'phase', 'elapsed_ms', 'success', 'error'})
        self.assertFalse(event['success'])
        self.assertEqual(event['error'], 'RuntimeError')
        self.assertNotIn('private', metrics.path.read_text())
        metrics.path.write_text('x'*(2*1024*1024))
        with metrics.phase(self.profiles[0]['id'], 'fixture'):
            pass
        self.assertTrue(metrics.path.with_suffix('.jsonl.1').exists())
        event = json.loads(metrics.path.read_text())
        self.assertTrue(event['success'])
        self.assertNotIn('error', event)


if __name__ == '__main__':
    unittest.main()
