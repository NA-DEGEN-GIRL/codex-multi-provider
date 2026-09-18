import json
from pathlib import Path
import sys,tempfile,unittest
from uuid import uuid4
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from manager_core.authority import read,transfer,transfer_many,_authority_guard,_writer_guard,AuthorityBatchIncomplete
from unittest.mock import patch


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.home=Path(self.temp.name)
        self.owner=str(uuid4());self.target=str(uuid4());self.tid=str(uuid4());self.sid='manager:'+str(uuid4())
        (self.home/'managed-source.json').write_text(json.dumps(dict(host_id='local',store_id=self.sid)))
        (self.home/'managed-authority').mkdir()
        self.record=dict(version=1,host_id='local',store_id=self.sid,thread_id=self.tid,owner_profile_id=self.owner,epoch=1,revision=1)
        self.path=self.home/'managed-authority'/f'{self.tid}.json';self.path.write_text(json.dumps(self.record))
    def tearDown(self):self.temp.cleanup()
    def test_close_proof_required(self):
        with self.assertRaises(RuntimeError):transfer(self.home,self.record,self.target,release_verified=False)
        self.assertEqual(read(self.home,self.tid),self.record)
    def test_transfer_and_back_advance_epoch(self):
        b=transfer(self.home,self.record,self.target,release_verified=True)
        a=transfer(self.home,b,self.owner,release_verified=True)
        self.assertEqual(a['epoch'],3);self.assertEqual(a['owner_profile_id'],self.owner)
    def test_stale_compare_and_swap_cannot_steal(self):
        b=transfer(self.home,self.record,self.target,release_verified=True)
        with self.assertRaises(RuntimeError):transfer(self.home,self.record,str(uuid4()),release_verified=True)
        self.assertEqual(read(self.home,self.tid),b)
    def test_existing_lock_never_removed(self):
        lock=self.home/'managed-authority'/f'{self.tid}.lock';lock.mkdir()
        with self.assertRaises(RuntimeError):transfer(self.home,self.record,self.target,release_verified=True)
        self.assertTrue(lock.exists())
    def test_wrong_store_refused(self):
        (self.home/'managed-source.json').write_text(json.dumps(dict(host_id='local',store_id='original:local')))
        with self.assertRaises(ValueError):read(self.home,self.tid)
    def test_admitted_writer_blocks_transfer(self):
        guard=self.path.with_suffix('.guard')
        with _authority_guard(guard,exclusive=False):
            with self.assertRaises(RuntimeError):transfer(self.home,self.record,self.target,release_verified=True)
            self.assertEqual(read(self.home,self.tid),self.record)
            self.assertFalse(self.path.with_suffix('.lock').exists())
        result=transfer(self.home,self.record,self.target,release_verified=True)
        self.assertEqual(result['epoch'],2)
        self.assertTrue(guard.exists())
    def test_shared_writers_can_coexist_but_not_cas(self):
        guard=self.path.with_suffix('.guard')
        with _authority_guard(guard,exclusive=False),_authority_guard(guard,exclusive=False):
            with self.assertRaises(RuntimeError):
                with _authority_guard(guard):self.fail('exclusive CAS admitted')
        with _authority_guard(guard):pass
    def child(self):
        record={**self.record,'thread_id':str(uuid4())}
        (self.path.parent/(record['thread_id']+'.json')).write_text(json.dumps(record))
        return record
    def test_tree_transfer_checks_every_grant_before_writing(self):
        child=self.child();stale={**child,'epoch':2}
        with self.assertRaises(RuntimeError):transfer_many(self.home,[self.record,stale],self.target,release_verified=True)
        self.assertEqual(read(self.home,self.tid),self.record)
        self.assertEqual(read(self.home,child['thread_id']),child)
    def test_tree_writer_blocks_whole_transfer(self):
        child=self.child()
        with _authority_guard(self.path.parent/(child['thread_id']+'.guard'),exclusive=False):
            with self.assertRaises(RuntimeError):transfer_many(self.home,[self.record,child],self.target,release_verified=True)
        self.assertEqual(read(self.home,self.tid),self.record)
        result=transfer_many(self.home,[child,self.record],self.target,release_verified=True)
        self.assertEqual([g['thread_id'] for g in result],[child['thread_id'],self.tid])
        self.assertTrue(all(g['epoch']==2 for g in result))
    def test_partial_publish_reports_committed_records_without_rollback(self):
        child=self.child()
        import manager_core.authority as module
        real_replace=module.os.replace
        def replace(source,target):
            if Path(target)==self.path:raise OSError('fixture disk failure')
            return real_replace(source,target)
        with patch.object(module.os,'replace',side_effect=replace):
            with self.assertRaises(AuthorityBatchIncomplete) as caught:
                transfer_many(self.home,[child,self.record],self.target,release_verified=True)
        self.assertEqual(caught.exception.changed[0]['thread_id'],child['thread_id'])
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(read(self.home,self.tid),self.record)
        self.assertEqual(read(self.home,child['thread_id'])['epoch'],2)
        self.assertFalse(list(self.path.parent.glob('*.lock')))
    def test_reopened_recorder_blocks_cas_even_after_close_proof(self):
        with _writer_guard(self.home,self.tid):
            with self.assertRaises(RuntimeError):transfer(self.home,self.record,self.target,release_verified=True)
        self.assertEqual(read(self.home,self.tid),self.record)
        self.assertEqual(transfer(self.home,self.record,self.target,release_verified=True)['epoch'],2)
    def test_cas_holds_recorder_guard_through_publication(self):
        import manager_core.authority as module
        original=module.os.replace
        def replace(source,target):
            with self.assertRaises(RuntimeError):
                with _writer_guard(self.home,self.tid):self.fail('recorder reopened during CAS')
            return original(source,target)
        with patch.object(module.os,'replace',side_effect=replace):
            transfer(self.home,self.record,self.target,release_verified=True)


if __name__=='__main__':unittest.main()
