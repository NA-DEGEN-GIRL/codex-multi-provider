import concurrent.futures
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from manager_core.store import Store
from manager_core.catalog import list_catalog
from manager_core.instances import Instances
from control_center import ControlCenter


def legacy_linked_profile(store,alias,source_home):
    """A profile imported from the retired account tool, as older releases saved it."""
    profile=store.add_profile(alias);uid=str(uuid4())
    def link(data):
        item=store.profile(profile['id'],data)
        item.update(usage_account_id=uid,source_home=source_home)
        Store._source(data,source_home,'usage:'+uid,alias)
        return item
    return store.mutate(link)


class ControlCenterTests(unittest.TestCase):
    def test_navigation_reuses_live_instance_without_touching_its_window(self):
        instance = Instances(self.root, self.store, None)
        active = dict(status='running', window_handle=123, executable_path='C:/fixture/ChatGPT.exe')
        with patch.object(instance, 'observe', return_value=active), \
             patch.object(instance, 'environment') as environment, \
             patch('manager_core.instances.subprocess.Popen') as launch:
            result = instance.show(self.one['id'], reopen_existing=False)
        self.assertEqual(result['state'], 'existing')
        self.assertEqual(result['profile']['window_handle'], 123)
        environment.assert_not_called()
        launch.assert_not_called()

    def test_existing_window_is_reactivated_by_its_own_electron_instance(self):
        instance = Instances(self.root, self.store, None)
        active = dict(status='running', window_handle=123, executable_path='C:/fixture/ChatGPT.exe')
        with patch.object(instance, 'observe', return_value=active), \
             patch.object(instance, 'environment', return_value={}), \
             patch('manager_core.instances.subprocess.Popen') as launch:
            result = instance.show(self.one['id'])
        self.assertEqual(result['state'], 'reopen_requested')
        self.assertEqual(launch.call_args.args[0], ['C:/fixture/ChatGPT.exe', '--user-data-dir=' + self.one['ui_home']])
        launch.return_value.wait.assert_called_once_with(timeout=4)

    def test_live_process_without_window_gets_only_its_scoped_reopen(self):
        instance = Instances(self.root, self.store, None)
        active = dict(status='running', window_handle=None, executable_path='C:/fixture/ChatGPT.exe')
        with patch.object(instance, 'observe', return_value=active), \
             patch.object(instance, 'environment', return_value={'fixture': 'private-env'}), \
             patch('manager_core.instances.subprocess.Popen') as launch:
            result = instance.show(self.one['id'])
        self.assertEqual(result['state'], 'reopen_requested')
        self.assertEqual(launch.call_args.args[0], ['C:/fixture/ChatGPT.exe', '--user-data-dir=' + self.one['ui_home']])
        self.assertEqual(launch.call_count, 1)
        self.assertNotIn('private-env', json.dumps(result))

    def setUp(self):
        self.quota_guard=patch('manager_core.usage_refresh.read_quota',side_effect=RuntimeError('fixture: no network'))
        self.quota_guard.start();self.addCleanup(self.quota_guard.stop)
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.store=Store(self.root)
        self.one=self.store.add_profile('01');self.two=self.store.add_profile('02')
        self.thread=str(uuid4())
    def tearDown(self):self.temp.cleanup()
    def link(self):return self.store.shortcut_add('작업',self.one['id'],self.thread,'local','manager:'+self.one['id'])

    def test_catalog_uses_current_name_and_reads_renames_without_legacy_title_column(self):
        for legacy_title in (True,False):
            with self.subTest(legacy_title=legacy_title):
                home=self.root/('legacy-title' if legacy_title else 'name-only');home.mkdir()
                path=home/'state_5.sqlite'
                source=dict(id='fixture',home=str(home),host_id='local',alias='A')
                with closing(sqlite3.connect(path)) as db, db:
                    title_column=',title TEXT' if legacy_title else ''
                    db.execute('CREATE TABLE threads(id TEXT,name TEXT,preview TEXT'+title_column+')')
                    db.execute('INSERT INTO threads(id,name,preview) VALUES (?,?,?)',
                               (self.thread,'  Initial name  ','First message preview'))
                    if legacy_title:db.execute('UPDATE threads SET title=?',('Older derived title',))
                self.assertEqual(list_catalog([source])['conversations'][0]['title'],'Initial name')
                with closing(sqlite3.connect(path)) as db, db:db.execute('UPDATE threads SET name=?',('수정한 작업 이름',))
                self.assertEqual(list_catalog([source])['conversations'][0]['title'],'수정한 작업 이름')
                with closing(sqlite3.connect(path)) as db, db:db.execute('UPDATE threads SET name=NULL')
                fallback='Older derived title' if legacy_title else 'First message preview'
                self.assertEqual(list_catalog([source])['conversations'][0]['title'],fallback)
    def test_move_only_changes_target(self):
        item=self.link();moved=self.store.shortcut_move(item['id'],self.two['id'])
        self.assertEqual(moved['source_store_id'],item['source_store_id']);self.assertEqual(moved['thread_id'],self.thread)
        self.assertEqual(moved['profile_id'],self.two['id'])
        self.assertEqual(self.store.profile(self.one['id'])['status'],'not_started')
    def test_shortcut_rename_and_move_preserve_canonical_identity(self):
        item=self.link()
        renamed=self.store.shortcut_rename(item['id'],'쉬운 별칭')
        moved=self.store.shortcut_move(item['id'],self.two['id'])
        self.assertEqual(renamed,{**item,'alias':'쉬운 별칭','revision':1})
        self.assertEqual(moved,{**renamed,'profile_id':self.two['id'],'revision':2})
    def test_shortcut_reorder_moves_one_link_and_keeps_identity(self):
        links=[self.store.shortcut_add(f'작업 {n}',self.one['id'],str(uuid4()),'local','manager:'+self.one['id']) for n in range(3)]
        ids=[link['id'] for link in links]
        result=self.store.shortcut_reorder(ids[2],ids[0],'before')
        self.assertEqual(result['shortcut_ids'],[ids[2],ids[0],ids[1]])
        self.assertEqual(self.store.shortcut_reorder(ids[2],ids[1],'after')['shortcut_ids'],[ids[0],ids[1],ids[2]])
        self.assertEqual(self.store.read()['shortcuts'],links)
        with self.assertRaises(ValueError):self.store.shortcut_reorder(ids[0],ids[1],'inside')
        with self.assertRaises(ValueError):self.store.shortcut_reorder(ids[0],str(uuid4()),'before')
    def test_delete_undo_preserves_source_files(self):
        marker=self.root/'original.jsonl';marker.write_text('preserve',encoding='utf-8')
        item=self.link();result=self.store.shortcut_delete(item['id'])
        self.assertFalse(result['record_deleted']);self.assertEqual(self.store.read()['shortcuts'],[])
        restored=self.store.shortcut_undo();self.assertEqual(item,restored);self.assertEqual(marker.read_text(),'preserve')
    def test_invalid_source_and_traversal_rejected(self):
        with self.assertRaises(ValueError):self.store.shortcut_add('x',self.one['id'],self.thread,'ssh:other','manager:'+self.one['id'])
        with self.assertRaises(ValueError):self.store.profile('../../escape')
        with self.assertRaises(ValueError):self.store.shortcut_delete('not-a-uuid')

    def test_remote_source_keeps_linux_path_and_separate_host_identity(self):
        pid=self.one['id'];base='/home/test/.local/share/codex-control-center/profiles/'+pid
        binding=dict(profile_id=pid,alias='remote-dev',prepared=True,revision='a'*64,
                     remote_python='/usr/bin/python3',remote_launcher=base+'/launch.py',remote_profile_home=base+'/codex')
        self.store.mutate(lambda data:Store.remote_source(data,binding,'04'))
        self.store.mutate(lambda data:Store.remote_source(data,binding,'renamed'))
        sources=[s for s in self.store.read()['sources'] if s['host_id']=='ssh:remote-dev']
        self.assertEqual(len(sources),1)
        self.assertEqual(sources[0]['home'],base+'/codex')
        self.assertEqual(sources[0]['alias'],'renamed')
        link=self.store.shortcut_add('remote',pid,self.thread,'ssh:remote-dev','manager:'+pid)
        self.assertEqual(link['host_id'],'ssh:remote-dev')

    def test_remote_source_rejects_conflicting_record_location(self):
        pid=self.one['id'];base='/home/test/.local/share/codex-control-center/profiles/'+pid
        binding=dict(profile_id=pid,alias='remote-dev',prepared=True,revision='a'*64,
                     remote_python='/usr/bin/python3',remote_launcher=base+'/launch.py',remote_profile_home='/home/test/.codex')
        before=self.store.read()
        with self.assertRaises(ValueError):self.store.mutate(lambda data:Store.remote_source(data,binding,'04'))
        self.assertEqual(before,self.store.read())
    def test_concurrent_writers_do_not_lose_links(self):
        def add(_):return Store(self.root).shortcut_add('병렬',self.one['id'],str(uuid4()),'local','manager:'+self.one['id'])
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(add,range(24)))
        self.assertEqual(len(self.store.read()['shortcuts']),24)
    def test_corrupt_state_is_not_overwritten(self):
        self.store.path.write_text('{broken',encoding='utf-8')
        with self.assertRaises(ValueError):self.store.add_profile('no')
        self.assertEqual(self.store.path.read_text(),'{broken')
    def test_catalog_reads_only_metadata_and_keeps_identity(self):
        source=self.root/'source';source.mkdir();database=source/'state_5.sqlite'
        with sqlite3.connect(database) as db:
            db.execute('CREATE TABLE threads(id TEXT,title TEXT,cwd TEXT,updated_at INTEGER)')
            db.execute('INSERT INTO threads VALUES (?,?,?,?)',(self.thread,'Example','/project',42))
        db.close()
        before=database.read_bytes()
        result=list_catalog([dict(id='source',home=str(source),host_id='local',alias='A')])
        item=result['conversations'][0]
        self.assertEqual(item['thread_id'],self.thread);self.assertEqual(item['source_store_id'],'source')
        self.assertEqual(before,database.read_bytes());self.assertFalse(result['original_gui_federation'])
    def test_profile_paths_cannot_select_original_home(self):
        instance=Instances(self.root,self.store,None)
        manipulated=dict(self.one,home=str(self.root/'original'))
        with self.assertRaises(ValueError):instance.paths(manipulated)
    def test_invalid_command_is_structured_and_no_side_effect(self):
        c=ControlCenter(self.root)
        result=c.request({'id':'one','command':'not.a.command'})
        self.assertFalse(result['ok']);self.assertEqual(result['id'],'one')
    def test_removed_account_link_commands_are_unsupported_and_change_nothing(self):
        c=ControlCenter(self.root);before=self.store.read()
        for command,args in (('profile.bind',dict(profile_id=self.one['id'],usage_account_id=str(uuid4()))),
                             ('accounts.list',{})):
            with self.subTest(command=command):
                result=c.request({'id':command,'command':command,'args':args})
                self.assertFalse(result['ok'])
                self.assertEqual(result['error'],dict(code='request_failed',message='지원하지 않는 관리 명령입니다.'))
        self.assertEqual(before,self.store.read())
    def test_legacy_linked_profile_alias_is_renamed_by_the_manager(self):
        legacy=legacy_linked_profile(self.store,'legacy',str(self.root/'imported-home'))
        self.assertNotIn('alias_authority',legacy)
        sources=self.store.read()['sources']
        c=ControlCenter(self.root)
        result=c.dispatch('profile.rename',dict(profile_id=legacy['id'],alias='renamed'))
        self.assertEqual(result['alias'],'renamed')
        saved=self.store.profile(legacy['id'])
        self.assertEqual(saved['alias'],'renamed')
        self.assertEqual(saved['usage_account_id'],legacy['usage_account_id'])
        self.assertEqual(saved['source_home'],legacy['source_home'])
        self.assertEqual(saved['home'],legacy['home'])
        source=next(s for s in self.store.read()['sources'] if s['id']=='manager:'+legacy['id'])
        self.assertEqual(source['alias'],'renamed')
        imported='usage:'+legacy['usage_account_id']
        self.assertEqual([s for s in self.store.read()['sources'] if s['id']==imported],
                         [s for s in sources if s['id']==imported])
    def test_unrefreshed_imported_usage_is_shown_as_stale(self):
        # Never launched with a bound login, so no fingerprint: no background
        # probe refreshes this imported snapshot, and the view must not call it current.
        legacy=legacy_linked_profile(self.store,'legacy',str(self.root/'imported-home'))
        usage=dict(windows=[dict(label='5시간',used_percent=10,remaining_percent=90,resets_at=None)],
                   observed_at='2026-01-01T00:00:00+00:00',freshness='cached',error=None)
        self.store.mutate(lambda data:self.store.profile(legacy['id'],data).update(usage=usage))
        c=ControlCenter(self.root)
        with patch.object(c.remote,'list_hosts',return_value=[]),patch('manager_core.note_forks.refresh'):
            c._remote_reconcile_started=True
            state=c.state()
        shown=next(p for p in state['profiles'] if p['id']==legacy['id'])
        self.assertEqual(shown['usage']['freshness'],'stale')
        self.assertEqual(shown['usage']['windows'],usage['windows'])
        self.assertEqual(shown['status_message'],'기존 연결 계정')
        self.assertFalse(c.usage_refresh.active(legacy['id']))
    def test_backend_never_loads_an_external_account_tool(self):
        import importlib.util
        before=list(sys.path)
        c=ControlCenter(self.root)
        with patch.object(c.remote,'list_hosts',return_value=[]),patch.object(c.usage_refresh,'schedule'),\
             patch('manager_core.note_forks.refresh'):
            c._remote_reconcile_started=True
            state=c.state()
        self.assertEqual(state['notices'],[])
        self.assertEqual(sys.path,before)
        self.assertFalse(any(Path(entry).name=='src' and Path(entry).parent.name in ('llm-usage','llm_usage') for entry in sys.path))
        self.assertNotIn('llm_usage',sys.modules)
        self.assertIsNone(importlib.util.find_spec('manager_core.accounts'))
        self.assertFalse(hasattr(c,'accounts'))
    def test_policy_wrong_model_does_not_change_saved_policy(self):
        c=ControlCenter(self.root);before=self.store.read()
        result=c.request({'id':'x','command':'policy.set','args':{'profile_id':self.one['id'],'enabled':True,'model_ids':[str(uuid4())]}})
        self.assertFalse(result['ok']);self.assertEqual(before,self.store.read())
    def test_policy_save_schedules_only_running_selected_profile_without_waiting_for_restart(self):
        c=ControlCenter(self.root)
        with patch.object(c.providers,'render_for_host'),patch.object(c.instances,'prepare',return_value=dict(state='pending',message='old')):
            with patch.object(c.instances,'observe',return_value={'status':'running'}),patch.object(c.restarts,'schedule',return_value={'phase':'waiting'}) as schedule:
                result=c.request({'id':'save','command':'policy.set','args':dict(profile_id=self.one['id'],enabled=False,model_ids=[])})
                self.assertTrue(result['ok'],result)
                self.assertEqual(result['result']['restart']['phase'],'waiting')
                schedule.assert_called_once_with(self.one['id'])
            with patch.object(c.instances,'observe',return_value={'status':'not_started'}),patch.object(c.restarts,'schedule') as schedule:
                result=c.dispatch('policy.set',dict(profile_id=self.one['id'],enabled=False,model_ids=[]))
                schedule.assert_not_called()
    def test_foreign_open_never_launches_wrong_instance(self):
        item=self.link();self.store.shortcut_move(item['id'],self.two['id'])
        c=ControlCenter(self.root)
        with patch.object(c.instances,'show') as show:
            result=c.request({'id':'x','command':'conversation.open','args':{'shortcut_id':item['id']}})
            self.assertEqual(result['result']['state'],'blocked');show.assert_not_called()
    def test_viewer_projection_resolves_only_registered_canonical_source(self):
        projection=str(uuid4());path=self.root/'work/control-center/catalog/local-records.json'
        path.parent.mkdir(parents=True)
        entry=dict(projectionThreadId=projection,threadId=self.thread,hostId='local',
                   sourceStoreId='manager:'+self.one['id'],codexHome=self.one['home'],rolloutPath='unused')
        manifest=dict(version=1,hostId='local',entries=[entry]);path.write_text(json.dumps(manifest))
        self.store.mutate(lambda data:self.store.profile(self.two['id'],data).update(
            view_only=True,representative_profile_id=self.one['id'],record_catalog_path=str(path)))
        c=ControlCenter(self.root)
        args=dict(viewer_profile_id=self.two['id'],projection_thread_id=projection)
        with patch.object(c.instances,'show') as show:
            resolved=c.dispatch('catalog.resolve',args)
            self.assertEqual(resolved['thread_id'],self.thread)
            self.assertEqual(resolved['source_store_id'],'manager:'+self.one['id'])
            show.assert_not_called()
            manifest['entries'].append(dict(entry));path.write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):c.dispatch('catalog.resolve',args)
            manifest['entries']=[dict(entry,codexHome=str(self.root/'wrong-source'))]
            path.write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):c.dispatch('catalog.resolve',args)
    def test_usage_refresh_command_reads_only_windows_login_accounts(self):
        c=ControlCenter(self.root)
        native=dict(accounts=2,refreshed=1,results=[])
        with patch.object(c.native_login,'refresh_all',return_value=native) as refresh:
            result=c.dispatch('accounts.refresh',{})
        refresh.assert_called_once_with()
        self.assertEqual(result,{**native,'message':'Windows 로그인 계정 2개 중 1개의 최신 사용량을 확인했습니다.'})
        before=self.store.read()
        result=c.dispatch('accounts.refresh',{})
        self.assertEqual((result['accounts'],result['refreshed']),(0,0))
        self.assertEqual(result['message'],'새로 확인할 Windows 로그인 계정이 없습니다.')
        self.assertEqual(before,self.store.read())
    def test_record_viewer_of_native_parent_uses_the_parents_own_home(self):
        stale=str(self.root/'imported-home')
        for profile,auth_mode in ((self.one,'native'),(self.two,None)):
            def link(data,profile=profile,auth_mode=auth_mode):
                item=self.store.profile(profile['id'],data)
                item.update(source_home=stale,usage_account_id=str(uuid4()))
                if auth_mode:item['auth_mode']=auth_mode
            self.store.mutate(link)
        c=ControlCenter(self.root)
        catalog=Mock();catalog.ensure.return_value=dict(path=str(self.root/'catalog.json'),entries=0)
        with patch('control_center.runtime_build',return_value={'capabilities':{'native_record_catalog':True}}),\
             patch.object(c,'current_catalog_refresh',return_value=catalog),\
             patch.object(c.instances,'show',return_value={'state':'fixture'}):
            for profile,expected in ((self.one,self.one['home']),(self.two,stale)):
                with self.subTest(parent=profile['alias']):
                    result=c.dispatch('catalog.show',dict(profile_id=profile['id']))
                    self.assertTrue(result['readonly_viewer'])
                    viewer=next(p for p in self.store.read()['profiles']
                                if p.get('view_only') and p.get('representative_profile_id')==profile['id'])
                    self.assertEqual(viewer['source_home'],expected)
    def test_existing_record_viewer_drops_the_imported_home_of_a_native_parent(self):
        from uuid import uuid5,UUID
        stale=str(self.root/'imported-home')
        self.store.mutate(lambda data:self.store.profile(self.one['id'],data).update(
            auth_mode='native',source_home=stale,usage_account_id=str(uuid4())))
        vid=str(uuid5(UUID(self.one['id']),'codex-control-center-record-viewer-v1'))
        directory=self.store.directory/'profiles'/vid
        def older_viewer(data):
            data['profiles'].append(dict(id=vid,alias='전체 기록 · 01',usage_account_id=None,
                home=str(directory/'codex'),ui_home=str(directory/'ui'),status='not_started',process_id=None,
                source_home=stale,policy=dict(enabled=False,model_ids=[],desired_revision=0,effective_revision=None),
                view_only=True,representative_profile_id=self.one['id'],record_catalog_path=str(self.root/'catalog.json')))
        self.store.mutate(older_viewer)
        c=ControlCenter(self.root)
        catalog=Mock();catalog.ensure.return_value=dict(path=str(self.root/'catalog.json'),entries=0)
        with patch('control_center.runtime_build',return_value={'capabilities':{'native_record_catalog':True}}),\
             patch.object(c,'current_catalog_refresh',return_value=catalog),\
             patch.object(c.instances,'show',return_value={'state':'fixture'}) as show:
            c.dispatch('catalog.show',dict(profile_id=self.one['id']))
        show.assert_called_once_with(vid)
        viewers=[p for p in self.store.read()['profiles'] if p['id']==vid]
        self.assertEqual(len(viewers),1)
        self.assertEqual(viewers[0]['source_home'],self.one['home'])
        self.assertEqual(viewers[0]['home'],str(directory/'codex'))


if __name__=='__main__':unittest.main()
