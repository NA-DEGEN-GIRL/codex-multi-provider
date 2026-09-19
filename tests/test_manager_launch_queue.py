from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.launch_queue import LaunchQueue


class LaunchQueueTests(unittest.TestCase):
    def order(self, profiles, preferred):
        queue = LaunchQueue()
        queue.acquire('current')
        order, workers = [], []
        def run(profile):
            queue.acquire(profile)
            try:
                order.append(profile)
            finally:
                queue.release()
        try:
            for index, profile in enumerate(profiles):
                worker = threading.Thread(target=run, args=(profile,))
                workers.append(worker)
                worker.start()
                with queue.condition:
                    self.assertTrue(queue.condition.wait_for(lambda: len(queue.waiting) == index + 1, 2))
            queue.prefer(preferred)
            self.assertEqual(order, [], 'priority never interrupts the admitted launch')
        finally:
            queue.release()
            for worker in workers:
                worker.join(3)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        return order

    def test_selected_worker_already_waiting_for_admission_goes_next(self):
        self.assertEqual(self.order(['a', 'b', 'selected'], 'selected'), ['selected', 'a', 'b'])

    def test_unknown_priority_does_not_block_and_maintenance_remains_a_fence(self):
        self.assertEqual(self.order(['a', 'b'], 'missing'), ['a', 'b'])
        self.assertEqual(self.order(['a', None, 'selected', 'b'], 'selected'), ['a', None, 'selected', 'b'])

    def test_reentrant_owner_and_foreign_release(self):
        queue = LaunchQueue()
        queue.acquire('a')
        queue.acquire('a')
        self.assertEqual(queue.depth, 2)
        queue.release()
        errors = []
        def wrong_release():
            try:
                queue.release()
            except RuntimeError as error:
                errors.append(error)
        worker = threading.Thread(target=wrong_release)
        worker.start()
        worker.join(2)
        self.assertEqual(len(errors), 1)
        queue.release()
        self.assertIsNone(queue.owner)


if __name__ == '__main__':
    unittest.main()
