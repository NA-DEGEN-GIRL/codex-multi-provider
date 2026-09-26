"""The startup sampler counts working code locations and ignores waiting threads."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core import startup_profile


def busy_loop(stop):
    while not stop.is_set():
        sum(range(2000))


class StartupProfileTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_busy_threads_are_counted_and_waiting_threads_are_not(self):
        stop, idle = threading.Event(), threading.Event()
        workers = [threading.Thread(target=busy_loop, args=(stop,), name='busy-worker'),
                   threading.Thread(target=idle.wait, args=(5,), name='idle-worker')]
        for worker in workers:
            worker.start()
        self.addCleanup(lambda: (stop.set(), idle.set(), [w.join(2) for w in workers]))
        path = self.root / 'profile.json'
        startup_profile._run(path, .4, .01)
        report = json.loads(path.read_text(encoding='utf-8'))
        self.assertGreater(report['busy_samples'], 0)
        self.assertIn('busy-worker', report['threads'])
        self.assertNotIn('idle-worker', report['threads'])
        self.assertTrue(any('busy_loop' in row['where'] for row in report['top_functions']))
        self.assertEqual(set(report), {'version', 'unit', 'started_at', 'duration_s', 'interval_ms', 'snapshots',
                                       'busy_samples', 'process_cpu_s', 'threads', 'top_functions', 'top_stacks'})
        self.assertEqual(report['unit'], 'cpu_ms' if os.name == 'nt' else 'samples')

    def test_cpu_weights_skip_threads_that_did_not_run(self):
        busy, leaves, stacks, threads = [0], {}, {}, {}
        frame = sys._getframe()
        startup_profile.sample({1: frame, 2: frame}, {1: 'ran', 2: 'blocked'}, 0, busy, leaves, stacks, threads,
                               weights={1: 12.5, 2: 0})
        self.assertEqual(threads, {'ran': 12.5})
        self.assertEqual(busy, [12.5])

    @unittest.skipUnless(os.name == 'nt', 'GetThreadTimes is Windows-only')
    def test_thread_clock_sees_a_blocked_thread_as_idle(self):
        blocked = threading.Event()
        waiter = threading.Thread(target=blocked.wait, args=(3,), name='blocked')
        stop = threading.Event()
        spinner = threading.Thread(target=busy_loop, args=(stop,), name='spinner')
        waiter.start(); spinner.start()
        self.addCleanup(lambda: (blocked.set(), stop.set(), waiter.join(2), spinner.join(2)))
        clock = startup_profile._ThreadClock()
        self.addCleanup(clock.close)
        clock.deltas([waiter, spinner])
        time.sleep(.3)
        deltas = clock.deltas([waiter, spinner])
        self.assertEqual(deltas.get(waiter.ident, 0), 0)
        self.assertGreater(deltas.get(spinner.ident, 0), 0)

    def test_opt_out_and_report_location(self):
        with patch.dict(os.environ, {'CODEX_MANAGER_STARTUP_PROFILE': '0'}):
            self.assertIsNone(startup_profile.start(self.root))
        with patch.object(startup_profile, '_run') as run:
            worker = startup_profile.start(self.root, duration=0)
            worker.join(2)
        self.assertEqual(run.call_args.args[0], self.root / 'work/control-center/logs/backend-startup-profile.json')
        self.assertTrue(worker.daemon)

    def test_samples_record_code_locations_only(self):
        busy, leaves, stacks, threads = [0], {}, {}, {}
        frame = sys._getframe()
        startup_profile.sample({1: frame}, {1: 'worker'}, 0, busy, leaves, stacks, threads)
        self.assertEqual(busy, [1])
        (where,) = leaves
        self.assertRegex(where, r'^[\w.]+:\w+:\d+$')


if __name__ == '__main__':
    unittest.main()
