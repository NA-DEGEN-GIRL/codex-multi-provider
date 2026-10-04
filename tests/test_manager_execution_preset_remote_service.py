"""SSH task selection is bound to the live profile, host and definition."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.execution_preset_service import dispatch
from manager_core.execution_preset_remote_service import canonical_host, publish_connected_hosts
from manager_core.runtime_admin import AdminError, sanitize_result, validate_request
from manager_core.ssh_runtime_control import endpoint_id
from manager_core.store import Store, atomic_json


class RemoteSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root)
        profile = self.store.add_profile('Owner')
        self.profile_id, self.generation = profile['id'], str(uuid4())
        self.thread, self.preset_id = str(uuid4()), str(uuid4())
        self.binding = dict(profile_id=self.profile_id, alias='dev', revision='a' * 64,
            remote_python='/usr/bin/python3', prepared=True, host_identity='b' * 64,
            remote_launcher='/home/test/.local/share/codex-control-center/profiles/' + self.profile_id + '/launch.py',
            execution_presets_version=1)
        self.store.mutate(lambda data: self.store.profile(self.profile_id, data).update(
            generation=self.generation, remote_bindings=[self.binding]))
        self.path = self.store.directory / 'profiles' / self.profile_id / 'ssh-bindings.json'
        atomic_json(self.path, dict(profile_id=self.profile_id, generation=self.generation, bindings=[self.binding]))
        self.center = SimpleNamespace(root=self.root, store=self.store, providers=Mock(), instances=Mock(), remote=Mock())
        self.center.remote.publish_execution_presets.return_value = {'runtime_prepare_required': False}
        self.args = dict(profile_id=self.profile_id, host_id='remote-ssh-discovered:dev', thread_id=self.thread,
                         preset_id=self.preset_id, revision=2)
        self.applied = {'id': self.preset_id, 'revision': 2}
        self.observed = dict(known=False, version=1, executionPreset=None, sshBinding={
            'profileId': self.profile_id, 'hostAlias': 'dev', 'revision': self.binding['revision']})
        self.factory = patch('manager_core.execution_preset_service.ExecutionPresets').start()
        self.addCleanup(patch.stopall)
        self.registry = self.factory.return_value
        self.registry.bind.return_value = {'preset_id': self.preset_id, 'revision': 2}
        self.registry.get_for_task.return_value = None
        self.admin_factory = patch('manager_core.execution_preset_remote_service.AdminClient').start()
        self.admin = self.admin_factory.return_value
        self.admin.request.side_effect = [self.observed, {'thread': {'status': {'type': 'active'}}}, {}]

    def test_selected_host_receives_only_next_turn_update_without_lifecycle(self):
        result = dispatch(self.center, 'presets.select', self.args)
        self.assertEqual('queued', result['state'])
        self.assertTrue(result['confirmation_pending'])
        self.admin_factory.assert_called_once_with(self.root, endpoint_id(self.profile_id, 'dev'), self.generation)
        self.assertEqual(self.admin.request.call_args.args, ('thread/settings/update', {
            'threadId': self.thread, 'executionPreset': self.applied}))
        self.registry.bind.assert_called_once_with(self.profile_id, 'ssh:dev', self.thread, self.preset_id, 2)
        self.registry.publish_registry.assert_not_called()
        self.assertEqual([], self.center.instances.method_calls)

    def test_cold_task_retains_selection_without_loading_it(self):
        self.admin.request.side_effect = [self.observed, {'thread': {'status': {'type': 'notLoaded'}}}]
        result = dispatch(self.center, 'presets.select', self.args)
        self.assertEqual('saved', result['state'])
        self.assertEqual(2, self.admin.request.call_count)

    def test_different_live_revision_prevents_publication(self):
        self.observed['sshBinding']['revision'] = 'c' * 64
        result = dispatch(self.center, 'presets.select', self.args)
        self.assertEqual('confirmation_pending', result['state'])
        self.center.remote.publish_execution_presets.assert_not_called()
        self.assertEqual(1, self.admin.request.call_count)

    def test_profile_relaunch_during_publish_prevents_update(self):
        def publish(*_):
            self.store.mutate(lambda data: self.store.profile(self.profile_id, data).update(generation=str(uuid4())))
            return {'runtime_prepare_required': False}
        self.center.remote.publish_execution_presets.side_effect = publish
        result = dispatch(self.center, 'presets.select', self.args)
        self.assertEqual('confirmation_pending', result['state'])
        self.assertEqual(1, self.admin.request.call_count)

    def test_new_role_saves_pending_without_restart_or_config_replacement(self):
        self.center.remote.publish_execution_presets.return_value = {'runtime_prepare_required': True,
            'unprepared_presets': [{'id': self.preset_id, 'revision': 2}]}
        result = dispatch(self.center, 'presets.select', self.args)
        self.assertEqual('prepare_required', result['state'])
        self.assertEqual(1, self.admin.request.call_count)
        self.assertEqual([], self.center.instances.method_calls)

    def test_uncertain_update_is_never_retried(self):
        self.admin.request.side_effect = [self.observed, {'thread': {'status': {'type': 'idle'}}}, AdminError('timeout', uncertain=True)]
        result = dispatch(self.center, 'presets.select', self.args)
        self.assertEqual('confirmation_pending', result['state'])
        self.assertEqual(3, self.admin.request.call_count)

    def test_status_distinguishes_observed_null_from_unknown_and_inherited_default(self):
        for applied in (None, self.applied):
            with self.subTest(applied=applied):
                self.registry.get.return_value = {'id': self.preset_id, 'revision': 2, 'name': 'Review'}
                self.admin.request.side_effect = [{**self.observed, 'known': True, 'executionPreset': applied}]
                result = dispatch(self.center, 'presets.status', self.args)
                self.assertTrue(result['observed_preset_known'])
                self.assertFalse(result['confirmation_pending'])
                self.assertEqual(applied, result['observed_preset'])
        self.center.remote.publish_execution_presets.assert_not_called()

    def test_invalid_host_cannot_create_a_binding(self):
        for host in ('ssh:../dev', 'https://example.test', 'remote-ssh-discovered:'):
            with self.subTest(host=host), self.assertRaises(ValueError):
                dispatch(self.center, 'presets.select', {**self.args, 'host_id': host})
        self.registry.bind.assert_not_called()
        self.assertEqual(('ssh:dev', 'dev'), canonical_host('remote-ssh-discovered:dev'))

    def test_admin_status_has_no_history_or_authentication_payload(self):
        params = {'threadId': self.thread}
        self.assertEqual(params, validate_request('manager/executionPresets/status', params))
        self.assertEqual(self.observed, sanitize_result('manager/executionPresets/status', params,
            {**self.observed, 'history': 'private', 'token': 'private'}))
        with self.assertRaises(AdminError):
            validate_request('manager/executionPresets/status', {**params, 'accountId': str(uuid4())})
        with self.assertRaises(AdminError):
            sanitize_result('manager/executionPresets/status', params, {**self.observed, 'known': False, 'executionPreset': self.applied})

    def test_default_save_and_delete_publish_connected_host_without_lifecycle_or_task_update(self):
        self.registry.publish_registry.return_value = {'runtime_prepare_required': False}
        self.registry.set_default.return_value = {'profile_id': self.profile_id, 'default': {
            'preset_id': self.preset_id, 'revision': 2}}
        self.registry.save.return_value = {'id': self.preset_id, 'revision': 2}
        self.registry.delete.return_value = {'deleted': True}
        for command in ('presets.default', 'presets.save', 'presets.delete'):
            with self.subTest(command=command):
                self.admin.request.reset_mock()
                self.admin.request.side_effect = [self.observed]
                self.center.remote.publish_execution_presets.reset_mock()
                args = {**self.args, 'preset': {'name': 'Saved', 'roles': []}, 'expected_revision': 2}
                result = dispatch(self.center, command, args)
                self.assertEqual(result['remote_publication'], {'dev': 'published'})
                self.center.remote.publish_execution_presets.assert_called_once()
                self.assertEqual(self.admin.request.call_count, 1)
                self.assertEqual(self.admin.request.call_args.args[0], 'manager/executionPresets/status')
                self.assertEqual(self.center.instances.method_calls, [])

    def test_offline_default_is_saved_with_pending_host_and_no_lifecycle(self):
        self.registry.set_default.return_value = {'profile_id': self.profile_id, 'default': {
            'preset_id': self.preset_id, 'revision': 2}}
        self.registry.publish_registry.return_value = {'runtime_prepare_required': False}
        self.admin.request.side_effect = AdminError('endpoint_unavailable')
        result = dispatch(self.center, 'presets.default', self.args)
        self.assertEqual(result['remote_publication'], {'dev': 'pending'})
        self.registry.set_default.assert_called_once_with(self.profile_id, self.preset_id, 2)
        self.center.remote.publish_execution_presets.assert_not_called()
        self.assertEqual(self.center.instances.method_calls, [])

    def test_fanout_skips_runtime_without_capability_and_does_not_block_ready_preset(self):
        self.admin.request.side_effect = [{**self.observed, 'version': 0}]
        profile = self.store.profile(self.profile_id)
        self.assertEqual(publish_connected_hosts(self.center, profile), {'dev': 'prepare_required'})
        self.center.remote.publish_execution_presets.assert_not_called()
        self.admin.request.side_effect = [self.observed]
        self.center.remote.publish_execution_presets.return_value = {'runtime_prepare_required': True,
            'unprepared_presets': [{'id': str(uuid4()), 'revision': 1}]}
        self.assertEqual(publish_connected_hosts(self.center, profile,
            {'preset_id': self.preset_id, 'revision': 2}), {'dev': 'published'})

    def test_generation_changed_while_observing_fanout_prevents_publication(self):
        profile = self.store.profile(self.profile_id)
        def observed(*args, **kwargs):
            self.store.mutate(lambda data: self.store.profile(self.profile_id, data).update(generation=str(uuid4())))
            return self.observed
        self.admin.request.side_effect = observed
        self.assertEqual(publish_connected_hosts(self.center, profile), {'dev': 'pending'})
        self.center.remote.publish_execution_presets.assert_not_called()

    def test_binding_changed_while_observing_fanout_prevents_publication(self):
        profile = self.store.profile(self.profile_id)
        def observed(*args, **kwargs):
            atomic_json(self.path, dict(profile_id=self.profile_id, generation=self.generation,
                bindings=[{**self.binding, 'revision': 'c' * 64}]))
            return self.observed
        self.admin.request.side_effect = observed
        self.assertEqual(publish_connected_hosts(self.center, profile), {'dev': 'pending'})
        self.center.remote.publish_execution_presets.assert_not_called()

    def test_generation_changed_during_fanout_is_pending_and_never_retried(self):
        profile = self.store.profile(self.profile_id)
        self.admin.request.side_effect = [self.observed]
        def publish(*args):
            self.store.mutate(lambda data: self.store.profile(self.profile_id, data).update(generation=str(uuid4())))
            return {'runtime_prepare_required': False}
        self.center.remote.publish_execution_presets.side_effect = publish
        self.assertEqual(publish_connected_hosts(self.center, profile), {'dev': 'pending'})
        self.center.remote.publish_execution_presets.assert_called_once()


if __name__ == '__main__':
    unittest.main()
