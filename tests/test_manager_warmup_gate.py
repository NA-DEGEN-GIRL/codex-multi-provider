"""Leader-first warmup: fake launches, fixture stores and the real launch fence, never apps."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.launch_metrics import LaunchMetrics
from manager_core.profile_warmup import LEADER_GATE_CAP_SECONDS, LEADER_HEAD_START_SECONDS, ProfileWarmup
from manager_core.store import Store
from manager_core.update_hooks import UpdateHooks
from test_manager_parallel_launch import BlockingInstances


class Launches:
    """Warmup launch callback: each profile runs until the test releases it."""

    def __init__(self):
        self.lock = threading.Lock()
        self.entered, self.release = {}, {}
        self.order, self.active, self.peak = [], set(), [0]
        self.fail, self.open = set(), False
        # Returns running without a window: the 8 s window wait ran out.
        self.windowless = set()

    def event(self, table, profile_id):
        with self.lock:
            return table.setdefault(profile_id, threading.Event())

    def __call__(self, profile_id):
        with self.lock:
            self.order.append(profile_id)
            self.active.add(profile_id)
            self.peak[0] = max(self.peak[0], len(self.active))
        self.event(self.entered, profile_id).set()
        try:
            if not self.open and not self.event(self.release, profile_id).wait(5):
                raise TimeoutError('fixture launch was not released')
            if profile_id in self.fail:
                raise RuntimeError('private-launch-diagnostic')
            return dict(state='launched', profile=dict(
                status='running', window_handle=None if profile_id in self.windowless else 12))
        finally:
            with self.lock:
                self.active.discard(profile_id)


class WarmupFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root)
        self.ids = [self.store.add_profile(f'gate {index}')['id'] for index in range(4)]
        self.leader = self.ids[2]
        self.others = [p for p in self.ids if p != self.leader]
        self.metrics = LaunchMetrics(self.store.directory)
        self.threads = []
        self.addCleanup(self.join)

    def observe(self, profile):
        return dict(status='not_started')

    def spawn(self, target):
        thread = threading.Thread(target=target)
        self.threads.append(thread)
        thread.start()

    def warmup(self, launch, head_start=5, cap=10):
        warmup = ProfileWarmup(self.store, self, launch, spawn=self.spawn,
                               health=lambda _: dict(blocks_launch=False), max_workers=4,
                               metrics=self.metrics, head_start_seconds=head_start, cap_seconds=cap)
        self.addCleanup(warmup.shutdown)
        return warmup

    def join(self, timeout=5):
        for thread in self.threads:
            thread.join(timeout)
        self.assertTrue(all(not thread.is_alive() for thread in self.threads), 'a warmup worker stayed blocked')

    def gate_events(self):
        path = self.metrics.path
        events = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []
        return {e['profile_id']: (e['released_by'], e['elapsed_ms']) for e in events if e['phase'] == 'warmup_gate'}


class LeaderGateTests(WarmupFixture):
    def setUp(self):
        super().setUp()
        self.launches = Launches()
        self.addCleanup(self.release_all)

    def release_all(self):
        self.launches.open = True
        for event in list(self.launches.release.values()):
            event.set()

    def entered(self, profile_id, timeout=2):
        return self.launches.event(self.launches.entered, profile_id).wait(timeout)

    def finish(self, warmup):
        self.release_all()
        self.join()
        return warmup.status()

    def test_selected_profile_starts_first_and_the_rest_start_together_after_it(self):
        warmup = self.warmup(self.launches)
        warmup.start(leader=self.leader)
        self.assertTrue(self.entered(self.leader))
        self.assertFalse(self.entered(self.others[0], .3))
        self.assertFalse(any(self.launches.entered.get(p, threading.Event()).is_set() for p in self.others))
        status = warmup.status()
        self.assertEqual(status['gate'], dict(state='waiting', leaders=[self.leader]))
        self.assertEqual({e['state'] for e in status['profiles'] if e['profile_id'] in self.others}, {'queued'})
        self.launches.event(self.launches.release, self.leader).set()
        for profile_id in self.others:
            self.assertTrue(self.entered(profile_id), 'a profile stayed held after the leader was ready')
        self.assertEqual(self.launches.peak[0], 3, 'the others did not start together after the leader')
        status = self.finish(warmup)
        self.assertEqual(status['gate'], dict(state='released', released_by='leader_ready', leaders=[self.leader]))
        self.assertEqual((status['state'], status['counts']['ready']), ('complete', 4))
        self.assertEqual(self.launches.order[0], self.leader)
        events = self.gate_events()
        self.assertEqual(events[self.leader][0], 'leader')
        self.assertLess(events[self.leader][1], 5)
        self.assertEqual({events[p][0] for p in self.others}, {'leader_ready'})
        self.assertTrue(all(300 <= events[p][1] < 5000 for p in self.others), events)

    def test_timeout_releases_the_others_while_the_leader_still_prepares(self):
        warmup = self.warmup(self.launches, head_start=.2)
        started = time.monotonic()
        warmup.start(leader=self.leader)
        for profile_id in self.others:
            self.assertTrue(self.entered(profile_id))
        self.assertGreaterEqual(time.monotonic() - started, .2)
        self.assertIn(self.leader, self.launches.active, 'the running leader was interrupted')
        self.assertEqual(warmup.status()['gate']['released_by'], 'timeout')
        self.assertEqual(self.launches.peak[0], 4)
        status = self.finish(warmup)
        self.assertEqual(status['counts']['ready'], 4)
        events = self.gate_events()
        self.assertEqual({events[p][0] for p in self.others}, {'timeout'})
        self.assertTrue(all(abs(events[p][1] - 200) < 50 for p in self.others), events)
        self.assertEqual((LEADER_HEAD_START_SECONDS, LEADER_GATE_CAP_SECONDS), (30.0, 45.0))

    def test_window_pending_leader_releases_the_others_as_ready(self):
        # The launch returned after the backend's 8 s window wait without a window.
        self.launches.windowless.add(self.leader)
        warmup = self.warmup(self.launches)
        warmup.start(leader=self.leader)
        self.assertTrue(self.entered(self.leader))
        self.assertFalse(self.entered(self.others[0], .2))
        self.launches.event(self.launches.release, self.leader).set()
        for profile_id in self.others:
            self.assertTrue(self.entered(profile_id), 'a window-pending leader held the others')
        status = self.finish(warmup)
        self.assertEqual(status['gate']['released_by'], 'leader_ready')
        leader = next(e for e in status['profiles'] if e['profile_id'] == self.leader)
        self.assertEqual((leader['state'], leader['code']), ('started', 'window_pending'))
        self.assertEqual({self.gate_events()[p][0] for p in self.others}, {'leader_ready'})

    # Timing tests shrink the limits to .2-1.0 s; this one scales by 1/50 (30 s -> .6 s, 45 s -> .9 s).
    def test_cold_leader_slower_than_the_old_15_s_budget_keeps_its_head_start(self):
        warmup = self.warmup(self.launches, head_start=.6, cap=.9)
        warmup.start(leader=self.leader)
        self.assertTrue(self.entered(self.leader))
        # Revision 93 solo cold launch: 17.7 s + 4.8 s (.45 s here), past the old 15 s (.3 s).
        self.assertFalse(self.entered(self.others[0], .45), 'the others started before the leader returned')
        self.assertEqual(warmup.status()['gate']['state'], 'waiting')
        self.launches.event(self.launches.release, self.leader).set()
        for profile_id in self.others:
            self.assertTrue(self.entered(profile_id))
        self.assertEqual(self.finish(warmup)['gate']['released_by'], 'leader_ready')
        events = self.gate_events()
        self.assertTrue(all(450 <= events[p][1] < 600 for p in self.others), events)

    def test_the_newest_clicked_leader_governs_the_release(self):
        warmup = self.warmup(self.launches)
        warmup.start(leader=self.leader)
        self.assertTrue(self.entered(self.leader))
        *held, clicked = self.others
        self.assertTrue(warmup.prioritize(clicked))
        self.assertTrue(self.entered(clicked))
        # The restored profile's window is ready, but the user now waits for the click.
        self.launches.event(self.launches.release, self.leader).set()
        self.assertFalse(self.entered(held[0], .3), 'an older leader released the gate before the click')
        self.assertEqual(warmup.status()['gate']['state'], 'waiting')
        self.launches.event(self.launches.release, clicked).set()
        for profile_id in held:
            self.assertTrue(self.entered(profile_id))
        status = self.finish(warmup)
        self.assertEqual(status['gate'], dict(state='released', released_by='leader_ready',
                                              leaders=[self.leader, clicked]))
        self.assertEqual({self.gate_events()[p][0] for p in held}, {'leader_ready'})

    def test_a_failed_newest_leader_falls_back_to_an_earlier_one(self):
        *held, clicked = self.others
        self.launches.fail.add(clicked)
        warmup = self.warmup(self.launches)
        warmup.start(leader=self.leader)
        self.assertTrue(self.entered(self.leader))
        self.assertTrue(warmup.prioritize(clicked))
        self.assertTrue(self.entered(clicked))
        self.launches.event(self.launches.release, clicked).set()
        self.assertFalse(self.entered(held[0], .3), 'a failed click released the gate while the leader launched')
        self.assertEqual(warmup.status()['gate']['state'], 'waiting')
        self.launches.event(self.launches.release, self.leader).set()
        for profile_id in held:
            self.assertTrue(self.entered(profile_id))
        self.assertEqual(self.finish(warmup)['gate']['released_by'], 'leader_ready')

    def test_reselecting_a_launching_leader_makes_it_govern_again(self):
        warmup = self.warmup(self.launches)
        warmup.start(leader=self.leader)
        self.assertTrue(self.entered(self.leader))
        *held, clicked = self.others
        self.assertTrue(warmup.prioritize(clicked))
        self.assertTrue(self.entered(clicked))
        # The user goes back to the restored profile while both still launch.
        self.assertTrue(warmup.prioritize(self.leader))
        self.assertEqual(warmup.status()['gate'], dict(state='waiting', leaders=[clicked, self.leader]))
        self.launches.event(self.launches.release, clicked).set()
        self.assertFalse(self.entered(held[0], .3), 'the click released the gate after the user went back')
        self.assertEqual(warmup.status()['gate']['state'], 'waiting')
        self.launches.event(self.launches.release, self.leader).set()
        for profile_id in held:
            self.assertTrue(self.entered(profile_id))
        status = self.finish(warmup)
        self.assertEqual(status['gate'], dict(state='released', released_by='leader_ready',
                                              leaders=[clicked, self.leader]))
        self.assertEqual(sorted(self.launches.order), sorted(self.ids), 'a leader was launched twice')
        self.assertEqual({self.gate_events()[p][0] for p in held}, {'leader_ready'})

    def test_a_reselected_leader_keeps_the_head_start_from_its_first_launch(self):
        warmup = self.warmup(self.launches, head_start=.8, cap=5)
        warmup.start(leader=self.leader)
        self.assertTrue(self.entered(self.leader))
        time.sleep(.4)
        *held, clicked = self.others
        self.assertTrue(warmup.prioritize(clicked))
        self.assertTrue(self.entered(clicked))
        self.assertTrue(warmup.prioritize(self.leader))
        # The leader's own .8 s governs again, not the click's (.4 + .8 s) nor a new one.
        for profile_id in held:
            self.assertTrue(self.entered(profile_id))
        self.assertTrue({self.leader, clicked} <= self.launches.active, 'a leader was interrupted')
        self.assertEqual(self.finish(warmup)['gate']['released_by'], 'timeout')
        events = self.gate_events()
        self.assertTrue(all(abs(events[p][1] - 800) < 100 for p in held), events)

    def test_reselecting_a_leader_whose_head_start_is_spent_opens_the_gate_now(self):
        warmup = self.warmup(self.launches, head_start=1, cap=5)
        warmup.start(leader=self.leader)
        self.assertTrue(self.entered(self.leader))
        time.sleep(.5)
        *held, clicked = self.others
        self.assertTrue(warmup.prioritize(clicked))
        self.assertTrue(self.entered(clicked))
        # The leader's 1 s passes while the click (.5 + 1 s) governs.
        self.assertFalse(self.entered(held[0], .75))
        reselected = time.monotonic()
        self.assertTrue(warmup.prioritize(self.leader))
        for profile_id in held:
            self.assertTrue(self.entered(profile_id))
        self.assertEqual(self.finish(warmup)['gate']['released_by'], 'timeout')
        # Held until the re-selection, never backdated to the spent head start.
        floor = (reselected - warmup.gate_started) * 1000
        events = self.gate_events()
        self.assertTrue(all(floor - 1 <= events[p][1] < floor + 250 for p in held), (floor, events))

    def test_a_failed_newest_leader_after_an_earlier_head_start_releases_at_the_failure(self):
        *held, clicked = self.others
        self.launches.fail.add(clicked)
        warmup = self.warmup(self.launches, head_start=1, cap=5)
        warmup.start(leader=self.leader)
        self.assertTrue(self.entered(self.leader))
        time.sleep(.5)
        self.assertTrue(warmup.prioritize(clicked))
        self.assertTrue(self.entered(clicked))
        # The first leader's 1 s passes while the click (.5 + 1 s) governs.
        self.assertFalse(self.entered(held[0], .75))
        failed = time.monotonic()
        self.launches.event(self.launches.release, clicked).set()
        for profile_id in held:
            self.assertTrue(self.entered(profile_id))
        self.assertIn(self.leader, self.launches.active, 'the running leader was interrupted')
        self.assertEqual(self.finish(warmup)['gate']['released_by'], 'leader_failed')
        # Held until the failure, never backdated to the first leader's spent head start.
        floor = (failed - warmup.gate_started) * 1000
        events = self.gate_events()
        self.assertEqual({events[p][0] for p in held}, {'leader_failed'})
        self.assertTrue(all(floor - 1 <= events[p][1] < floor + 250 for p in held), (floor, events))

    def test_a_failed_newest_leader_releases_at_once_when_an_earlier_one_is_ready(self):
        *held, clicked = self.others
        self.launches.fail.add(clicked)
        warmup = self.warmup(self.launches)
        warmup.start(leader=self.leader)
        self.assertTrue(self.entered(self.leader))
        self.assertTrue(warmup.prioritize(clicked))
        self.assertTrue(self.entered(clicked))
        self.launches.event(self.launches.release, self.leader).set()
        self.assertFalse(self.entered(held[0], .2))
        self.launches.event(self.launches.release, clicked).set()
        for profile_id in held:
            self.assertTrue(self.entered(profile_id), 'the gate stayed closed with a ready leader')
        self.assertEqual(self.finish(warmup)['gate']['released_by'], 'leader_ready')

    def test_a_clicked_leader_gets_its_own_head_start_from_its_promotion(self):
        warmup = self.warmup(self.launches, head_start=.8, cap=5)
        warmup.start(leader=self.leader)
        self.assertTrue(self.entered(self.leader))
        time.sleep(.4)
        *held, clicked = self.others
        self.assertTrue(warmup.prioritize(clicked))
        self.assertTrue(self.entered(clicked))
        # The first leader's own head start (.8 s) passes; the click's (.4 + .8 s) governs.
        self.assertFalse(self.entered(held[0], .6), 'the click only got the remainder of the first head start')
        for profile_id in held:
            self.assertTrue(self.entered(profile_id))
        self.assertTrue({self.leader, clicked} <= self.launches.active, 'a leader was interrupted')
        self.assertEqual(self.finish(warmup)['gate']['released_by'], 'timeout')
        events = self.gate_events()
        promoted = events[clicked]
        self.assertEqual(promoted[0], 'promoted')
        self.assertTrue(all(abs(events[p][1] - (promoted[1] + 800)) < 100 for p in held), events)

    def test_absolute_cap_bounds_a_late_click(self):
        warmup = self.warmup(self.launches, head_start=.8, cap=1.0)
        warmup.start(leader=self.leader)
        self.assertTrue(self.entered(self.leader))
        time.sleep(.3)
        *held, clicked = self.others
        self.assertTrue(warmup.prioritize(clicked))
        self.assertTrue(self.entered(clicked))
        # The click's own head start would end near 1.1 s; the cap from the gate start ends first.
        for profile_id in held:
            self.assertTrue(self.entered(profile_id))
        self.assertEqual(self.finish(warmup)['gate']['released_by'], 'cap')
        events = self.gate_events()
        self.assertTrue(all(abs(events[p][1] - 1000) < 60 for p in held), events)

    def test_leader_failure_releases_the_others_without_leaking_diagnostics(self):
        self.launches.fail.add(self.leader)
        warmup = self.warmup(self.launches)
        warmup.start(leader=self.leader)
        self.assertTrue(self.entered(self.leader))
        self.assertFalse(self.entered(self.others[0], .2))
        self.launches.event(self.launches.release, self.leader).set()
        for profile_id in self.others:
            self.assertTrue(self.entered(profile_id))
        status = self.finish(warmup)
        self.assertEqual(status['gate']['released_by'], 'leader_failed')
        leader = next(e for e in status['profiles'] if e['profile_id'] == self.leader)
        self.assertEqual((leader['state'], leader['code']), ('attention', 'prepare_failed'))
        self.assertEqual(status['counts']['ready'], 3)
        self.assertNotIn('private', str(status) + self.metrics.path.read_text(encoding='utf-8'))

    def test_a_failed_leader_waits_for_another_leader_still_launching(self):
        *held, clicked = self.others
        self.launches.fail.update({self.leader, clicked})
        warmup = self.warmup(self.launches)
        warmup.start(leader=self.leader)
        self.assertTrue(self.entered(self.leader))
        self.assertTrue(warmup.prioritize(clicked))
        self.assertTrue(self.entered(clicked))
        self.launches.event(self.launches.release, self.leader).set()
        deadline = time.monotonic() + 2
        while (next(e for e in warmup.status()['profiles'] if e['profile_id'] == self.leader)['state'] != 'attention'
               and time.monotonic() < deadline):
            time.sleep(.01)
        self.assertFalse(self.entered(held[0], .2), 'one failed leader released the gate while another launched')
        self.assertEqual(warmup.status()['gate']['state'], 'waiting')
        self.launches.event(self.launches.release, clicked).set()
        for profile_id in held:
            self.assertTrue(self.entered(profile_id), 'the gate stayed closed after every leader failed')
        self.assertEqual(self.finish(warmup)['gate']['released_by'], 'leader_failed')

    def test_skipped_or_unknown_leader_never_holds_the_others(self):
        self.store.mutate(lambda data: self.store.profile(self.leader, data).update(native_login_pending=True))
        warmup = self.warmup(self.launches, head_start=30, cap=30)
        warmup.start(leader=self.leader)
        for profile_id in self.others:
            self.assertTrue(self.entered(profile_id), 'a skipped leader held the others')
        status = self.finish(warmup)
        self.assertEqual(status['gate']['released_by'], 'leader_failed')
        self.assertNotIn(self.leader, self.launches.order)
        self.threads.clear()
        self.launches = Launches()
        warmup = self.warmup(self.launches, head_start=30, cap=30)
        warmup.start(leader=str(uuid4()))
        for profile_id in self.others:
            self.assertTrue(self.entered(profile_id), 'an unknown selection held the others')
        self.assertEqual(self.finish(warmup)['gate'], dict(state='none', leaders=[]))

    def test_click_promotes_a_waiting_profile_at_once_without_interrupting_the_leader(self):
        warmup = self.warmup(self.launches)
        warmup.start(leader=self.leader)
        self.assertTrue(self.entered(self.leader))
        *held, clicked = self.others
        self.assertTrue(warmup.prioritize(clicked))
        self.assertTrue(self.entered(clicked), 'a clicked profile waited at the gate')
        self.assertFalse(any(self.launches.entered.get(p, threading.Event()).is_set() for p in held))
        self.assertEqual(warmup.status()['gate'], dict(state='waiting', leaders=[self.leader, clicked]))
        self.assertIn(self.leader, self.launches.active)
        # Already running: never launched again. Selecting the newest leader
        # again keeps it newest; a login promotion never reorders.
        self.assertTrue(warmup.prioritize(clicked))
        self.assertFalse(warmup.promote(clicked))
        self.assertEqual(warmup.status()['gate']['leaders'], [self.leader, clicked])
        # The newest leader's return releases the others; the first keeps running.
        self.launches.event(self.launches.release, clicked).set()
        for profile_id in held:
            self.assertTrue(self.entered(profile_id))
        self.assertIn(self.leader, self.launches.active)
        status = self.finish(warmup)
        self.assertEqual(status['gate']['released_by'], 'leader_ready')
        events = self.gate_events()
        self.assertEqual(events[self.leader][0], 'leader')
        self.assertEqual(events[clicked][0], 'promoted')
        self.assertGreater(events[clicked][1], 0)
        self.assertEqual({events[p][0] for p in held}, {'leader_ready'})

    def test_login_open_promotes_only_while_the_gate_waits(self):
        warmup = self.warmup(self.launches)
        self.assertFalse(warmup.promote(self.others[0]), 'promotion before any pass')
        warmup.start(leader=self.leader)
        self.assertTrue(self.entered(self.leader))
        self.assertTrue(warmup.promote(self.others[0]))
        self.assertTrue(self.entered(self.others[0]))
        self.assertFalse(self.entered(self.others[1], .2))
        # The login's entry is the newest leader; its return releases the others.
        self.launches.event(self.launches.release, self.others[0]).set()
        self.assertTrue(self.entered(self.others[1]))
        self.assertFalse(warmup.promote(self.others[2]), 'a released gate cannot be promoted through')
        self.assertEqual(self.finish(warmup)['gate']['leaders'], [self.leader, self.others[0]])

    def test_without_a_leader_there_is_no_gate(self):
        warmup = self.warmup(self.launches, head_start=30, cap=30)
        warmup.start()
        for profile_id in self.ids:
            self.assertTrue(self.entered(profile_id))
        self.assertEqual(self.launches.peak[0], 4)
        self.assertFalse(warmup.promote(self.ids[0]))
        status = self.finish(warmup)
        self.assertEqual(status['gate'], dict(state='none', leaders=[]))
        self.assertEqual(self.gate_events(), {})

    def test_gate_is_one_shot_per_service_start(self):
        self.launches.open = True
        warmup = self.warmup(self.launches)
        warmup.start(leader=self.leader)
        self.join()
        done = warmup.status()
        self.assertEqual((done['state'], done['gate']['released_by']), ('complete', 'leader_ready'))
        spawned = len(self.threads)
        self.assertEqual(warmup.start(leader=self.others[0]), done)
        self.assertEqual(len(self.threads), spawned)
        self.assertFalse(warmup.prioritize(self.others[0]))
        self.assertFalse(warmup.promote(self.others[0]))
        self.assertEqual(sorted(self.launches.order), sorted(self.ids))
        self.assertEqual(warmup.status()['gate'], done['gate'])

    def test_shutdown_wakes_held_workers_and_cancels_their_profiles(self):
        warmup = self.warmup(self.launches, head_start=30, cap=30)
        warmup.start(leader=self.leader)
        self.assertTrue(self.entered(self.leader))
        warmup.shutdown()
        status = warmup.status()
        self.assertEqual(status['gate']['released_by'], 'stopped')
        self.assertEqual({e['state'] for e in status['profiles'] if e['profile_id'] in self.others}, {'cancelled'})
        started = time.monotonic()
        status = self.finish(warmup)
        self.assertLess(time.monotonic() - started, 5, 'held workers waited for the gate timeout')
        self.assertEqual((status['state'], status['worker_active']), ('stopped', False))
        self.assertEqual(self.launches.order, [self.leader])


class GateFenceTests(WarmupFixture):
    """The gate with the real shared/exclusive launch fence (UpdateHooks)."""

    def setUp(self):
        super().setUp()
        self.instances = BlockingInstances(self.store)
        self.hooks = UpdateHooks(self.root, self.store, self.instances)
        self.instances.hooks = self.hooks
        for profile_id in self.ids:
            self.instances.gate(profile_id)
        self.enterContext(patch('subprocess.Popen',
            side_effect=AssertionError('fixture must not launch an app or SSH')))
        self.enterContext(patch('manager_core.rust_service.launch',
            side_effect=AssertionError('fixture must not launch through the service')))
        self.addCleanup(self.release_all)

    def release_all(self):
        for event in self.instances.release.values():
            event.set()

    def launch(self, profile_id):
        result = self.instances.show(profile_id)
        return {**result, 'profile': {**result['profile'], 'status': 'running', 'window_handle': 12}}

    def queued(self, count):
        queue = self.hooks._launch_queue
        with queue.condition:
            self.assertTrue(queue.condition.wait_for(lambda: len(queue.waiting) == count, 2))

    def test_serial_migration_runs_the_leader_first_and_a_click_next(self):
        self.instances.exclusive = True
        *rest, clicked = self.others
        warmup = self.warmup(self.launch)
        self.hooks.prioritize_launch(self.leader)
        warmup.start(leader=self.leader)
        self.assertTrue(self.instances.entered[self.leader].wait(2))
        # Held profiles take no queue ticket, admission or fence.
        self.assertFalse(self.instances.entered[rest[0]].wait(.2))
        self.assertEqual((self.hooks._launch_queue.waiting, len(self.hooks._launch_queue.held)), ([], 1))
        self.assertTrue(warmup.prioritize(clicked))
        self.hooks.prioritize_launch(clicked)
        self.queued(1)
        self.instances.release[self.leader].set()
        self.assertTrue(self.instances.entered[clicked].wait(2), 'the click waited behind held warmup')
        # The click is now the newest leader: the rest stay at the gate, not in the queue.
        self.assertFalse(self.instances.entered[rest[0]].wait(.2))
        self.assertEqual((self.hooks._launch_queue.waiting, warmup.status()['gate']['state']), ([], 'waiting'))
        self.instances.release[clicked].set()
        # One at a time: the first of the rest holds the exclusive fence, the others queue.
        self.queued(len(rest) - 1)
        self.release_all()
        self.join()
        self.assertEqual(self.instances.shown[:2], [self.leader, clicked])
        self.assertEqual(sorted(self.instances.shown[2:]), sorted(rest))
        self.assertEqual(self.instances.peak[0], 1)
        self.assertEqual(warmup.status()['gate']['released_by'], 'leader_ready')
        self.assertEqual((self.hooks._launch_queue.waiting, self.hooks._launch_queue.held), ([], {}))

    def test_maintenance_waits_for_the_leader_and_the_gate_never_deadlocks(self):
        warmup = self.warmup(self.launch)
        warmup.start(leader=self.leader)
        self.assertTrue(self.instances.entered[self.leader].wait(2))
        done, errors = threading.Event(), []
        def maintenance():
            try:
                self.hooks._begin_global(str(uuid4()))
            except Exception as error:
                errors.append(error)
            finally:
                done.set()
        threading.Thread(target=maintenance).start()
        self.queued(1)
        self.assertFalse(done.wait(.2), 'maintenance began beside an admitted launch')
        self.instances.release[self.leader].set()
        self.assertTrue(done.wait(2))
        self.join()
        self.assertEqual(errors, [])
        status = warmup.status()
        self.assertEqual(status['gate']['released_by'], 'leader_ready')
        self.assertEqual({e['state'] for e in status['profiles'] if e['profile_id'] in self.others}, {'attention'})
        self.assertEqual(self.instances.shown, [self.leader])
        self.assertEqual((self.hooks._launch_queue.waiting, self.hooks._launch_queue.held), ([], {}))

    def test_held_maintenance_fails_the_leader_and_releases_the_gate_at_once(self):
        self.hooks._begin_global(str(uuid4()))
        warmup = self.warmup(self.launch, head_start=30, cap=30)
        started = time.monotonic()
        warmup.start(leader=self.leader)
        self.join()
        self.assertLess(time.monotonic() - started, 5)
        status = warmup.status()
        self.assertEqual((status['state'], status['counts']['attention']), ('attention', 4))
        self.assertEqual(status['gate']['released_by'], 'leader_failed')
        self.assertEqual(self.instances.shown, [])


if __name__ == '__main__':
    unittest.main()
