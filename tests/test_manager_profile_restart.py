from copy import deepcopy
from unittest.mock import patch
from pathlib import Path
from uuid import uuid4
import tempfile
import threading
import unittest

from manager_core.profile_restart import ProfileRestarts, supersede_previous_notice
from manager_core.store import Store
from manager_core.updates import UpdateError


class FakeInstances:
    def __init__(self, store):
        self.store = store
        self.running = True

    def paths(self, profile):
        return Path(profile['home']), Path(profile['ui_home'])

    def observe(self, profile):
        return {'status': 'running' if self.running else 'not_started'}


class FakeHooks:
    def __init__(self, store, instances):
        self.store, self.instances = store, instances
        self.idle = True
        self.calls = []
        self.exit_verified = True
        self.restore_verified = True
        self.change_during_open = False
        self.gated = False

    def guard_launch(self, profile_id):
        if self.gated:
            raise UpdateError('update_maintenance', 'another update')

    def snapshot_instances(self, profile_ids=None):
        self.calls.append(('snapshot', profile_ids))
        return [dict(id=profile_ids[0], idle_verified=self.idle,
                     update_blocker=None if self.idle else 'runtime_not_idle')]

    def acquire_maintenance(self, instances, *, transaction_id, profile_scope):
        self.calls.append(('acquire', deepcopy(profile_scope)))
        return {'transaction_id': transaction_id, 'profile_scope': profile_scope}

    def close_instance(self, snapshot, *, finish_idle_exit=False):
        self.idle_exit_requested = finish_idle_exit
        self.calls.append(('close', snapshot['id']))
        if self.exit_verified:
            self.instances.running = False
        return self.exit_verified

    def release_maintenance(self, lease):
        self.calls.append(('release', lease['profile_scope']))

    def restore_instance(self, entry):
        self.calls.append(('restore', entry['profile_id']))
        self.instances.running = True
        def save(data):
            p = self.store.profile(entry['profile_id'], data)
            p['policy']['launched_revision'] = p['policy']['desired_revision']
            if self.change_during_open:
                p['policy']['desired_revision'] += 1
                self.change_during_open = False
        self.store.mutate(save)
        return {'verified': self.restore_verified}

    # Explicit SSH drain: one transaction; a stop flag only ever turns on.
    remote_busy = False
    during_start = None

    def begin_remote_reconcile(self, profile_id, *, graceful_drain=False, stop_only=False):
        self.calls.append(('begin_remote', stop_only))
        self.remote = dict(transaction_id=str(uuid4()), stop_only=stop_only, committed=False)
        return self.remote['transaction_id']

    def downgrade_remote_reconcile_to_stop(self, profile_id, transaction_id):
        self.calls.append(('downgrade', transaction_id))
        if self.remote['transaction_id'] != transaction_id or self.remote['committed']:
            return False
        self.remote['stop_only'] = True
        return True

    def reconcile_opened_remotes(self, profile_id, transaction_id):
        self.calls.append(('reconcile', transaction_id))
        if self.remote['transaction_id'] != transaction_id:
            raise UpdateError('ssh_generation_changed', 'released transaction reused')
        if self.remote_busy:
            return False
        if not self.remote['stop_only']:
            self.remote['committed'] = True
            if self.during_start:
                self.during_start, during = None, self.during_start
                during()
            self.calls.append(('remote_start', transaction_id))
        self.remote['transaction_id'] = None  # released
        return True

    def remote_open_failed(self, profile_id, transaction_id, code):
        self.calls.append(('remote_failed', code))


