import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from manager_core import authority
from manager_core.store import atomic_json
from remote_helpers import managed_sources
from manager_core.remote_handoff import RemoteHandoffManager
from manager_core.handoff import HandoffManager, HandoffError

spec = importlib.util.spec_from_file_location('handoff_store_test', ROOT / 'scripts/remote_helpers/handoff_store.py')
helper = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, authority=authority, managed_sources=managed_sources):
    spec.loader.exec_module(helper)


class RemoteHandoffStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.a, self.b, self.tid, self.child = (str(uuid4()) for _ in range(4))
        for pid in (self.a, self.b):
            home = self.root / 'profiles' / pid / 'codex'
            home.mkdir(parents=True)
            atomic_json(home / 'managed-source.json', dict(host_id='local', store_id='manager:' + pid))
        self.home = self.root / 'profiles' / self.a / 'codex'
        self.grants = [dict(version=1, host_id='local', store_id='manager:' + self.a, thread_id=tid,
                            owner_profile_id=self.a, epoch=1, revision=1) for tid in (self.tid, self.child)]
        for grant in self.grants:
            atomic_json(self.home / 'managed-authority' / (grant['thread_id'] + '.json'), grant)
        self.storage = helper.Storage(self.root)
        self.request = dict(operation='transfer', profile_id=self.a, target_profile_id=self.b,
            transaction_id=str(uuid4()), expected_grants=self.grants,
            close_proof=dict(threadId=self.tid, closedThreadIds=[self.tid, self.child], writerReleaseVerified=True))

    def tearDown(self):
        self.temp.cleanup()

    def test_entire_subtree_cas_is_durable_and_retry_does_not_increment(self):
        result = self.storage.dispatch(self.request)
        self.assertEqual([2, 2], [g['epoch'] for g in result['grants']])
        self.assertTrue(all(g['owner_profile_id'] == self.b for g in result['grants']))
        with patch.object(authority, 'transfer_many', side_effect=AssertionError('must not replay')):
            retry = self.storage.dispatch(self.request)
        self.assertTrue(retry['already_committed'])
        self.assertEqual(result['grants'], retry['grants'])

    def test_missing_child_release_does_not_change_authority(self):
        self.request['close_proof']['closedThreadIds'] = [self.tid]
        with self.assertRaisesRegex(ValueError, 'subtree'):
            self.storage.dispatch(self.request)
        self.assertEqual(self.grants[0], authority.read(self.home, self.tid))
        self.assertFalse((self.root / 'handoffs').exists())

    def test_stale_owner_blocks_whole_batch(self):
        changed = {**self.grants[1], 'epoch': 2, 'revision': 2}
        atomic_json(self.home / 'managed-authority' / (self.child + '.json'), changed)
        with self.assertRaises(RuntimeError): self.storage.dispatch(self.request)
        self.assertEqual(self.grants[0], authority.read(self.home, self.tid))
        journal = helper.read_json(self.root / 'handoffs' / (self.request['transaction_id'] + '.json'))
        self.assertEqual('recovery_required', journal['status'])
        with self.assertRaisesRegex(RuntimeError, 'recovery'): self.storage.dispatch(self.request)

    def test_writer_lock_prevents_owner_change(self):
        with authority._writer_guard(self.home, self.tid):
            with self.assertRaises(RuntimeError): self.storage.dispatch(self.request)
        self.assertEqual(self.grants[0], authority.read(self.home, self.tid))
        self.assertEqual(self.grants[1], authority.read(self.home, self.child))

    def test_partial_commit_is_journaled_without_rollback_or_replay(self):
        original = authority._replace_owner
        def fail_second(home, directory, path, expected, target):
            if expected['thread_id'] == self.child: raise OSError('fixture interrupted disk write')
            return original(home, directory, path, expected, target)
        with patch.object(authority, '_replace_owner', side_effect=fail_second):
            with self.assertRaises(RuntimeError): self.storage.dispatch(self.request)
        journal = helper.read_json(self.root / 'handoffs' / (self.request['transaction_id'] + '.json'))
        self.assertEqual('recovery_required', journal['status'])
        self.assertEqual([self.tid], [g['thread_id'] for g in journal['changed_grants']])
        self.assertEqual(2, authority.read(self.home, self.tid)['epoch'])
        self.assertEqual(1, authority.read(self.home, self.child)['epoch'])
        with self.assertRaisesRegex(RuntimeError, 'recovery'): self.storage.dispatch(self.request)

    def test_manifest_keeps_foreign_owner_then_refreshes_same_source(self):
        request = dict(operation='manifest', profile_id=self.b, required=self.grants)
        before = helper.read_json(Path(self.storage.dispatch(request)['path']))
        self.assertEqual({self.a}, {b['ownerProfileId'] for b in before['bindings']})
        changed = self.storage.dispatch(self.request)['grants']
        request['required'] = changed
        after = helper.read_json(Path(self.storage.dispatch(request)['path']))
        self.assertEqual({self.b}, {b['ownerProfileId'] for b in after['bindings']})
        self.assertEqual({2}, {b['ownershipEpoch'] for b in after['bindings']})
        self.assertEqual(before['sources'], after['sources'])
        self.assertFalse((self.root / 'profiles' / self.b / 'codex' / 'sessions').exists())

    def test_conflicting_request_cannot_reuse_transaction_id(self):
        self.storage.dispatch(self.request)
        self.request['close_proof']['threadId'] = self.child
        with self.assertRaisesRegex(RuntimeError, 'recovery'): self.storage.dispatch(self.request)


class RemoteHandoffIdentityTests(unittest.TestCase):
    def test_durable_identity_distinguishes_linux_daemon_from_windows_transport(self):
        manager=object.__new__(RemoteHandoffManager)
        manager.remote_identities={}
        admin=object();local={'generation':str(uuid4()),'runtime':{'pid':123},'proxy':{'pid':456}}
        linux=dict(process=dict(pid=9001,process_start='81726',boot_id=str(uuid4())),revision='a'*64)
        with patch.object(HandoffManager,'_admin',return_value=(admin,local)),patch.object(manager,'_binding',return_value={'profile_id':'fixture'}),patch.object(manager,'_inspect',return_value=linux):
            actual,record=manager._admin({'id':'fixture'})
            self.assertIs(actual,admin)
            self.assertEqual(record['remote'],linux)
            self.assertEqual(record['runtime']['pid'],123)
            self.assertNotIn('remote',local)
            with patch.object(HandoffManager,'_same_identity') as check:
                manager._same_identity(admin,record)
                check.assert_called_once_with(admin,local)
            with patch.object(HandoffManager,'_same_identity'),patch.object(manager,'_inspect',return_value={**linux,'process':{**linux['process'],'pid':9002}}):
                with self.assertRaises(HandoffError):manager._same_identity(admin,record)


if __name__ == '__main__':
    unittest.main()
