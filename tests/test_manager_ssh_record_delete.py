import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from uuid import uuid4

from manager_core.source_catalog import projection_id
from manager_core.ssh_record_delete import RecordDelete
from manager_core.catalog_origin import CatalogOrigins
from remote_helpers.delete_record import source_home
from test_manager_ssh_runtime_control import Auth
from manager_core.ssh_runtime_control import SshRuntimeControl


class DeleteTests(unittest.TestCase):
    def test_transport_failure_never_reports_deleted_and_keeps_native_rpc_available(self):
        tid=str(uuid4());sid='legacy:fixture';projection=projection_id(sid,tid)
        def fail(*args):raise RuntimeError('PRIVATE transport output')
        control=SshRuntimeControl(Auth(),str(uuid4()),str(uuid4()),1,lambda frame:None,record_delete=fail)
        self.addCleanup(control.close)
        control.process('frontend',dict(id=1,method='initialize',params={}))
        control.process('runtime',dict(id=1,result={}))
        control.process('frontend',dict(method='initialized'))
        control.catalog_origins.observe_thread(dict(id=projection,extra={'managedRecord':dict(
            hostId='local',sourceStoreId=sid,canonicalThreadId=tid)},canAcceptDirectInput=False,path=None))
        request=dict(id=2,method='thread/delete',params={'threadId':projection})
        self.assertEqual(control.process('frontend',request).runtime,[])
        unrelated=dict(id=3,method='thread/read',params={'threadId':str(uuid4())})
        self.assertEqual(control.process('frontend',unrelated).runtime,[unrelated])
        result=[];deadline=time.monotonic()+3
        while not result and time.monotonic()<deadline:result=control.poll().frontend;time.sleep(.01)
        self.assertEqual(len(result),1);self.assertIn('error',result[0]);self.assertNotIn('PRIVATE',json.dumps(result))

    def test_only_observed_projection_is_routed_and_completion_is_asynchronous(self):
        tid=str(uuid4());sid='legacy:fixture';projection=projection_id(sid,tid)
        origin=dict(hostId='local',sourceStoreId=sid,canonicalThreadId=tid)
        origins=CatalogOrigins();self.addCleanup(origins.close)
        origins.observe_thread(dict(id=projection,extra={'managedRecord':origin},canAcceptDirectInput=False,path=None))
        entered=threading.Event();release=threading.Event();self.addCleanup(release.set)
        def execute(identity,observed):
            self.assertEqual((identity,observed),(projection,origin));entered.set();release.wait(3)
            return {'status':'deleted'}
        delete=RecordDelete(origins,execute);self.addCleanup(delete.close)
        self.assertFalse(delete.submit(dict(id=1,method='thread/delete',params={'threadId':tid})))
        self.assertTrue(delete.submit(dict(id=2,method='thread/delete',params={'threadId':projection})))
        self.assertTrue(entered.wait(1));self.assertEqual(delete.poll().frontend,[])
        release.set();deadline=time.monotonic()+3
        result=[]
        while not result and time.monotonic()<deadline:result=delete.poll().frontend;time.sleep(.01)
        self.assertEqual(result,[{'id':2,'result':{}},{'method':'thread/deleted','params':{'threadId':projection}}])

    def test_server_revalidates_source_and_projection_without_accepting_a_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary);home=base/'source';home.mkdir()
            tid=str(uuid4());sid='legacy:fixture'
            (base/'catalog-mixed-sources.json').write_text(json.dumps(dict(version=3,legacySources=[
                dict(sourceStoreId=sid,hostId='local',codexHome=str(home))])))
            (base/'catalog-sources.json').write_text(json.dumps(dict(version=2,sources=[])))
            request=dict(projection=projection_id(sid,tid),origin=dict(hostId='local',sourceStoreId=sid,canonicalThreadId=tid))
            self.assertEqual(source_home(base,request),home.resolve())
            request['projection']=str(uuid4())
            with self.assertRaises(ValueError):source_home(base,request)
            request['projection']=projection_id(sid,tid)
            (base/'catalog-mixed-sources.json').write_text(json.dumps(dict(version=3,legacySources=[])))
            with self.assertRaises(ValueError):source_home(base,request)
