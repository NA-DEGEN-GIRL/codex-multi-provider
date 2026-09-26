import base64,json,os,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from manager_core.native_login import NativeLogin
from manager_core.store import Store
from manager_core.instances import Instances


class NativeLoginTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=Store(self.root);self.p=self.store.add_profile('04')
        self.instances=Instances(self.root,self.store,None)
        self.login=NativeLogin(self.root,self.store,self.instances)
    def tearDown(self):self.tmp.cleanup()
    def prepare(self):
        with patch.object(self.instances,'prepare'),patch.object(self.instances,'observe',return_value={'status':'not_started'}):
            return self.login.prepare(self.p['id'])
    def auth(self,account='fake-account'):
        payload=base64.urlsafe_b64encode(json.dumps({'exp':time.time()+3600,'https://api.openai.com/auth':{'chatgpt_account_id':account}}).encode()).decode().rstrip('=')
        value={'tokens':{'access_token':'header.'+payload+'.signature','account_id':account}}
        (Path(self.p['home'])/'auth.json').write_text(json.dumps(value))
    def test_native_onboarding_never_copies_borrowed_credentials(self):
        old=self.root/'old';old.mkdir();(old/'auth.json').write_text('privatefixture')
        self.store.mutate(lambda d:self.store.profile(self.p['id'],d).update(source_home=str(old),account_fingerprint='old',account_missing=True))
        p=self.prepare()
        self.assertEqual(p['auth_mode'],'native');self.assertEqual(p['alias_authority'],'manager')
        self.assertEqual(p['account_fingerprint'],'old');self.assertFalse(p['account_missing'])
        self.assertFalse((Path(p['home'])/'auth.json').exists())
        self.assertEqual((old/'auth.json').read_text(),'privatefixture')
    def test_native_environment_uses_packaged_binary_and_no_borrowed_token_source(self):
        p=self.prepare();app=self.root/'installed';runtime=app/'app/resources/codex.exe';runtime.parent.mkdir(parents=True);runtime.touch()
        runtime.write_bytes(b'official CLI fixture')
        with patch('desktop_launch.find_app',return_value={'InstallLocation':str(app),'Version':'fixture'}),patch.dict(os.environ,{'OPENAI_API_KEY':'fake','CODEX_MANAGER_AUTH_SOURCE':'outside','CODEX_SQLITE_HOME':'outside'}):
            env=self.instances.environment(p)
        self.assertEqual(env['CODEX_HOME'],p['home'])
        staged=Path(env['CODEX_CLI_PATH'])
        self.assertFalse(staged.is_relative_to(app))
        self.assertEqual(staged.read_bytes(),runtime.read_bytes())
        self.assertEqual(env['CODEX_ELECTRON_USER_DATA_PATH'],p['ui_home'])
        self.assertEqual(env['CODEX_MANAGER_DESKTOP_PIPE'],'codex-manager-'+p['id'])
        self.assertNotIn('OPENAI_API_KEY',env);self.assertNotIn('CODEX_MANAGER_AUTH_SOURCE',env);self.assertNotIn('CODEX_SQLITE_HOME',env)
    def test_signed_out_is_truthful_and_saved_credential_not_serverproof(self):
        self.prepare();self.assertEqual(self.login.status(self.p['id'])['state'],'signed_out')
        self.auth();status=self.login.status(self.p['id'])
        self.assertEqual(status['state'],'credential_saved');self.assertFalse(status['server_verified'])
        self.assertNotIn('fake-account',json.dumps(status));self.assertNotIn('access_token',json.dumps(status))

    def test_login_startup_exit_is_not_reported_as_ready_window(self):
        p=self.prepare()
        process=MagicMock(pid=123)
        process.poll.return_value=1
        identity=dict(process_id=123,process_created=456,executable_path='fixture.exe')
        with patch.object(self.instances,'observe',return_value={'status':'not_started'}), \
                patch.object(self.instances,'prepare',return_value={}), \
                patch.object(self.instances,'environment',return_value={}), \
                patch('desktop_launch.find_app',return_value={}), \
                patch('manager_core.desktop_bundle.prepare',return_value={
                    'executable':'fixture.exe','Version':'fixture','desktop_isolation_revision':1}), \
                patch('manager_core.app_catalog_cache.prepare',return_value={}), \
                patch('manager_core.rust_service.enabled',return_value=False), \
                patch('manager_core.instances.subprocess.Popen',return_value=process), \
                patch('manager_core.instances.process_identity',return_value=identity), \
                patch('manager_core.instances.main_window',return_value=321):
            with self.assertRaisesRegex(RuntimeError,'로그인 창이 시작 중 종료'):
                self.instances.show(p['id'])
        self.assertNotEqual(self.store.profile(p['id']).get('window_handle'),321)
    def test_changed_account_not_silently_rebound(self):
        self.prepare();self.auth();self.login.status(self.p['id']);before=self.store.profile(self.p['id'])['account_fingerprint']
        self.auth('differentfake');status=self.login.status(self.p['id'])
        self.assertEqual(status['state'],'account_changed');self.assertEqual(self.store.profile(self.p['id'])['account_fingerprint'],before)
    def test_running_borrowed_profile_is_not_reconfigured(self):
        before=self.store.read()
        with patch.object(self.instances,'observe',return_value={'status':'running'}):
            with self.assertRaises(RuntimeError):self.login.prepare(self.p['id'])
        self.assertEqual(self.store.read(),before)

    def test_removed_login_invalidates_saved_server_status_and_quota(self):
        self.prepare();self.auth();self.login.status(self.p['id'])
        self.store.mutate(lambda data:self.store.profile(self.p['id'],data).update(
            login_state='signed_in',login_verified_at='earlier',usage={'windows':[{'remaining_percent':50}],'freshness':'live'}))
        (Path(self.p['home'])/'auth.json').unlink()
        value=self.login.status(self.p['id'])
        self.assertEqual(value['state'],'signed_out');self.assertFalse(value['server_verified'])
        saved=self.store.profile(self.p['id'])
        self.assertEqual(saved['login_state'],'signed_out');self.assertEqual(saved['usage']['freshness'],'stale')

    def test_relogin_uses_packaged_runtime_then_restores_managed_choice_after_verification(self):
        self.prepare();self.auth();self.login.status(self.p['id'])
        self.store.mutate(lambda data:self.store.profile(self.p['id'],data).update(
            runtime_channel='managed',desired_runtime_channel='managed'))
        prepared=self.prepare()
        self.assertEqual(prepared['runtime_channel'],'packaged')
        self.assertEqual(prepared['desired_runtime_channel'],'packaged')
        with patch('desktop_launch.find_app',return_value={}),patch('manager_core.login_probe.verification_runtime',return_value=self.root/'fixture.exe'),patch('manager_core.login_probe.verify',return_value={'quota_read':True}):
            self.login.verify(self.p['id'])
        saved=self.store.profile(self.p['id'])
        self.assertEqual(saved['runtime_channel'],'packaged')
        self.assertEqual(saved['desired_runtime_channel'],'managed')
        self.assertNotIn('post_login_runtime_channel',saved)

    def test_verification_reuses_the_service_package_lookup(self):
        self.prepare();self.auth()
        with patch('desktop_launch.cached_app',return_value={'Version':'fixture'}) as lookup,patch('desktop_launch.find_app',side_effect=AssertionError('uncached package lookup')),patch('manager_core.login_probe.verification_runtime',return_value=self.root/'fixture.exe') as runtime,patch('manager_core.login_probe.verify',return_value={'quota_read':True}):
            self.assertEqual(self.login.verify(self.p['id'])['state'],'signed_in')
        lookup.assert_called_once_with()
        self.assertEqual(runtime.call_args.args[1],{'Version':'fixture'})

    def test_first_verified_login_prepares_shared_runtime_for_next_open_without_restarting(self):
        self.prepare();self.auth()
        auth=Path(self.p['home'])/'auth.json';before=auth.read_bytes()
        with patch('desktop_launch.find_app',return_value={}),patch('manager_core.login_probe.verification_runtime',return_value=self.root/'fixture.exe'),patch('manager_core.login_probe.verify',return_value={'quota_read':True}),patch('manager_core.runtime_build.resolve',return_value={'capabilities':{'shared_record_catalog':True}}),patch.object(self.instances,'show') as show:
            self.login.verify(self.p['id'])
            show.assert_not_called()
        saved=self.store.profile(self.p['id'])
        self.assertEqual(saved['login_state'],'signed_in')
        self.assertEqual(saved['runtime_channel'],'packaged')
        self.assertEqual(saved['desired_runtime_channel'],'managed')
        self.assertEqual(auth.read_bytes(),before)

    def legacy_verified(self):
        self.prepare(); self.auth(); self.login.status(self.p['id'])
        def save(data):
            p = self.store.profile(self.p['id'], data)
            p.update(login_state='signed_in', login_verified_at='earlier', process_id=123)
            p.pop('desired_runtime_channel', None)
        self.store.mutate(save)

    def migrate(self):
        with patch('manager_core.runtime_build.resolve', return_value={'capabilities': {
            'shared_record_catalog': True, 'managed_store_binding': True}}):
            return self.login.migrate_verified_shared_history()

    def test_older_verified_login_gets_shared_history_without_changing_running_process(self):
        self.legacy_verified()
        before = self.store.profile(self.p['id'])
        auth = Path(before['home']) / 'auth.json'; auth_before = auth.read_bytes()
        with patch.object(self.instances, 'show') as show, patch.object(self.instances, 'observe') as observe:
            self.assertEqual(self.migrate(), [self.p['id']])
            show.assert_not_called(); observe.assert_not_called()
        after = self.store.profile(self.p['id'])
        self.assertEqual(after, {**before, 'desired_runtime_channel': 'managed'})
        self.assertEqual(auth.read_bytes(), auth_before)
        revision = self.store.read()['revision']
        self.assertEqual(self.migrate(), [])
        self.assertEqual(self.store.read()['revision'], revision)

    def test_shared_history_migration_keeps_explicit_login_mode_and_checks_identity(self):
        self.legacy_verified()
        self.store.mutate(lambda d: self.store.profile(self.p['id'], d).update(desired_runtime_channel='packaged'))
        self.assertEqual(self.migrate(), [])
        self.store.mutate(lambda d: self.store.profile(self.p['id'], d).pop('desired_runtime_channel'))
        self.auth('different-account')
        self.assertEqual(self.migrate(), [])
        self.assertNotIn('desired_runtime_channel', self.store.profile(self.p['id']))

    def test_shared_history_migration_requires_verified_account_and_ready_runtime(self):
        self.legacy_verified()
        with patch('manager_core.runtime_build.resolve', return_value={'capabilities': {}}):
            self.assertEqual(self.login.migrate_verified_shared_history(), [])
        self.store.mutate(lambda d: self.store.profile(self.p['id'], d).update(login_state='credential_saved'))
        self.assertEqual(self.migrate(), [])

    def test_new_login_uses_common_storage_without_a_second_runtime(self):
        with patch.object(self.login,'shared_login_ready',return_value=True), \
                patch.object(self.instances,'observe',return_value={'status':'not_started'}), \
                patch.object(self.instances,'prepare') as prepare:
            p=self.login.prepare(self.p['id'])
        self.assertEqual(p['runtime_channel'],'managed')
        self.assertEqual(p['desired_runtime_channel'],'managed')
        self.assertTrue(p['native_login_pending'])
        self.assertEqual(prepare.call_args.args[0]['runtime_channel'],'managed')

    def test_new_login_environment_has_private_auth_and_common_records(self):
        from manager_core.runtime_proxy import runtime_environment
        p=self.prepare();p.update(runtime_channel='managed',native_login_pending=True,generation='fixture')
        runtime=self.root/'fixture.exe';runtime.touch()
        proxy=self.root/'artifacts/manager/Codex.ControlCenter.RuntimeProxy.exe'
        proxy.parent.mkdir(parents=True);proxy.touch()
        common=self.root/'common';common.mkdir()
        with patch('manager_core.runtime_build.resolve',return_value={'runtime':str(runtime),
                'capabilities':{'shared_record_catalog':True,'canonical_record_storage':True}}), \
                patch('manager_core.canonical_storage.migrate',return_value={'home':str(common)}), \
                patch.dict(os.environ,{'CODEX_MANAGER_AUTH_SOURCE':'wrong-profile','CODEX_HOME':'wrong-home'}):
            env=self.instances.environment(p)
        self.assertEqual(env['CODEX_MANAGER_NATIVE_LOGIN'],'1')
        self.assertEqual(env['CODEX_HOME'],p['home'])
        self.assertEqual(env['CODEX_RECORD_HOME'],str(common))
        self.assertEqual(env['CODEX_SQLITE_HOME'],str(common))
        self.assertEqual(env['CODEX_CLI_PATH'],str(proxy))
        self.assertNotIn('CODEX_MANAGER_AUTH_SOURCE',env)
        child=runtime_environment(env)
        self.assertNotIn('CODEX_MANAGER_NATIVE_LOGIN',child)
        self.assertEqual(child['CODEX_RECORD_HOME'],str(common))
        self.assertEqual(child['CODEX_HOME'],p['home'])

    def test_legacy_unverified_login_migrates_on_next_open_without_changing_live_window(self):
        self.prepare();self.auth()
        before=(Path(self.p['home'])/'auth.json').read_bytes()
        self.store.mutate(lambda d:self.store.profile(self.p['id'],d).update(process_id=123))
        with patch.object(self.login,'shared_login_ready',return_value=True), \
                patch.object(self.instances,'show') as show:
            self.assertEqual(self.login.migrate_verified_shared_history(),[self.p['id']])
            self.assertEqual(self.login.migrate_verified_shared_history(),[])
            show.assert_not_called()
        p=self.store.profile(self.p['id'])
        self.assertEqual(p['process_id'],123)
        self.assertEqual(p['runtime_channel'],'packaged')
        self.assertEqual(p['desired_runtime_channel'],'managed')
        self.assertTrue(p['native_login_pending'])
        self.assertEqual((Path(self.p['home'])/'auth.json').read_bytes(),before)

    def test_saved_login_completes_only_for_current_runtime_binding_without_restart_or_probe(self):
        from manager_core.proxy_auth import account_fingerprint
        self.prepare();self.auth()
        self.store.mutate(lambda d:self.store.profile(self.p['id'],d).update(
            runtime_channel='managed',native_login_pending=True,generation='new'))
        p=self.store.profile(self.p['id'])
        observer={'generation':'old','native_account':{
            'matches':True,'account_fingerprint':account_fingerprint('fake-account')}}
        with patch.object(self.instances,'show') as show,patch.object(self.login,'verify') as verify:
            self.login.complete_shared_login({**p,'runtime_state':observer})
            self.assertTrue(self.store.profile(p['id'])['native_login_pending'])
            observer['generation']='new'
            done=self.login.complete_shared_login({**p,'runtime_state':observer})
            self.assertEqual(done['login_state'],'credential_saved')
            self.assertNotIn('native_login_pending',done)
            self.assertEqual(done['desired_runtime_channel'],'managed')
            self.assertNotIn('login_verified_at',done)
            show.assert_not_called();verify.assert_not_called()


if __name__=='__main__':unittest.main()
