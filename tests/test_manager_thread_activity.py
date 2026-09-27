"""Per-thread working/waiting state for the task shortcut list."""
import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from manager_core.app_transport import RuntimeObserver
from manager_core import thread_activity as activity


def notify(observer, method, params):
    observer.consume('server', {'method': method, 'params': params})


class RuntimeObserverActivityTests(unittest.TestCase):
    def setUp(self):
        self.observer = RuntimeObserver(str(uuid4()))
        self.thread = str(uuid4())

    def test_status_flags_map_to_display_states(self):
        notify(self.observer, 'thread/status/changed', {'threadId': self.thread, 'status': {'type': 'active', 'activeFlags': []}})
        self.assertEqual(self.observer.thread_activity(), {self.thread: 'working'})
        notify(self.observer, 'thread/status/changed',
               {'threadId': self.thread, 'status': {'type': 'active', 'activeFlags': ['waitingOnApproval']}})
        self.assertEqual(self.observer.snapshot()['thread_activity'], {self.thread: 'waiting_approval'})
        notify(self.observer, 'thread/status/changed',
               {'threadId': self.thread, 'status': {'type': 'active', 'activeFlags': ['waitingOnUserInput']}})
        self.assertEqual(self.observer.thread_activity(), {self.thread: 'waiting_input'})
        notify(self.observer, 'thread/status/changed', {'threadId': self.thread, 'status': {'type': 'idle'}})
        self.assertEqual(self.observer.thread_activity(), {})

    def test_started_turn_counts_as_working_until_completed(self):
        turn = {'id': str(uuid4()), 'status': 'inProgress'}
        notify(self.observer, 'turn/started', {'threadId': self.thread, 'turn': turn})
        self.assertEqual(self.observer.thread_activity(), {self.thread: 'working'})
        notify(self.observer, 'turn/completed', {'threadId': self.thread, 'turn': {**turn, 'status': 'completed'}})
        self.assertEqual(self.observer.thread_activity(), {})


class ActivityFileTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.clock = [100.0]
        self.wall = [1_000_000.0]

    def file(self, name, generation='g1'):
        return activity.ActivityFile(self.directory / name, generation=generation,
                                     clock=lambda: self.clock[0], wall=lambda: self.wall[0])

    def test_fresh_files_of_this_generation_merge_with_strongest_state(self):
        a, b = str(uuid4()), str(uuid4())
        self.file('ssh-activity-hp-1.json').publish({a: 'working', b: 'working'})
        self.file('ssh-activity-remote-dev-2.json').publish({a: 'waiting_approval'})
        self.file('ssh-activity-old-3.json', generation='g0').publish({b: 'waiting_input'})
        merged = activity.read_ssh(self.directory, 'g1', wall=lambda: self.wall[0] + 5)
        self.assertEqual(merged, {a: 'waiting_approval', b: 'working'})

    def test_stale_or_closed_writers_never_look_busy(self):
        thread = str(uuid4())
        writer = self.file('ssh-activity-hp-1.json')
        writer.publish({thread: 'working'})
        self.assertEqual(activity.read_ssh(self.directory, 'g1', wall=lambda: self.wall[0] + activity.MAX_AGE_SECONDS + 1), {})
        writer.close()
        self.assertEqual(activity.read_ssh(self.directory, 'g1', wall=lambda: self.wall[0]), {})

    def test_unchanged_activity_is_rewritten_only_for_freshness(self):
        thread = str(uuid4())
        writer = self.file('ssh-activity-hp-1.json')
        writer.publish({thread: 'working'})
        first = json.loads(writer.path.read_text())['observed_at']
        self.wall[0] += 1; self.clock[0] += 1
        writer.publish({thread: 'working'})
        self.assertEqual(json.loads(writer.path.read_text())['observed_at'], first)
        self.wall[0] += activity.REFRESH_SECONDS; self.clock[0] += activity.REFRESH_SECONDS
        writer.publish({thread: 'working'})
        self.assertGreater(json.loads(writer.path.read_text())['observed_at'], first)
        writer.publish({thread: 'unknown-state', 'x': 'working'})
        self.assertEqual(json.loads(writer.path.read_text())['thread_activity'], {'x': 'working'})


if __name__ == '__main__':
    unittest.main()