class RestartTests(unittest.TestCase):
    def test_login_preflight_fails_before_closing_an_existing_profile(self):
        self.store.mutate(lambda d: self.store.profile(self.profile['id'], d).update(
            source_home=str(self.store.root / 'missing-login')))
        job = self.restarts.schedule(self.profile['id'])
        self.restarts.step(self.profile['id'], job['id'])
        self.assertTrue(self.instances.running)
        self.assertFalse(any(call[0] == 'close' for call in self.hooks.calls))
        self.assertEqual(self.restarts.status()[self.profile['id']]['code'], 'login_required')

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name))
        self.profile = self.store.add_profile('selected')
        self.peer = self.store.add_profile('peer')
        self.instances = FakeInstances(self.store)
        self.hooks = FakeHooks(self.store, self.instances)
        self.pending = []
        self.restarts = ProfileRestarts(self.store, self.instances, self.hooks, spawn=self.pending.append)

    def auto(self, key='release-1'):
        return self.restarts.schedule(self.profile['id'], automatic_key=key,
            expected_generation=self.store.profile(self.profile['id']).get('generation'))

    def test_automatic_update_waits_for_active_work_then_finishes_verified_idle_exit(self):
        self.hooks.idle = False
        job = self.auto()
        self.assertFalse(self.restarts.step(self.profile['id'], job['id']))
        self.assertEqual(self.hooks.calls, [('snapshot', [self.profile['id']])])
        self.hooks.idle = True
        self.pending.pop()()
        self.assertTrue(self.hooks.idle_exit_requested)
        self.assertEqual(self.restarts.status()[self.profile['id']]['phase'], 'complete')
        self.assertEqual(self.auto()['id'], job['id'])
        self.assertFalse(self.pending)

    def test_automatic_update_failure_is_not_retried_on_every_window_open(self):
        self.hooks.exit_verified = False
        job = self.auto()
        self.pending.pop()()
        self.assertEqual(self.auto()['id'], job['id'])
        self.assertEqual(self.restarts.status()[self.profile['id']]['phase'], 'attention')
        self.assertFalse(self.pending)

    def test_stopped_backend_resumes_automatic_busy_wait_on_next_startup(self):
        job = self.auto()
        self.pending.clear()
        second = ProfileRestarts(self.store, self.instances, self.hooks, spawn=self.pending.append,
                                 owner_alive=lambda _: False)
        resumed = second.schedule(self.profile['id'], automatic_key='release-1', expected_generation=None)
        self.assertNotEqual(job['id'], resumed['id'])
        self.pending.pop()()
        self.assertEqual(second.status()[self.profile['id']]['phase'], 'complete')

    def test_explicit_all_profile_retry_replaces_failed_job_with_same_release(self):
        old = self.auto()
        snapshot = [dict(id=self.profile['id'], idle_verified=False, update_blocker='runtime_identity_not_ready')]
        with patch.object(self.hooks, 'snapshot_instances', return_value=snapshot):
            self.pending.pop()()
        retried = self.restarts.schedule(self.profile['id'], automatic_key='release-1',
            expected_generation=self.store.profile(self.profile['id']).get('generation'), retry_failed=True)
        self.assertNotEqual(old['id'], retried['id'])
        self.pending.pop()()
        self.assertEqual(self.restarts.status()[self.profile['id']]['phase'], 'complete')

    def test_profile_replaced_after_startup_observation_is_never_closed(self):
        self.store.mutate(lambda d: self.store.profile(self.profile['id'], d).update(generation='new'))
        result = self.restarts.schedule(self.profile['id'], automatic_key='release', expected_generation='old')
        self.assertEqual(result['phase'], 'superseded')
        self.assertFalse(self.pending)
        self.assertFalse(self.hooks.calls)

    def test_service_shutdown_while_waiting_is_resumable_on_next_startup(self):
        job = self.auto()
        self.restarts.shutdown()
        self.pending.pop()()
        self.assertEqual(self.restarts.status()[self.profile['id']]['code'], 'service_stopped')
        second = ProfileRestarts(self.store, self.instances, self.hooks, spawn=self.pending.append)
        resumed = second.schedule(self.profile['id'], automatic_key='release-1', expected_generation=None)
        self.assertNotEqual(resumed['id'], job['id'])
        self.pending.pop()()
        self.assertEqual(second.status()[self.profile['id']]['phase'], 'complete')

    def test_profile_replaced_while_waiting_is_never_closed(self):
        job = self.auto()
        self.store.mutate(lambda d: self.store.profile(self.profile['id'], d).update(generation='new'))
        self.pending.pop()()
        self.assertEqual(self.restarts.status()[self.profile['id']]['phase'], 'superseded')
        self.assertFalse(self.hooks.calls)

    def test_automatic_reopen_retains_last_opened_task_without_starting_a_turn(self):
        with patch.object(self.instances, 'observe', return_value={'status': 'running',
            'runtime_state': {'opened_task': {'thread_id': self.peer['id']}}}), \
             patch.object(self.hooks, 'restore_instance', wraps=self.hooks.restore_instance) as restored:
            self.auto()
            self.pending.pop()()
        self.assertEqual(restored.call_args.args[0]['thread_id'], self.peer['id'])

    def test_busy_work_waits_without_freezing_and_duplicate_saves_share_one_worker(self):
        self.hooks.idle = False
        job = self.restarts.schedule(self.profile['id'])
        self.assertEqual(self.restarts.schedule(self.profile['id'])['id'], job['id'])
        self.assertEqual(len(self.pending), 1)
        self.assertFalse(self.restarts.step(self.profile['id'], job['id']))
        self.assertEqual(self.hooks.calls, [('snapshot', [self.profile['id']])])
        self.hooks.idle = True
        self.pending.pop()()
        self.assertEqual(self.restarts.status()[self.profile['id']]['phase'], 'complete')
        self.assertTrue(self.hooks.idle_exit_requested, 'manual policy application must finish a verified drained tray process')
        self.assertEqual([name for name, _ in self.hooks.calls],
                         ['snapshot', 'snapshot', 'acquire', 'close', 'restore', 'release'])
        self.assertTrue(all(self.peer['id'] not in str(value) for _, value in self.hooks.calls))

    def test_successful_new_launch_clears_only_previous_failed_notice(self):
        data = {'profile_restarts': {
            self.profile['id']: {'phase': 'attention', 'message': 'old error'},
            self.peer['id']: {'phase': 'attention', 'message': 'peer error'}}}
        supersede_previous_notice(data, {**self.profile, 'generation': 'new-launch'})
        selected = data['profile_restarts'][self.profile['id']]
        self.assertEqual((selected['phase'], selected['message']), ('superseded', ''))
        self.assertEqual(selected['replacement_generation'], 'new-launch')
        self.assertEqual(data['profile_restarts'][self.peer['id']]['phase'], 'attention')

    def test_repaint_and_live_restart_never_clear_current_failure(self):
        for phase, generation in (('attention', 'current'), ('waiting', 'old'), ('closing', 'old')):
            data = {'profile_restarts': {self.profile['id']: {'phase': phase, 'generation': generation}}}
            before = deepcopy(data)
            supersede_previous_notice(data, {**self.profile, 'generation': 'current'})
            self.assertEqual(data, before)

    def test_unconfirmed_exit_never_starts_replacement_or_replays_close(self):
        self.hooks.exit_verified = False
        self.restarts.schedule(self.profile['id'])
        self.pending.pop()()
        job = self.restarts.status()[self.profile['id']]
        self.assertEqual(job['phase'], 'attention')
        self.assertEqual([name for name, _ in self.hooks.calls], ['snapshot', 'acquire', 'close', 'release'])
        self.assertEqual(job['code'], 'normal_exit_pending')

    def test_missing_runtime_state_shows_attention_instead_of_waiting_forever(self):
        self.restarts.schedule(self.profile['id'])
        snapshot = [dict(id=self.profile['id'], idle_verified=False, update_blocker='runtime_identity_not_ready')]
        with patch.object(self.hooks, 'snapshot_instances', return_value=snapshot):
            self.pending.pop()()
        job = self.restarts.status()[self.profile['id']]
        self.assertEqual((job['phase'], job['code']), ('attention', 'runtime_state_unavailable'))
        self.assertEqual(self.hooks.calls, [])

    def test_new_settings_during_launch_require_another_application(self):
        self.hooks.change_during_open = True
        self.restarts.schedule(self.profile['id'])
        self.pending.pop()()
        self.assertEqual([name for name, _ in self.hooks.calls].count('restore'), 2)
        self.assertEqual(self.restarts.status()[self.profile['id']]['phase'], 'complete')

    def test_dead_backend_status_is_passive_but_explicit_apply_needs_only_one_click(self):
        job = self.restarts.schedule(self.profile['id'])
        self.pending.clear()  # The former backend is now gone, including its worker.
        recovered = ProfileRestarts(self.store, self.instances, self.hooks, spawn=self.pending.append,
                                    owner_alive=lambda job: False)
        self.assertEqual(recovered.status()[self.profile['id']]['phase'], 'attention')
        self.assertFalse(self.pending)
        self.assertEqual(self.hooks.calls, [])
        result = recovered.schedule(self.profile['id'])
        self.assertNotEqual(result['id'], job['id'])
        self.assertEqual(result['phase'], 'waiting')
        self.assertEqual(len(self.pending), 1)
        self.assertEqual(self.hooks.calls, [])
        self.pending.pop()()
        self.assertEqual(recovered.status()[self.profile['id']]['phase'], 'complete')

    def test_status_of_a_poll_snapshot_does_not_reread_the_store(self):
        job = self.restarts.schedule(self.profile['id'])
        self.pending.clear()
        recovered = ProfileRestarts(self.store, self.instances, self.hooks, spawn=self.pending.append,
                                    owner_alive=lambda job: False)
        snapshot = self.store.read()
        with patch.object(self.store, 'read', side_effect=AssertionError('store re-read')):
            jobs = recovered.status(state=snapshot)
        # The dead-owner view is applied to the caller's snapshot, never saved.
        self.assertIs(jobs, snapshot['profile_restarts'])
        self.assertEqual(jobs[self.profile['id']]['phase'], 'attention')
        self.assertEqual(self.store.read()['profile_restarts'][self.profile['id']]['phase'], job['phase'])
        self.assertEqual(recovered.status(state={}), {})

    def test_another_live_backend_cannot_replace_or_release_the_worker(self):
        job = self.restarts.schedule(self.profile['id'])
        second = ProfileRestarts(self.store, self.instances, self.hooks, spawn=self.pending.append,
                                 owner_alive=lambda job: True)
        self.assertEqual(second.schedule(self.profile['id'])['id'], job['id'])
        self.assertEqual(second.status()[self.profile['id']]['phase'], 'waiting')
        self.assertEqual(len(self.pending), 1)
        self.assertEqual(self.hooks.calls, [])

    def test_closed_profile_reopens_without_acquiring_or_closing_other_instances(self):
        self.instances.running = False
        self.restarts.schedule(self.profile['id'])
        self.pending.pop()()
        self.assertEqual(self.hooks.calls, [('snapshot', [self.profile['id']]), ('restore', self.profile['id'])])

    def test_update_gate_waits_without_closing_and_shutdown_does_not_replay(self):
        self.hooks.gated = True
        job = self.restarts.schedule(self.profile['id'])
        self.assertFalse(self.restarts.step(self.profile['id'], job['id']))
        self.assertEqual(self.hooks.calls, [])
        self.restarts.shutdown()
        self.pending.pop()()
        self.assertEqual(self.restarts.status()[self.profile['id']]['phase'], 'attention')
        self.assertEqual(self.hooks.calls, [])


