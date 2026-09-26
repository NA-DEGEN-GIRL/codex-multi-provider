from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.launch_queue import LaunchQueue


class Holder:
    """One worker thread that acquires admission and holds it until released."""

    def __init__(self, queue, profile, **options):
        self.queue, self.profile = queue, profile
        self.admitted, self.release = threading.Event(), threading.Event()
        self.errors = []
        self.thread = threading.Thread(target=self.run, args=(options,))

    def run(self, options):
        try:
            self.queue.acquire(self.profile, **options)
        except Exception as error:
            self.errors.append(error)
            return
        try:
            self.admitted.set()
            if not self.release.wait(5):
                raise TimeoutError('fixture holder was not released')
        finally:
            self.queue.release()

    def start(self, waiting=None):
        self.thread.start()
        if waiting is not None:
            with self.queue.condition:
                assert self.queue.condition.wait_for(lambda: len(self.queue.waiting) == waiting, 2)
        return self

    def finish(self):
        self.release.set()
        if self.thread.ident is None:
            return
        self.thread.join(3)
        assert not self.thread.is_alive()


class LaunchQueueTests(unittest.TestCase):
    def holders(self, *rows):
        created = list(rows)
        self.addCleanup(lambda: [row.finish() for row in created])
        return created

    def test_different_profiles_are_admitted_together_without_reordering(self):
        queue = LaunchQueue()
        current, a, b, selected = self.holders(*(Holder(queue, p) for p in ('current', 'a', 'b', 'selected')))
        current.start()
        self.assertTrue(current.admitted.wait(2))
        queue.prefer('selected')
        for holder in (a, b, selected):
            holder.start()
        for holder in (a, b, selected):
            self.assertTrue(holder.admitted.wait(2), 'another profile waited for a running launch')
        self.assertEqual(queue.waiting, [])
        self.assertEqual(len(queue.held), 4)

    def test_same_profile_waits_in_arrival_order_without_blocking_others(self):
        queue = LaunchQueue()
        first, second, other, third = self.holders(
            Holder(queue, 'a'), Holder(queue, 'a'), Holder(queue, 'b'), Holder(queue, 'a'))
        first.start()
        self.assertTrue(first.admitted.wait(2))
        second.start(waiting=1)
        other.start()
        self.assertTrue(other.admitted.wait(2), 'a waiting launch blocked another profile')
        third.start(waiting=2)
        first.finish()
        self.assertTrue(second.admitted.wait(2))
        self.assertFalse(third.admitted.is_set())
        second.finish()
        self.assertTrue(third.admitted.wait(2))

    def test_maintenance_waits_for_every_launch_and_fences_later_ones(self):
        queue = LaunchQueue()
        a, b, maintenance, late = self.holders(
            Holder(queue, 'a'), Holder(queue, 'b'), Holder(queue, None), Holder(queue, 'selected'))
        a.start()
        b.start()
        self.assertTrue(a.admitted.wait(2) and b.admitted.wait(2))
        maintenance.start(waiting=1)
        queue.prefer('selected')
        late.start(waiting=2)
        a.finish()
        self.assertFalse(maintenance.admitted.wait(.2))
        b.finish()
        self.assertTrue(maintenance.admitted.wait(2))
        self.assertFalse(late.admitted.wait(.2), 'a selected launch moved ahead of maintenance')
        maintenance.finish()
        self.assertTrue(late.admitted.wait(2))

    def test_exclusive_launch_runs_alone(self):
        queue = LaunchQueue()
        other, exclusive, later = self.holders(
            Holder(queue, 'b'), Holder(queue, 'a', exclusive=True), Holder(queue, 'c'))
        other.start()
        self.assertTrue(other.admitted.wait(2))
        exclusive.start(waiting=1)
        later.start(waiting=2)
        other.finish()
        self.assertTrue(exclusive.admitted.wait(2))
        self.assertFalse(later.admitted.wait(.2))
        exclusive.finish()
        self.assertTrue(later.admitted.wait(2))

    def test_reentrant_holder_never_waits_for_its_own_fence(self):
        queue = LaunchQueue()
        maintenance = self.holders(Holder(queue, None))[0]
        queue.acquire('a')
        try:
            queue.acquire('a')
            self.assertEqual(queue.depth, 2)
            queue.release()
            with self.assertRaises(RuntimeError):
                queue.acquire(None)  # Waiting for every holder includes this one.
            maintenance.start(waiting=1)
            queue.acquire('c')  # A nested open ignores queued maintenance.
            queue.release()
            errors = []
            def foreign_release():
                try:
                    queue.release()
                except RuntimeError as error:
                    errors.append(error)
            worker = threading.Thread(target=foreign_release)
            worker.start()
            worker.join(2)
            self.assertEqual(len(errors), 1)
            self.assertFalse(maintenance.admitted.is_set())
        finally:
            queue.release()
        self.assertEqual(queue.depth, 0)
        self.assertTrue(maintenance.admitted.wait(2))

    def exclusive_order(self, profiles, preferred):
        """Admission order behind a running exclusive launch (pending migration).

        ``None`` queues maintenance. The selection arrives after every request
        has queued; running work is never interrupted.
        """
        queue = LaunchQueue()
        queue.acquire('current', exclusive=True)
        order, workers = [], []
        def run(profile):
            queue.acquire(profile, exclusive=True)
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
        self.assertEqual(queue.held, {})
        return order, queue

    def test_selected_exclusive_launch_goes_ahead_of_queued_warmup(self):
        order, queue = self.exclusive_order(['a', 'b', 'selected', 'c'], 'selected')
        self.assertEqual(order, ['selected', 'a', 'b', 'c'])
        self.assertIsNone(queue.preferred, 'an admitted selection is consumed')

    def test_exclusive_priority_never_passes_maintenance_and_ignores_unknown(self):
        self.assertEqual(self.exclusive_order(['a', 'b'], 'missing')[0], ['a', 'b'])
        self.assertEqual(self.exclusive_order(['a', None, 'selected', 'b'], 'selected')[0],
                         ['a', None, 'selected', 'b'])
        self.assertEqual(self.exclusive_order(['a', 'selected', None, 'b'], 'selected')[0],
                         ['selected', 'a', None, 'b'])

    def test_selection_made_before_its_launch_queues_still_applies(self):
        queue = LaunchQueue()
        queue.prefer('selected')
        current = self.holders(Holder(queue, 'current', exclusive=True))[0].start()
        self.assertTrue(current.admitted.wait(2))
        a, selected = self.holders(Holder(queue, 'a', exclusive=True), Holder(queue, 'selected', exclusive=True))
        a.start(waiting=1)
        selected.start(waiting=2)
        current.finish()
        self.assertTrue(selected.admitted.wait(2))
        self.assertFalse(a.admitted.wait(.2), 'an earlier warmup launch ran beside the selected one')
        selected.finish()
        self.assertTrue(a.admitted.wait(2))

    def test_nested_open_waits_only_for_the_same_profile(self):
        queue = LaunchQueue()
        peer = self.holders(Holder(queue, 'b'))[0].start()
        self.assertTrue(peer.admitted.wait(2))
        entered = threading.Event()
        def nested_open():
            queue.acquire('a')
            try:
                queue.acquire('b')
                entered.set()
                queue.release()
            finally:
                queue.release()
        worker = threading.Thread(target=nested_open)
        worker.start()
        self.assertFalse(entered.wait(.2), 'a nested open ran beside another launch of that profile')
        peer.finish()
        self.assertTrue(entered.wait(2))
        worker.join(2)
        self.assertEqual(queue.held, {})


if __name__ == '__main__':
    unittest.main()
