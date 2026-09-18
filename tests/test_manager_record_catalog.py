import json
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from manager_core.record_catalog import build


class RecordCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.home=self.root/'source';(self.home/'sessions').mkdir(parents=True)
        self.sources=[dict(id='original:local',home=str(self.home),host_id='local',alias='Source')]
    def tearDown(self):self.temp.cleanup()
    def record(self,thread=None,name='one.jsonl',mode=None):
        thread=thread or str(uuid4());payload=dict(id=thread,cwd='/test')
        if mode:payload['history_mode']=mode
        (self.home/'sessions'/name).write_text(json.dumps({'type':'session_meta','payload':payload})+'\n',encoding='utf-8')
        return thread
    def test_projection_stable_and_canonical_preserved(self):
        tid=self.record();a=build(self.root,self.sources);b=build(self.root,self.sources)
        self.assertEqual(a['revision'],b['revision']);self.assertEqual(a['mapping'][0]['thread_id'],tid)
        self.assertNotEqual(a['mapping'][0]['projection_thread_id'],tid);self.assertTrue(a['complete'])
    def test_duplicate_identity_refused(self):
        tid=self.record();self.record(tid,'two.jsonl')
        result=build(self.root,self.sources);self.assertEqual(result['entries'],0);self.assertFalse(result['complete'])
    def test_modern_not_falsely_included(self):
        self.record(mode='paginated');result=build(self.root,self.sources)
        self.assertEqual(result['entries'],0);self.assertIn('modern_history_unsupported',result['limitations'])
    def test_limit_does_not_claim_complete(self):
        self.record();self.record(name='two.jsonl');result=build(self.root,self.sources,max_entries=1)
        self.assertEqual(result['entries'],1);self.assertFalse(result['complete'])
    def test_source_rollouts_unchanged(self):
        self.record();path=self.home/'sessions/one.jsonl';before=path.read_bytes()
        build(self.root,self.sources);self.assertEqual(path.read_bytes(),before)


if __name__=='__main__':unittest.main()
