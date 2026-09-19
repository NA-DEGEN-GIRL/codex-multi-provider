from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from control_center import ControlCenter


class ProfileOpeningTests(unittest.TestCase):
    def setUp(self):
        self.center=ControlCenter.__new__(ControlCenter)
        self.profile={'id':'selected','generation':'old'}
        self.center.store=Mock()
        self.center.store.profile.return_value=self.profile
        self.center.store.read.return_value={}
        self.center.instances=Mock()
        self.center.update_hooks=Mock()
        self.center.instances.observe.return_value={'status':'not_started'}
        self.center.profile_warmup=Mock()
        self.center.profile_warmup.status.return_value={'worker_active':False,'profiles':[]}
        self.center.remote_maintenance=Mock()
        self.center.remote_maintenance.pending_on_open.return_value=False
        self.center.restarts=Mock()

    def open(self):
        return self.center.dispatch('profile.show',{'profile_id':'selected'})

    def test_preloaded_profile_returns_identity_without_electron_reopen(self):
        self.center.instances.observe.return_value={'status':'running','window_handle':123}
        result=self.open()
        self.assertEqual(result['state'],'existing')
        self.assertEqual(result['profile']['window_handle'],123)
        self.center.instances.show.assert_not_called()
        self.center.restarts.schedule.assert_not_called()
        self.center.remote_maintenance.pending_on_open.assert_not_called()

    def test_selected_queued_profile_is_prioritized_without_duplicate_launch(self):
        for state in ('queued','checking','opening'):
            with self.subTest(state=state):
                self.center.profile_warmup.status.return_value={'worker_active':True,
                    'profiles':[{'profile_id':'selected','state':state}]}
                self.assertEqual(self.open()['state'],'opening')
                self.center.profile_warmup.prioritize.assert_called_with('selected')
                self.center.update_hooks.prioritize_launch.assert_called_with('selected')
        self.center.instances.show.assert_not_called()

    def test_pending_ssh_uses_local_first_path_instead_of_whole_profile_restart(self):
        self.center.remote_maintenance.pending_on_open.return_value=True
        self.center.restarts.open_local.return_value={'state':'launched','ssh_pending':True}
        self.assertEqual(self.open()['state'],'launched')
        self.center.restarts.open_local.assert_called_once_with('selected')
        self.center.restarts.schedule.assert_not_called()

    def test_ssh_failure_retry_keeps_current_local_window(self):
        self.center.instances.observe.return_value={'status':'running','window_handle':123}
        self.center.store.read.return_value={'ssh_maintenance':{'selected':{'state':'attention'}}}
        self.open()
        self.center.restarts.open_local.assert_called_once_with('selected')
        self.center.instances.show.assert_not_called()

    def test_cold_local_profile_and_failed_warmup_use_normal_launch(self):
        self.center.profile_warmup.status.return_value={'worker_active':True,
            'profiles':[{'profile_id':'selected','state':'attention'}]}
        self.open()
        self.center.instances.show.assert_called_once_with('selected',reopen_existing=False)

    def test_manager_close_cancels_pending_preloads(self):
        self.center.dispatch('manager.stop_warmup',{})
        self.center.profile_warmup.shutdown.assert_called_once_with()

    def test_cleanup_is_scoped_to_the_requested_current_generation(self):
        pid='00000000-0000-4000-8000-000000000001'
        generation='00000000-0000-4000-8000-000000000002'
        self.center.store.profile.return_value={'id':pid,'generation':generation}
        with patch('manager_core.rust_service.enabled',return_value=True), \
             patch('manager_core.rust_service.request',return_value={'stopped':True}) as broker:
            result=self.center.dispatch('profile.cleanup',{'profile_id':pid,'generation':generation})
            self.assertTrue(result['stopped'])
            broker.assert_called_once_with('process.stop',profile_id=pid,generation=generation)
            broker.reset_mock()
            with self.assertRaises(ValueError):
                self.center.dispatch('profile.cleanup',{'profile_id':pid,
                    'generation':'00000000-0000-4000-8000-000000000003'})
            broker.assert_not_called()


if __name__=='__main__':
    unittest.main()
