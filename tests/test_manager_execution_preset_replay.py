"""Held SSH turns require both private acknowledgement and observed application."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.execution_preset_replay import ERROR_CODE, ExecutionPresetReplay, SelectionSource, selection_source
from manager_core.execution_presets import ExecutionPresets
from manager_core.proxy_auth import AuthProxyResult
from manager_core.runtime_admin import AdminError
from manager_core.ssh_runtime_control import SshRuntimeControl
from manager_core.store import Store, atomic_json


class Auth:
    state = 'ready'
    execution_presets_version = 1
    account_fingerprint = 'f' * 64

    def process(self, direction, message):
        return AuthProxyResult(**{'runtime' if direction == 'frontend' else 'frontend': [message]})

    def poll(self):
        return AuthProxyResult()


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.profile, self.generation, self.thread, self.preset = (str(uuid4()) for _ in range(4))
        self.selected = dict(id=self.preset, revision=2)
        self.bindings = {self.thread: self.selected}
        self.valid = True
        self.reads = []
        def source(thread):
            self.reads.append(thread)
            if not self.valid:
                raise ValueError('generation changed')
            return thread in self.bindings, deepcopy(self.bindings.get(thread))
        self.control = SshRuntimeControl(Auth(), self.profile, self.generation, 1, lambda frame: None,
            host_alias='dev', revision='a' * 64, preset_source=source)
        self.addCleanup(self.control.close)
        self.now = 0
        self.control.preset_replay.clock = lambda: self.now
        self.control.process('frontend', {'id': 'init', 'method': 'initialize', 'params': {}})
        self.control.process('runtime', {'id': 'init', 'result': {}})
        self.control.process('frontend', {'method': 'initialized'})

    def turn(self, thread=None, request_id=1):
        return {'id': request_id, 'method': 'turn/start', 'params': {
            'threadId': thread or self.thread, 'input': [{'type': 'text', 'text': 'Original prompt'}], 'model': 'gpt-6-astra'}}

    def start(self, message=None):
        message = message or self.turn()
        result = self.control.process('frontend', message)
        self.assertEqual(len(result.runtime), 1)
        self.assertEqual(result.runtime[0]['method'], 'thread/settings/update')
        self.assertEqual(result.frontend, [])
        return result.runtime[0]

    def notify(self, selection=None, thread=None):
        return self.control.process('runtime', {'method': 'thread/settings/updated', 'params': {
            'threadId': thread or self.thread, 'threadSettings': {'executionPreset': selection}}})

    def ack(self, private):
        return self.control.process('runtime', {'id': private['id'], 'result': {}})

    def test_ack_then_notification_releases_exact_original_turn_once(self):
        original = self.turn()
        private = self.start(original)
        original['params']['input'][0]['text'] = 'Caller mutated its buffer'
        self.assertEqual(self.ack(private).runtime, [])
        status = self.control.dispatch('manager/executionPresets/status', {'threadId': self.thread}, 1, None)
        self.assertFalse(status['known'])
        outcome = self.notify(self.selected)
        self.assertEqual(outcome.runtime, [self.turn()])
        self.assertEqual(self.control.preset_replay.pending_count(), 0)
        self.assertEqual(self.ack(private).runtime, [])
        self.assertEqual(self.notify(self.selected).runtime, [])
        self.assertEqual(self.reads, [self.thread, self.thread])

    def test_notification_before_ack_waits_for_success_and_null_clear_is_explicit(self):
        self.bindings[self.thread] = None
        private = self.start()
        self.assertIsNone(private['params']['executionPreset'])
        self.assertEqual(self.notify(None).runtime, [])
        self.assertEqual(self.ack(private).runtime, [self.turn()])

    def test_missing_explicit_binding_never_applies_a_new_default_to_loaded_task(self):
        self.bindings.clear()
        self.assertEqual(self.control.process('frontend', self.turn()).runtime, [self.turn()])
        self.assertEqual(self.control.preset_replay.pending_count(), 0)

    def test_observed_current_selection_needs_no_duplicate_update_but_still_checks_generation(self):
        self.notify(self.selected)
        self.assertEqual(self.control.process('frontend', self.turn()).runtime, [self.turn()])
        self.valid = False
        result = self.control.process('frontend', self.turn(request_id=2))
        self.assertEqual(result.runtime, [])
        self.assertEqual(result.frontend[0]['error']['code'], ERROR_CODE)

    def test_frontend_spoofs_and_runtime_request_cannot_supply_acknowledgement(self):
        private = self.start()
        result = self.control.process('frontend', {'id': private['id'], 'result': {}})
        self.assertEqual(result.runtime, [])
        self.assertEqual(result.frontend[0]['error']['code'], -32043)
        self.notify(self.selected)
        spoof = dict(id=private['id'], method='server/request', result={})
        self.assertEqual(self.control.process('runtime', spoof).runtime, [])
        self.assertEqual(self.control.preset_replay.pending_count(), 1)
        self.assertEqual(self.ack(private).runtime, [self.turn()])

    def test_wrong_notification_timeout_and_late_ack_never_send_the_turn(self):
        private = self.start()
        self.ack(private)
        self.notify(dict(id=str(uuid4()), revision=1))
        self.now = 16
        outcome = self.control.poll()
        self.assertEqual(outcome.runtime, [])
        self.assertEqual(outcome.frontend[0]['id'], 1)
        self.assertEqual(self.notify(self.selected).runtime, [])
        self.assertEqual(self.ack(private).frontend, [])
        self.assertEqual(self.control.preset_replay.pending_count(), 0)

    def test_rejected_or_malformed_ack_is_safe_and_never_retried(self):
        for response in ({'error': {'message': 'private provider detail'}}, {'result': {'unknown': 'field'}}, {}):
            with self.subTest(response=response):
                private = self.start()
                outcome = self.control.process('runtime', {'id': private['id'], **response})
                self.assertEqual(outcome.runtime, [])
                self.assertEqual(outcome.frontend[0]['error']['code'], ERROR_CODE)
                self.assertNotIn('private provider detail', repr(outcome.frontend))
                self.assertEqual(self.control.preset_replay.pending_count(), 0)

    def test_generation_or_choice_change_during_confirmation_rejects_original_turn(self):
        private = self.start()
        self.notify(self.selected)
        self.valid = False
        outcome = self.ack(private)
        self.assertEqual(outcome.runtime, [])
        self.assertEqual(outcome.frontend[0]['error']['code'], ERROR_CODE)
        self.valid = True
        self.control.observer.execution_presets.clear()
        private = self.start(self.turn(request_id=2))
        self.ack(private)
        self.bindings[self.thread] = dict(id=self.preset, revision=3)
        self.assertEqual(self.notify(self.selected).runtime, [])
        self.assertEqual(self.control.preset_replay.pending_count(), 0)

    def test_disconnect_returns_pending_turn_error_and_clears_buffers(self):
        private = self.start()
        outcome = self.control.close()
        self.assertEqual(outcome.runtime, [])
        self.assertEqual(outcome.frontend[0]['id'], 1)
        self.assertEqual(self.control.preset_replay.bytes, 0)
        self.assertEqual(self.ack(private).runtime, [])

    def test_concurrent_other_task_can_continue_and_confirmations_stay_isolated(self):
        first = self.start()
        unrelated = self.turn(thread=str(uuid4()), request_id=3)
        self.assertEqual(self.control.process('frontend', unrelated).runtime, [unrelated])
        other = str(uuid4())
        self.bindings[other] = None
        second_turn = self.turn(thread=other, request_id=2)
        second = self.start(second_turn)
        self.ack(first)
        self.notify(None, thread=other)
        self.assertEqual(self.ack(second).runtime, [second_turn])
        self.assertEqual(self.notify(self.selected).runtime, [self.turn()])

    def test_pending_limits_same_task_and_maintenance_prevent_competing_mutations(self):
        self.start()
        self.assertEqual(self.control.process('frontend', self.turn(request_id=2)).runtime, [])
        with self.assertRaises(AdminError):
            self.control.dispatch('manager/maintenance/acquire', {'transactionId': str(uuid4())}, 1, None)
        self.control.preset_replay.max_pending = 1
        other = str(uuid4())
        self.bindings[other] = None
        result = self.control.process('frontend', self.turn(thread=other, request_id=3))
        self.assertEqual(result.runtime, [])
        self.assertEqual(result.frontend[0]['error']['code'], ERROR_CODE)

    def test_duplicate_public_request_does_not_create_second_turn_or_error_original(self):
        private = self.start()
        duplicate = self.control.process('frontend', self.turn())
        self.assertEqual(duplicate.runtime, [])
        self.assertEqual(duplicate.frontend, [])
        self.ack(private)
        self.assertEqual(self.notify(self.selected).runtime, [self.turn()])

    def test_reused_public_id_with_changed_input_cancels_held_turn(self):
        private = self.start()
        changed = self.turn()
        changed['params']['input'][0]['text'] = 'Different prompt'
        result = self.control.process('frontend', changed)
        self.assertEqual(result.runtime, [])
        self.assertEqual(result.frontend[0]['error']['code'], ERROR_CODE)
        self.ack(private)
        self.assertEqual(self.notify(self.selected).runtime, [])

    def test_pending_byte_limit_does_not_retain_or_forward_oversized_turn(self):
        self.control.preset_replay.max_bytes = 10
        result = self.control.process('frontend', self.turn())
        self.assertEqual(result.runtime, [])
        self.assertEqual(result.frontend[0]['error']['code'], ERROR_CODE)
        self.assertEqual(self.control.preset_replay.bytes, 0)

    def test_capability_unknown_or_auth_lost_does_not_run_old_preset(self):
        self.control.auth.execution_presets_version = 0
        self.assertEqual(self.control.process('frontend', self.turn()).runtime, [])
        self.control.auth.execution_presets_version = 1
        self.start()
        self.control.auth.state = 'refreshing'
        outcome = self.control.poll()
        self.assertEqual(outcome.runtime, [])
        self.assertEqual(outcome.frontend[0]['error']['code'], ERROR_CODE)


class SelectionSourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root)
        self.profile = self.store.add_profile('Owner')
        self.generation, self.thread = str(uuid4()), str(uuid4())
        self.binding = dict(profile_id=self.profile['id'], alias='dev', revision='a' * 64,
            remote_python='/usr/bin/python3', prepared=True, execution_presets_version=1, host_identity='b' * 64,
            remote_launcher='/home/test/.local/share/codex-control-center/profiles/' + self.profile['id'] + '/launch.py')
        self.store.mutate(lambda data: self.store.profile(self.profile['id'], data).update(
            generation=self.generation, remote_bindings=[self.binding]))
        self.path = self.store.directory / 'profiles' / self.profile['id'] / 'ssh-bindings.json'
        atomic_json(self.path, dict(profile_id=self.profile['id'], generation=self.generation, bindings=[self.binding]))
        self.authority = dict(profile_id=self.profile['id'], host_id='ssh:dev', revision='a' * 64, host_identity='b' * 64)
        self.source = SelectionSource(self.root, self.generation, self.authority)
        self.presets = ExecutionPresets(self.store)

    def test_atomic_reader_uses_explicit_host_binding_and_never_waits_for_store_lock(self):
        preset = self.presets.save(self.profile['id'], dict(name='Saved', roles=[]))
        self.presets.set_default(self.profile['id'], preset['id'], 1)
        with patch.object(Store, 'read', side_effect=AssertionError('no Store lock in pump')):
            self.assertEqual(self.source(self.thread), (False, None))
        self.presets.bind(self.profile['id'], 'local', self.thread, preset['id'], 1)
        self.assertEqual(self.source(self.thread), (False, None))
        self.presets.bind(self.profile['id'], 'ssh:dev', self.thread, preset['id'], 1)
        self.assertEqual(self.source(self.thread), (True, dict(id=preset['id'], revision=1)))
        self.presets.bind(self.profile['id'], 'ssh:dev', self.thread, None)
        self.assertEqual(self.source(self.thread), (True, None))

    def test_generation_host_and_launch_binding_drift_fail_closed(self):
        original = self.store._read_unlocked()
        for changes in ({'generation': str(uuid4())}, {'remote_bindings': [{**self.binding, 'host_identity': 'c' * 64}]}):
            with self.subTest(changes=changes):
                self.store.mutate(lambda data: self.store.profile(self.profile['id'], data).update(changes))
                with self.assertRaises(ValueError):
                    self.source(self.thread)
                atomic_json(self.store.path, original)
        atomic_json(self.path, dict(profile_id=self.profile['id'], generation=self.generation,
                                   bindings=[{**self.binding, 'revision': 'c' * 64}]))
        with self.assertRaises(ValueError):
            self.source(self.thread)

    def test_factory_skips_unmanaged_auth_and_rejects_cross_host_authority(self):
        environment = dict(CODEX_MANAGER_ROOT=str(self.root), CODEX_MANAGER_GENERATION=self.generation)
        event = dict(profile_id=self.profile['id'], alias='dev', revision='a' * 64)
        self.assertIsNone(selection_source({}, event, SimpleNamespace()))
        self.assertIsNone(selection_source(environment, event, SimpleNamespace()))
        auth = SimpleNamespace(authority=self.authority)
        self.assertIsInstance(selection_source(environment, event, auth), SelectionSource)
        with self.assertRaises(ValueError):
            selection_source(environment, {**event, 'alias': 'other'}, auth)


if __name__ == '__main__':
    unittest.main()
