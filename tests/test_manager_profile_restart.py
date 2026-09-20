from copy import deepcopy
from unittest.mock import patch
from pathlib import Path
import tempfile
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
