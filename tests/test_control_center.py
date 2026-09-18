import concurrent.futures
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from manager_core.store import Store
from manager_core.catalog import list_catalog
from manager_core.instances import Instances
from control_center import ControlCenter


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
    def test_delete_undo_preserves_source_files(self):
        marker=self.root/'original.jsonl';marker.write_text('preserve',encoding='utf-8')
        item=self.link();result=self.store.shortcut_delete(item['id'])
        self.assertFalse(result['record_deleted']);self.assertEqual(self.store.read()['shortcuts'],[])
        restored=self.store.shortcut_undo();self.assertEqual(item,restored);self.assertEqual(marker.read_text(),'preserve')
    def test_invalid_source_and_traversal_rejected(self):
        with self.assertRaises(ValueError):self.store.shortcut_add('x',self.one['id'],self.thread,'ssh:other','manager:'+self.one['id'])
        with self.assertRaises(ValueError):self.store.profile('../../escape')
        with self.assertRaises(ValueError):self.store.shortcut_delete('not-a-uuid')
    def test_same_alias_new_account_never_rebinds(self):
        first=str(uuid4());second=str(uuid4())
        a=self.store.add_profile('동일 이름',first,str(self.root/'sourceA'))
        b=self.store.add_profile('동일 이름',second,str(self.root/'sourceB'))
        self.assertNotEqual(a['id'],b['id']);self.assertNotEqual(a['source_home'],b['source_home'])
    def test_duplicate_account_keeps_stable_profile_id(self):
        uid=str(uuid4())
        a=self.store.add_profile('A',uid);b=self.store.add_profile('Renamed',uid)
        self.assertEqual(a['id'],b['id'])

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
        with patch('manager_core.accounts.Accounts.list',return_value=[]):
            c=ControlCenter(self.root)
            result=c.request({'id':'one','command':'not.a.command'})
            self.assertFalse(result['ok']);self.assertEqual(result['id'],'one')
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
    def test_live_usage_command_does_not_discard_fresh_status_via_cached_sync(self):
        c=ControlCenter(self.root)
        with patch.object(c.accounts,'refresh_live',return_value={'refreshed':1}) as refresh,patch.object(c.accounts,'sync') as sync:
            self.assertEqual(c.dispatch('accounts.refresh',{}),{'refreshed':1})
            refresh.assert_called_once_with(c.store);sync.assert_not_called()


if __name__=='__main__':unittest.main()
