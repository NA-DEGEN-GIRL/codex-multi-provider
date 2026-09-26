from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, Mock, patch

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

    def test_reselecting_a_spawned_warmup_launch_lets_it_lead_the_gate_again(self):
        # Warmup spawned it and waits for its window: selecting it again makes
        # it the newest gate leader, still without a second launch.
        self.center.instances.observe.return_value={'status':'running','window_handle':None}
        for state in ('checking','opening'):
            with self.subTest(state=state):
                self.center.profile_warmup.prioritize.reset_mock()
                self.center.profile_warmup.status.return_value={'worker_active':True,
                    'profiles':[{'profile_id':'selected','state':state}]}
                self.assertEqual(self.open()['state'],'existing')
                self.center.profile_warmup.prioritize.assert_called_once_with('selected')
        for warmup in ({'worker_active':True,'profiles':[{'profile_id':'selected','state':'started'}]},
                       {'worker_active':False,'profiles':[{'profile_id':'selected','state':'opening'}]},
                       {'worker_active':True,'profiles':[]}):
            with self.subTest(warmup=warmup):
                self.center.profile_warmup.prioritize.reset_mock()
                self.center.profile_warmup.status.return_value=warmup
                self.assertEqual(self.open()['state'],'existing')
                self.center.profile_warmup.prioritize.assert_not_called()
        self.center.instances.show.assert_not_called()
        self.center.update_hooks.prioritize_launch.assert_not_called()

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

    def test_startup_passes_the_restored_profile_as_the_warmup_leader(self):
        self.center.startup_updates=Mock()
        self.center.startup_updates.start.return_value={'state':'checking'}
        self.center.instances.embed_windows=True
        self.center.profile_warmup.started=False
        selected='00000000-0000-4000-8000-00000000000A'
        self.assertEqual(self.center.dispatch('manager.startup',{'selected_profile_id':selected}),{'state':'checking'})
        self.center.profile_warmup.start.assert_called_once_with(leader=selected.lower())
        self.center.update_hooks.prioritize_launch.assert_called_once_with(selected.lower())
        for args in ({},{'selected_profile_id':'not-a-profile'},{'selected_profile_id':7},{'retry_failed':True}):
            with self.subTest(args=args):
                self.center.profile_warmup.start.reset_mock()
                self.center.update_hooks.prioritize_launch.reset_mock()
                self.center.dispatch('manager.startup',args)
                self.center.profile_warmup.start.assert_called_once_with(leader=None)
                self.center.update_hooks.prioritize_launch.assert_not_called()
        # A reconnect or retry after the pass started never reorders launches.
        self.center.profile_warmup.started=True
        self.center.update_hooks.prioritize_launch.reset_mock()
        self.center.dispatch('manager.startup',{'selected_profile_id':selected})
        self.center.update_hooks.prioritize_launch.assert_not_called()

    def test_login_open_promotes_its_warmup_entry_after_launching(self):
        self.center.update_hooks=MagicMock()
        self.center.native_login=Mock()
        order=[]
        self.center.native_login.open.side_effect=lambda profile_id:order.append('login') or {'state':'launched'}
        self.center.profile_warmup.promote.side_effect=lambda profile_id:order.append('promote')
        self.assertEqual(self.center.dispatch('profile.login',{'profile_id':'selected'}),{'state':'launched'})
        self.assertEqual(order,['login','promote'])
        self.center.profile_warmup.promote.assert_called_once_with('selected')
        self.center.native_login.open.side_effect=RuntimeError('fixture login failed')
        with self.assertRaisesRegex(RuntimeError,'fixture login failed'):
            self.center.dispatch('profile.login',{'profile_id':'selected'})
        self.assertEqual(self.center.profile_warmup.promote.call_count,2)

    def test_manager_close_cancels_pending_preloads(self):
        self.center.dispatch('manager.stop_warmup',{})
        self.center.profile_warmup.shutdown.assert_called_once_with()

    def test_failed_full_exit_resumes_launches_and_warmup_admission(self):
        self.center.dispatch('manager.resume_launches',{})
        self.center.instances.resume_launches.assert_called_once_with()
        self.center.profile_warmup.resume.assert_called_once_with()

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