class RemoteDrainConversionTests(unittest.TestCase):
    """Full exit (profile.remote_stop) while "SSH 작업 종료 후 설정 적용" is pending."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name))
        self.profile = self.store.add_profile('selected')
        self.store.mutate(lambda d: self.store.profile(self.profile['id'], d).update(generation='gen-1'))
        self.instances = FakeInstances(self.store)
        self.hooks = FakeHooks(self.store, self.instances)
        self.pending = []
        self.restarts = ProfileRestarts(self.store, self.instances, self.hooks, spawn=self.pending.append)

    def remote(self, stop_only=False):
        return self.restarts.schedule_remote(self.profile['id'], expected_generation='gen-1', stop_only=stop_only)

    def step(self, job):
        return self.restarts.step(self.profile['id'], job['id'])

    def job(self):
        return self.store.read()['profile_restarts'][self.profile['id']]

    def names(self):
        return [name for name, _ in self.hooks.calls]

    def test_full_exit_retires_local_wait_without_reopening_then_accepts_remote_stop(self):
        self.hooks.idle = False
        job = self.restarts.schedule(self.profile['id'], automatic_key='new-manager',
                                     expected_generation='gen-1')
        self.assertFalse(self.step(job))
        desired = self.store.profile(self.profile['id'])['policy']['desired_revision']
        self.restarts.pause_local()
        self.assertEqual(self.restarts.shutdown_status()['pending'], 1)
        with self.assertRaises(UpdateError):
            self.restarts.schedule(self.profile['id'])
        self.pending.pop(0)()
        self.assertEqual(self.restarts.shutdown_status(), dict(paused=True, active=0, pending=0))
        self.assertEqual(self.job()['code'], 'service_stopped')
        self.assertEqual(self.store.profile(self.profile['id'])['policy']['desired_revision'], desired)
        stopped = self.remote(stop_only=True)
        self.pending.pop(0)()
        self.assertEqual(self.job()['id'], stopped['id'])
        self.assertEqual(self.job()['phase'], 'complete')
        self.assertNotIn('restore', self.names())
        self.assertNotIn('close', self.names())
        self.assertNotIn('remote_start', self.names())

    def test_shutdown_waits_for_admitted_local_observation_before_reporting_quiescence(self):
        entered, release = threading.Event(), threading.Event()
        self.hooks.idle = False
        snapshot = self.hooks.snapshot_instances
        def observing(**args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('fixture release timed out')
            return snapshot(**args)
        self.hooks.snapshot_instances = observing
        self.restarts.interval = .001
        self.restarts.schedule(self.profile['id'])
        worker = threading.Thread(target=self.pending.pop())
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            self.restarts.pause_local()
            self.assertEqual(self.restarts.shutdown_status(), dict(paused=True, active=1, pending=1))
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.restarts.shutdown_status()['pending'], 0)
        self.assertEqual(self.job()['code'], 'service_stopped')
        self.assertNotIn('restore', self.names())

    def test_pause_does_not_discard_an_acquired_restart_transaction(self):
        job = self.restarts.schedule(self.profile['id'])
        self.restarts._write(self.profile['id'], job['id'], transaction_id='already-acquired')
        self.restarts.pause_local()
        with patch.object(self.restarts, '_step', return_value=False) as advance:
            self.assertFalse(self.step(job))
        advance.assert_called_once_with(self.profile['id'], job['id'])
        self.assertEqual(self.job()['transaction_id'], 'already-acquired')
        self.assertNotIn('code', self.job())

    def test_cancelled_full_exit_allows_future_local_schedule(self):
        self.restarts.pause_local()
        self.restarts.resume_local()
        job = self.restarts.schedule(self.profile['id'])
        self.assertEqual(job['phase'], 'waiting')

    def test_full_exit_converts_waiting_reopen_drain_into_the_same_stop_job(self):
        self.hooks.remote_busy = True
        job = self.remote()
        self.assertFalse(self.step(job))
        # A repeated "SSH 작업 종료 후 설정 적용" still joins the same reopen.
        self.assertEqual(self.remote()['id'], job['id'])
        stopped = self.remote(stop_only=True)
        self.assertEqual(stopped['id'], job['id'])
        self.assertEqual((stopped['stop_only'], stopped['stop_requested'], stopped['stop_converted']),
                         (True, True, True))
        self.assertTrue(self.hooks.remote['stop_only'], 'the drain transaction itself must become stop-only')
        self.assertEqual(self.hooks.calls[-1], ('downgrade', job['transaction_id']))
        # A repeated full exit joins the converted job; no second transaction.
        self.assertEqual(self.remote(stop_only=True)['id'], job['id'])
        self.assertEqual(self.names().count('downgrade'), 1)
        self.assertEqual(self.names().count('begin_remote'), 1)
        self.assertEqual(len(self.pending), 1)
        self.hooks.remote_busy = False
        self.assertTrue(self.step(job))
        done = self.job()
        self.assertEqual((done['id'], done['phase']), (job['id'], 'complete'))
        self.assertIn('종료를 확인했습니다', done['message'])
        self.assertNotIn('remote_start', self.names())

    def test_stop_after_reopen_began_drains_again_before_completing(self):
        job = self.remote()
        converted = []
        def full_exit_during_start():
            converted.append(self.remote(stop_only=True))
            # Retrying full exit joins; a restart request cannot cancel the stop.
            self.assertEqual(self.remote(stop_only=True)['id'], job['id'])
            with self.assertRaises(UpdateError):
                self.remote()
        self.hooks.during_start = full_exit_during_start
        self.assertFalse(self.step(job), 'the job must not complete after only reopening')
        self.assertEqual(self.names().count('downgrade'), 1)
        self.assertEqual(converted[0]['id'], job['id'])
        self.assertEqual((converted[0]['stop_only'], converted[0]['stop_requested']), (False, True))
        waiting = self.job()
        self.assertEqual((waiting['phase'], waiting['stop_only']), ('waiting', True))
        self.assertNotEqual(waiting['transaction_id'], job['transaction_id'])
        self.assertEqual(self.hooks.calls[-1], ('begin_remote', True))
        self.assertTrue(self.step(job))
        done = self.job()
        self.assertEqual((done['id'], done['phase']), (job['id'], 'complete'))
        self.assertIn('종료를 확인했습니다', done['message'])
        self.assertEqual(self.names().count('remote_start'), 1)
        self.assertNotIn('remote_failed', self.names())

    def test_conversion_racing_completion_reports_instead_of_claiming_stop(self):
        job = self.remote()
        self.restarts._write(self.profile['id'], job['id'], phase='complete')
        with self.assertRaises(UpdateError) as raised:
            self.restarts._convert_to_stop(self.profile['id'], job['id'])
        self.assertEqual(raised.exception.code, 'profile_prepare_busy')
        self.assertIn('방금 끝났습니다', str(raised.exception))
        self.assertNotIn('downgrade', self.names())

    def test_worker_completion_cannot_slip_between_conversion_check_and_write(self):
        # The worker's completion write must wait for an in-flight conversion
        # (state.lock); completing first would report a stop that never ran.
        job = self.remote()
        finished, workers = [], []
        downgrade = self.hooks.downgrade_remote_reconcile_to_stop
        def reopen_released_meanwhile(profile_id, transaction_id):
            # The reopen already committed and released; its worker now tries
            # to complete while this conversion is between check and write.
            self.hooks.remote['committed'] = True
            worker = threading.Thread(target=lambda: finished.append(
                self.restarts._finish_remote(profile_id, job['id'])))
            workers.append(worker)
            worker.start()
            worker.join(0.5)
            return downgrade(profile_id, transaction_id)
        self.hooks.downgrade_remote_reconcile_to_stop = reopen_released_meanwhile
        converted = self.remote(stop_only=True)
        workers[0].join(10)
        self.assertFalse(workers[0].is_alive())
        self.assertEqual((converted['phase'], converted['stop_only'], converted['stop_requested']),
                         ('waiting', False, True))
        self.assertEqual(finished, [False], 'completion must see the stop request and chain a drain')
        self.assertEqual(self.job()['phase'], 'waiting')

    def test_failed_stop_after_reopen_reports_precisely_and_full_exit_can_retry(self):
        job = self.remote()
        self.hooks.during_start = lambda: self.remote(stop_only=True)
        begin = self.hooks.begin_remote_reconcile
        def admission_busy(profile_id, **options):
            self.hooks.calls.append(('begin_refused', options.get('stop_only')))
            raise UpdateError('profile_launch_busy', '다른 프로필을 여는 작업이 아직 진행 중입니다.')
        self.hooks.begin_remote_reconcile = admission_busy
        self.assertTrue(self.step(job))
        self.assertEqual(self.hooks.calls[-1], ('begin_refused', True))
        failed = self.job()
        self.assertEqual((failed['id'], failed['phase'], failed['code']), (job['id'], 'attention', 'profile_launch_busy'))
        self.assertTrue(failed['message'].startswith('SSH 설정은 적용했지만 이어서 원격 실행을 종료하지 못했습니다.'))
        self.assertIn('다른 프로필을 여는 작업', failed['message'])
        self.assertEqual(self.names().count('remote_start'), 1)
        # Direct step fixtures do not run _run's final worker cleanup.
        self.restarts.workers.clear()
        self.hooks.begin_remote_reconcile = begin
        retried = self.remote(stop_only=True)
        self.assertNotEqual(retried['id'], job['id'])
        self.assertTrue(retried['stop_only'])

    def test_normal_busy_claim_still_refuses_remote_stop(self):
        job = self.restarts.schedule(self.profile['id'])
        with self.assertRaises(UpdateError) as raised:
            self.remote(stop_only=True)
        self.assertEqual(raised.exception.code, 'profile_prepare_busy')
        self.assertEqual(self.job()['id'], job['id'])
        self.assertNotIn('stop_requested', self.job())
        self.assertEqual(self.hooks.calls, [])

    def test_ordinary_remote_preparation_can_convert_without_a_local_restart(self):
        job = self.remote()
        # Startup preparation did not originally request an explicit drain.
        self.restarts._write(self.profile['id'], job['id'], graceful_drain=False)
        stopped = self.remote(stop_only=True)
        self.assertEqual(stopped['id'], job['id'])
        self.assertTrue(stopped['stop_only'])
        self.pending.pop()()
        self.assertEqual(self.job()['phase'], 'complete')
        self.assertNotIn('remote_start', self.names())

    def test_pending_stop_is_never_upgraded_to_reopen(self):
        self.hooks.remote_busy = True
        job = self.remote(stop_only=True)
        with self.assertRaises(UpdateError) as raised:
            self.remote()
        self.assertEqual(raised.exception.code, 'profile_prepare_busy')
        self.assertTrue(self.job()['stop_only'])
        self.assertTrue(self.hooks.remote['stop_only'])
        self.assertEqual(self.names(), ['begin_remote'])
        self.assertEqual(self.remote(stop_only=True)['id'], job['id'])

    def test_converted_reopen_is_not_reopened_by_a_later_restart_request(self):
        self.hooks.remote_busy = True
        job = self.remote()
        self.remote(stop_only=True)
        with self.assertRaises(UpdateError):
            self.remote()
        self.hooks.remote_busy = False
        self.assertTrue(self.step(job))
        self.assertNotIn('remote_start', self.names())
