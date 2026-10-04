import unittest
from unittest.mock import Mock, patch
import finish_package_update as worker


class PackageAfterExitTests(unittest.TestCase):
    def startup(self, previous, current=None, lock_effect=None, recovered=None):
        manager = Mock()
        manager.directory = __import__('pathlib').Path('unused')
        manager.status.side_effect = [previous, current or previous]
        manager.recover.return_value = recovered or {'status': 'complete'}
        with patch.object(worker, 'UpdateManager', return_value=manager), \
             patch.object(worker, '_lock_file', side_effect=lock_effect), \
             patch.object(worker, '_unlock_file'), patch.object(worker.time, 'sleep'), \
             patch.object(worker, 'finalize', return_value={'status': 'complete'}) as finalize:
            result = worker.before_start('unused')
        return result, finalize, manager

    def test_startup_finishes_pending_registration_before_profiles(self):
        result, finalize, _ = self.startup({'mode':worker.MODE, 'status':'registration_pending', 'transaction_id':'old'})
        self.assertEqual(result['status'], 'complete')
        finalize.assert_called_once()

    def test_startup_waits_existing_installer_and_does_not_install_twice(self):
        result, finalize, manager = self.startup(
            {'mode':worker.MODE, 'status':'installing', 'registration_finalize':True, 'transaction_id':'new'},
            lock_effect=[worker.UpdateError('busy', 'busy'), Mock()])
        self.assertEqual(result['status'], 'complete')
        finalize.assert_not_called()
        manager.recover.assert_called_once()

    def test_startup_unknown_installer_outcome_blocks_launch(self):
        result, finalize, _ = self.startup(
            {'mode':worker.MODE, 'status':'recovery_required', 'registration_finalize':True, 'recovery_required':True},
            recovered={'status':'recovery_required', 'recovery_required':True})
        self.assertEqual(result['status'], 'startup_blocked')
        finalize.assert_not_called()

    def test_normal_startup_does_not_start_installer(self):
        result, finalize, _ = self.startup({'status':'complete'})
        self.assertEqual(result['status'], 'not_pending')
        finalize.assert_not_called()

    def test_recovery_status_without_flag_still_blocks_startup(self):
        result, finalize, manager = self.startup(
            {'mode': worker.MODE, 'status': 'recovery_required', 'registration_finalize': True},
            recovered={'status': 'recovery_required'})
        self.assertEqual(result['status'], 'startup_blocked')
        manager.recover.assert_called_once()
        finalize.assert_not_called()

    def test_live_parent_does_not_start_update(self):
        with patch.object(worker, 'process_identity', return_value={'process_created': 123}), \
             patch.object(worker, 'UpdateManager') as manager:
            self.assertEqual(worker.finish('unused', 'tx', 10, 123, timeout=0)['status'], 'parent_still_running')
            manager.assert_not_called()

    def test_stale_transaction_does_not_install(self):
        manager = Mock()
        manager.directory = __import__('pathlib').Path('unused')
        manager.status.return_value = {'transaction_id': 'new', 'mode': worker.MODE, 'status': 'registration_pending'}
        with patch.object(worker, 'process_identity', return_value=None), \
             patch.object(worker, 'UpdateManager', return_value=manager), \
             patch.object(worker, '_lock_file'), patch.object(worker, '_unlock_file'), \
             patch.object(worker, 'finalize') as finalize:
            self.assertEqual(worker.finish('unused', 'old', 10, 123)['status'], 'superseded')
            finalize.assert_not_called()

    def test_exited_parent_and_exact_pending_transaction_runs_once(self):
        manager = Mock()
        manager.directory = __import__('pathlib').Path('unused')
        manager.status.return_value = {'transaction_id': 'tx', 'mode': worker.MODE, 'status': 'registration_pending'}
        with patch.object(worker, 'process_identity', return_value=None), \
             patch.object(worker, 'UpdateManager', return_value=manager), \
             patch.object(worker, '_lock_file'), patch.object(worker, '_unlock_file') as unlock, \
             patch.object(worker, 'finalize', return_value={'status': 'complete'}) as finalize:
            self.assertEqual(worker.finish('unused', 'tx', 10, 123)['status'], 'complete')
            finalize.assert_called_once_with(manager)
            unlock.assert_called_once()
