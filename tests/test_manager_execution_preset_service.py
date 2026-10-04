"""Preset selection changes task metadata, never profile lifecycle or credentials."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.execution_preset_service import dispatch, _selection_requires_preparation
from manager_core.app_transport import RuntimeObserver
from manager_core.runtime_admin import AdminError, validate_request, sanitize_result, MaintenanceBarrier
from manager_core.store import Store
from manager_core.providers import ProviderRegistry


class ExecutionPresetServiceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root)
        self.profile = self.store.add_profile('Account')
        self.store.mutate(lambda data: self.store.profile(self.profile['id'], data).update(generation=str(uuid4())))
        self.center = SimpleNamespace(root=self.root, store=self.store, providers=ProviderRegistry(self.root), instances=Mock())
        self.center.instances.observe.return_value = {'status': 'running', 'runtime_state': {'execution_presets_version': 1}}
        self.thread = str(uuid4())
        self.preset = str(uuid4())
        self.args = {'profile_id': self.profile['id'], 'host_id': 'local', 'thread_id': self.thread,
                     'preset_id': self.preset, 'revision': 1}

    def test_loaded_selection_uses_only_scoped_next_turn_rpc(self):
        with patch('manager_core.execution_preset_service.ExecutionPresets') as factory, patch(
                'manager_core.execution_preset_service.AdminClient') as admin:
            registry = factory.return_value
            registry.bind.return_value = {'preset_id': self.preset, 'revision': 1}
            registry.publish_registry.return_value = {'runtime_prepare_required': False}
            admin.return_value.request.side_effect = [{'thread': {'status': {'type': 'active'}}}, {}]
            result = dispatch(self.center, 'presets.select', self.args)
            self.assertEqual(result['state'], 'queued')
            self.assertEqual(admin.return_value.request.call_args.args,
                ('thread/settings/update', {'threadId': self.thread, 'executionPreset': {'id': self.preset, 'revision': 1}}))
            self.assertEqual(self.center.instances.method_calls, [unittest.mock.call.observe(self.store.profile(self.profile['id']))])

    def test_unknown_prepared_role_preserves_active_runtime(self):
        with patch('manager_core.execution_preset_service.ExecutionPresets') as factory, patch(
                'manager_core.execution_preset_service.AdminClient') as admin:
            factory.return_value.bind.return_value = {'preset_id': self.preset, 'revision': 1}
            factory.return_value.publish_registry.return_value = {'runtime_prepare_required': True}
            result = dispatch(self.center, 'presets.select', self.args)
            self.assertEqual(result['state'], 'prepare_required')
            admin.assert_not_called()
            self.center.instances.assert_not_called()

    def test_uncertain_rpc_is_not_retried_or_reported_applied(self):
        with patch('manager_core.execution_preset_service.ExecutionPresets') as factory, patch(
                'manager_core.execution_preset_service.AdminClient') as admin:
            factory.return_value.bind.return_value = {'preset_id': self.preset, 'revision': 1}
            factory.return_value.publish_registry.return_value = {'runtime_prepare_required': False}
            admin.return_value.request.side_effect = [{'thread': {'status': {'type': 'idle'}}}, AdminError('timeout', uncertain=True)]
            result = dispatch(self.center, 'presets.select', self.args)
            self.assertEqual(result['state'], 'confirmation_pending')
            self.assertEqual(admin.return_value.request.call_count, 2)

    def test_remote_selection_cannot_publish_windows_account_paths(self):
        with patch('manager_core.execution_preset_service.ExecutionPresets') as factory:
            factory.return_value.bind.return_value = {'preset_id': self.preset, 'revision': 1}
            result = dispatch(self.center, 'presets.select', {**self.args, 'host_id': 'ssh:example'})
            self.assertEqual(result['state'], 'prepare_required')
            factory.return_value.bind.assert_called_once_with(self.profile['id'], 'ssh:example', self.thread, self.preset, 1)
            factory.return_value.publish_registry.assert_not_called()

    def test_admin_only_accepts_approved_preset_reference_and_clear(self):
        value = {'threadId': self.thread, 'executionPreset': {'id': self.preset, 'revision': 1}}
        self.assertEqual(validate_request('thread/settings/update', value), value)
        clear = {'threadId': self.thread, 'executionPreset': None}
        self.assertEqual(validate_request('thread/settings/update', clear), clear)
        for params in ({**value, 'approvalPolicy': 'never'}, {'threadId': self.thread},
                       {**value, 'executionPreset': {'id': self.preset, 'revision': True}},
                       {**value, 'executionPreset': {'id': self.preset, 'revision': 1, 'path': 'untrusted'}}):
            with self.subTest(params=params), self.assertRaises(AdminError):
                validate_request('thread/settings/update', params)
        self.assertEqual(sanitize_result('thread/settings/update', value, {}), {})
        with self.assertRaises(AdminError):
            sanitize_result('thread/settings/update', value, {'secret': 'not a queue acknowledgment'})
        barrier = MaintenanceBarrier(str(uuid4()))
        barrier.transaction_id = str(uuid4())
        with self.assertRaises(AdminError):
            barrier.authorize_admin('thread/settings/update', None)

    def test_unprepared_new_preset_does_not_block_a_prepared_selection(self):
        published = {'runtime_prepare_required': True, 'unprepared_presets': [{'id': str(uuid4()), 'revision': 1}]}
        self.assertFalse(_selection_requires_preparation(published, {'preset_id': self.preset, 'revision': 1}))
        self.assertFalse(_selection_requires_preparation(published, None))

    def test_runtime_observer_retains_only_applied_preset_identity(self):
        observer = RuntimeObserver(self.profile['id'])
        reference = {'id': self.preset, 'revision': 2}
        observer.consume('server', {'method': 'thread/settings/updated', 'params': {'threadId': self.thread,
            'threadSettings': {'executionPreset': reference, 'instructions': 'never persist this'}}})
        self.assertEqual(observer.snapshot()['execution_presets'], {self.thread: reference})
        observer.consume('server', {'method': 'thread/settings/updated', 'params': {'threadId': self.thread,
            'threadSettings': {'executionPreset': None}}})
        self.assertEqual(observer.snapshot()['execution_presets'], {self.thread: None})

    def test_status_displays_runtime_inherited_default_and_distinguishes_unknown(self):
        with patch('manager_core.execution_preset_service.ExecutionPresets') as factory:
            registry = factory.return_value
            registry.get_for_task.return_value = None
            registry.get.return_value = {'id': self.preset, 'revision': 2, 'name': '검토 조합'}
            self.center.instances.observe.return_value = {'runtime_state': {'execution_presets': {
                self.thread: {'id': self.preset, 'revision': 2}}}}
            result = dispatch(self.center, 'presets.status', self.args)
            self.assertEqual(result['preset']['name'], '검토 조합')
            self.assertTrue(result['observed_preset_known'])
            self.assertFalse(result['confirmation_pending'])
            self.center.instances.observe.return_value = {'runtime_state': {'execution_presets': {self.thread: None}}}
            result = dispatch(self.center, 'presets.status', self.args)
            self.assertTrue(result['observed_preset_known'])
            self.assertIsNone(result['preset'])
            self.center.instances.observe.return_value = {'runtime_state': {}}
            result = dispatch(self.center, 'presets.status', self.args)
            self.assertFalse(result['observed_preset_known'])
            registry.bind.assert_not_called()

    def test_status_keeps_desired_selection_when_native_is_still_old(self):
        with patch('manager_core.execution_preset_service.ExecutionPresets') as factory:
            factory.return_value.get_for_task.return_value = {'preset_id': self.preset, 'revision': 3,
                'preset': {'name': '새 조합'}, 'runtime_prepare_required': True}
            self.center.instances.observe.return_value = {'runtime_state': {'execution_presets': {self.thread: None}}}
            result = dispatch(self.center, 'presets.status', self.args)
            self.assertEqual(result['preset']['name'], '새 조합')
            self.assertTrue(result['confirmation_pending'])
            self.assertNotIn('runtime_prepare_required', result)


if __name__ == '__main__':
    unittest.main()
