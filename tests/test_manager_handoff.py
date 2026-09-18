import json
from pathlib import Path
import sys
import tempfile
import unittest
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core import authority
from manager_core.handoff import HandoffManager
from manager_core.runtime_admin import AdminError
from manager_core.managed_sources import mark, manifest
from manager_core.store import Store, atomic_json


class FakeAdmin:
    def __init__(self, fixture, profile, pid):
        self.fixture, self.profile = fixture, profile
        self.identity = dict(generation=profile['generation'],
                             runtime=dict(process_id=pid, process_created=pid * 100, executable_path=sys.executable),
                             proxy=dict(process_id=pid + 100, process_created=pid * 100 + 1, executable_path=sys.executable))
        self.loaded = set()
        self.calls = []
        self.ready = True
        self.held = False
        self.idle = True
        self.idle_scope = None
        self.close_scope = None
        self.activation_scope = None
        self.close_error = None
        self.activation_error = None
        self.on_close = None
        self.read_fail = False
        self.parent_missing = False

    def identities(self):
        return deepcopy(self.identity)

    def request(self, method, params, timeout=15):
        self.calls.append((method, deepcopy(params)))
        f = self.fixture
        if method == 'manager/maintenance/status':
            # Independent busy work is intentionally represented here.
            return dict(generation=self.profile['generation'], connected=True, initialized=True,
                        streamComplete=True, accountReady=self.ready, held=self.held,
                        pendingMutationCount=3, pendingApprovalCount=1)
        if method == 'thread/loaded/list':
            return dict(data=sorted(self.loaded), nextCursor=None)
        if method == 'thread/read':
            tid = params['threadId']
            if self.read_fail:
                raise RuntimeError('fixture read failed')
            data = dict(id=tid, status={'type': 'idle' if tid in self.loaded else 'notLoaded'})
            if not self.parent_missing:
                data['parentThreadId'] = f.parents.get(tid)
            return dict(thread=data)
        if method == 'thread/managedIdleStatus':
            return dict(threadId=params['threadId'], hostId='local', sourceStoreId=f.ref['source_store_id'],
                        observedThreadIds=self.idle_scope or f.scope, idle=self.idle,
                        blockers=[] if self.idle else [dict(threadId=f.tid, kind='queuedUserInput')],
                        proofScope='advisory')
        if method == 'thread/managedCloseIdle':
            if self.close_error:
                raise self.close_error
            closed = self.close_scope or f.scope
            self.loaded.difference_update(closed)
            if self.on_close:
                self.on_close()
            return dict(threadId=params['threadId'], closedThreadIds=closed, writerReleaseVerified=True)
        if method == 'thread/managedReloadBinding':
            if self.activation_error:
                raise self.activation_error
            value = json.loads((f.store.directory / 'profiles' / self.profile['id'] / 'managed-sources.json').read_text(encoding='utf-8'))
            declared = {b['threadId']: b for b in value['bindings']}
            for tid in f.scope:
                grant = authority.read(f.home, tid)
                assert declared[tid]['ownerProfileId'] == self.profile['id'] == grant['owner_profile_id']
                assert declared[tid]['ownershipEpoch'] == grant['epoch']
            return {**params, 'bindingReloaded': True, 'activatedThreadIds': self.activation_scope or f.scope}
        raise AssertionError('Unexpected RPC: ' + method)


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root)
        self.a = self.store.add_profile('source A')
        self.b = self.store.add_profile('target B')
        self.c = self.store.add_profile('peer C')
        for profile in (self.a, self.b, self.c):
            profile['generation'] = str(uuid4())
            Path(profile['home']).mkdir(parents=True)
            mark(profile)
            self.store.mutate(lambda data, p=profile: self.store.profile(p['id'], data).update(generation=p['generation']))
        self.tid, self.child, self.peer = (str(uuid4()) for _ in range(3))
        self.scope = [self.tid, self.child]
        self.parents = {self.tid: None, self.child: self.tid, self.peer: None}
        self.home = Path(self.a['home'])
        (self.home / 'managed-authority').mkdir()
        self.records = {}
        for tid in self.scope:
            record = dict(version=1, host_id='local', store_id='manager:' + self.a['id'],
                          thread_id=tid, owner_profile_id=self.a['id'], epoch=1, revision=1)
            atomic_json(self.home / 'managed-authority' / (tid + '.json'), record)
            self.records[tid] = record
        self.ref = dict(thread_id=self.tid, source_store_id='manager:' + self.a['id'], host_id='local')
        for profile in (self.a, self.b, self.c):
            manifest(self.store, profile)
        self.admins = {p['id']: FakeAdmin(self, p, index + 10) for index, p in enumerate((self.a, self.b, self.c))}
        self.source = self.admins[self.a['id']]
        self.target = self.admins[self.b['id']]
        self.source.loaded = {self.tid, self.child, self.peer}
        self.target.loaded = {str(uuid4())}
        self.target_peer = set(self.target.loaded)
        self.manager = HandoffManager(self.root, self.store, admin_factory=lambda profile: self.admins[profile['id']])

    def tearDown(self):
        self.temp.cleanup()

    def cold_advisory(self, blockers):
        original=self.source.request
        def request(method,params,timeout=15):
            result=original(method,params,timeout)
            if method=='thread/managedIdleStatus':result.update(idle=False,blockers=blockers)
            return result
        return patch.object(self.source,'request',side_effect=request)

    def test_cold_child_is_sent_to_strict_writer_claim_without_being_certified_idle(self):
        with self.cold_advisory([dict(threadId=self.child,kind='coldDescendantRequiresWriterClaim')]):
            preview=self.manager.preview(self.ref,self.b['id'])
            self.assertEqual(preview['status'],'ready')
            self.assertFalse(preview['writer_release_verified'])
            self.assertEqual(preview['strict_writer_claim_thread_ids'],[self.child])
            self.assertEqual(authority.read(self.home,self.tid),self.records[self.tid])
            result=self.manager.continue_conversation(self.ref,self.b['id'])
            self.assertTrue(result['writer_release_verified'],result)
            self.assertTrue(any(method=='thread/managedCloseIdle' for method,_ in self.source.calls))

    def test_cold_child_does_not_bypass_other_blockers_or_strict_close_failure(self):
        for blockers in ([dict(threadId=self.tid,kind='coldDescendantRequiresWriterClaim')],
                         [dict(threadId=self.child,kind='coldDescendantRequiresWriterClaim'),dict(threadId=self.tid,kind='queuedUserInput')],
                         [dict(threadId=self.child,kind='unrecognized')]):
            with self.cold_advisory(blockers):
                self.assertEqual(self.manager.preview(self.ref,self.b['id'])['code'],'source_busy')
        self.source.close_error=RuntimeError('canonical writer is held by another process')
        with self.cold_advisory([dict(threadId=self.child,kind='coldDescendantRequiresWriterClaim')]):
            result=self.manager.continue_conversation(self.ref,self.b['id'])
        self.assertEqual(result['status'],'recovery_required')
        self.assertEqual(authority.read(self.home,self.tid),self.records[self.tid])
        self.assertEqual(authority.read(self.home,self.child),self.records[self.child])

    def test_durable_read_status_does_not_override_live_runtime_inventory(self):
        original = self.source.request
        def durable_read(method, params, timeout=15):
            result = original(method, params, timeout)
            if method == 'thread/read':
                result['thread']['status'] = {'type': 'notLoaded'}
            return result
        self.source.request = durable_read
        self.assertEqual(self.manager.preview(self.ref, self.b['id'])['status'], 'ready')
        self.source.loaded.remove(self.tid)
        self.assertEqual(self.manager.preview(self.ref, self.b['id'])['code'], 'source_not_loaded')

    def count(self, admin, method):
        return sum(command == method for command, _ in admin.calls)

    def journal(self):
        paths = list((self.store.directory / 'handoffs').glob('*.json'))
        self.assertEqual(len(paths), 1)
        return json.loads(paths[0].read_text(encoding='utf-8'))

    def test_preview_is_advisory_and_does_not_change_manifest_or_authority(self):
        target_manifest = self.store.directory / 'profiles' / self.b['id'] / 'managed-sources.json'
        before = target_manifest.read_bytes()
        result = self.manager.preview(self.ref, self.b['id'])
        self.assertEqual(result['status'], 'ready')
        self.assertFalse(result['writer_release_verified'])
        self.assertTrue(result['requires_handoff'])
        self.assertEqual(target_manifest.read_bytes(), before)
        self.assertEqual(authority.read(self.home, self.tid), self.records[self.tid])
        self.assertEqual(self.count(self.source, 'thread/managedCloseIdle'), 0)
        self.assertFalse((self.store.directory / 'handoffs').exists())

    def test_full_transfer_keeps_canonical_records_and_independent_work(self):
        result = self.manager.continue_conversation(self.ref, self.b['id'])
        self.assertEqual(result['status'], 'ready', result)
        self.assertTrue(result['binding_reloaded'])
        self.assertTrue(result['writer_release_verified'])
        self.assertEqual(self.source.loaded, {self.peer})
        self.assertEqual(self.target.loaded, self.target_peer)
        self.assertEqual(self.journal()['status'], 'complete')
        for tid in self.scope:
            self.assertEqual(authority.read(self.home, tid)['owner_profile_id'], self.b['id'])
            self.assertEqual(authority.read(self.home, tid)['epoch'], 2)
        self.assertFalse((Path(self.b['home']) / 'managed-authority' / (self.tid + '.json')).exists())
        methods = [m for admin in self.admins.values() for m, _ in admin.calls]
        self.assertFalse(any(m in methods for m in ('turn/start', 'thread/resume', 'thread/fork', 'process/kill')))

    def test_child_link_closes_owning_root_and_complete_spawn_tree(self):
        result = self.manager.continue_conversation({**self.ref, 'thread_id': self.child}, self.b['id'])
        self.assertEqual(result['status'], 'ready', result)
        request = next(p for m, p in self.source.calls if m == 'thread/managedCloseIdle')
        self.assertEqual(request, {'threadId': self.tid})

    def test_return_handoff_uses_same_source_and_advances_each_grant(self):
        first = self.manager.continue_conversation(self.ref, self.b['id'])
        self.assertEqual(first['status'], 'ready', first)
        # The caller subsequently opens/resumes B. The orchestrator itself did not do so.
        self.target.loaded.update(self.scope)
        second = self.manager.continue_conversation(self.ref, self.a['id'])
        self.assertEqual(second['status'], 'ready', second)
        self.assertEqual(second['source'], self.ref)
        self.assertEqual(authority.read(self.home, self.child)['epoch'], 3)
        self.assertEqual(authority.read(self.home, self.tid)['owner_profile_id'], self.a['id'])
        self.assertIn(self.peer, self.source.loaded)

    def test_already_owner_does_not_close_or_activate(self):
        result = self.manager.continue_conversation(self.ref, self.a['id'])
        self.assertEqual(result['status'], 'ready')
        self.assertFalse(result['requires_handoff'])
        self.assertFalse(result['writer_release_verified'])
        self.assertEqual(self.count(self.source, 'thread/managedCloseIdle'), 0)

    def test_final_compact_runtime_identity_is_accepted(self):
        for admin in self.admins.values():
            for kind in ('runtime', 'proxy'):
                item = admin.identity[kind]
                item['pid'] = item.pop('process_id')
                item['created'] = item.pop('process_created')
        result = self.manager.continue_conversation(self.ref, self.b['id'])
        self.assertEqual(result['status'], 'ready', result)

    def test_deleted_old_account_does_not_invalidate_canonical_store(self):
        first = self.manager.continue_conversation(self.ref, self.b['id'])
        self.assertEqual(first['status'], 'ready', first)
        self.store.mutate(lambda data: self.store.profile(self.a['id'], data).update(account_missing=True))
        already = self.manager.continue_conversation(self.ref, self.b['id'])
        self.assertEqual(already['status'], 'ready', already)
        self.target.loaded.update(self.scope)
        next_owner = self.manager.continue_conversation(self.ref, self.c['id'])
        self.assertEqual(next_owner['status'], 'ready', next_owner)
        self.assertEqual(next_owner['source'], self.ref)
        self.assertEqual(authority.read(self.home, self.child)['owner_profile_id'], self.c['id'])

    def test_missing_current_owner_account_still_blocks_transfer(self):
        self.store.mutate(lambda data: self.store.profile(self.a['id'], data).update(account_missing=True))
        result = self.manager.continue_conversation(self.ref, self.b['id'])
        self.assertEqual(result['code'], 'profile_unavailable')
        self.assertEqual(self.count(self.source, 'thread/managedCloseIdle'), 0)

    def test_original_usage_and_ssh_sources_are_read_only_here(self):
        for sid, host in [('original:local', 'local'), ('usage:' + self.a['id'], 'local'), (self.ref['source_store_id'], 'ssh:server')]:
            result = self.manager.continue_conversation({**self.ref, 'source_store_id': sid, 'host_id': host}, self.b['id'])
            self.assertEqual(result['status'], 'blocked')
        self.assertEqual(self.source.calls, [])

    def test_cold_source_absence_never_becomes_release_proof(self):
        self.source.loaded.remove(self.tid)
        result = self.manager.continue_conversation(self.ref, self.b['id'])
        self.assertEqual(result['code'], 'source_not_loaded')
        self.assertEqual(self.count(self.source, 'thread/managedCloseIdle'), 0)
        self.assertEqual(authority.read(self.home, self.tid)['epoch'], 1)

    def test_never_started_owner_is_not_a_proof(self):
        self.store.mutate(lambda data: self.store.profile(self.a['id'], data).pop('generation'))
        result = self.manager.continue_conversation(self.ref, self.b['id'])
        self.assertEqual(result['code'], 'runtime_not_started')
        self.assertEqual(authority.read(self.home, self.tid)['epoch'], 1)

    def test_target_must_be_ready_before_source_close(self):
        self.target.ready = False
        result = self.manager.continue_conversation(self.ref, self.b['id'])
        self.assertEqual(result['code'], 'runtime_not_ready')
        self.assertEqual(self.count(self.source, 'thread/managedCloseIdle'), 0)

    def test_target_maintenance_blocks_handoff_without_affecting_work(self):
        self.target.held = True
        result = self.manager.continue_conversation(self.ref, self.b['id'])
        self.assertEqual(result['code'], 'runtime_not_ready')
        self.assertEqual(self.source.loaded, {self.tid, self.child, self.peer})

    def test_busy_subtree_is_not_closed(self):
        self.source.idle = False
        result = self.manager.continue_conversation(self.ref, self.b['id'])
        self.assertEqual(result['code'], 'source_busy')
        self.assertEqual(self.count(self.source, 'thread/managedCloseIdle'), 0)

    def test_missing_parentage_is_unknown_not_inferred_from_session(self):
        self.source.parent_missing = True
        result = self.manager.continue_conversation(self.ref, self.b['id'])
        self.assertEqual(result['code'], 'parentage_unknown')

    def test_target_conflicting_loaded_actor_blocks(self):
        self.target.loaded.add(self.child)
        result = self.manager.continue_conversation(self.ref, self.b['id'])
        self.assertEqual(result['code'], 'target_loaded')
        self.assertEqual(self.count(self.source, 'thread/managedCloseIdle'), 0)

    def test_failed_target_read_does_not_close_source_and_can_be_retried(self):
        self.target.read_fail = True
        result = self.manager.continue_conversation(self.ref, self.b['id'])
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(self.journal()['status'], 'blocked_before_close')
        self.assertEqual(self.count(self.source, 'thread/managedCloseIdle'), 0)
        self.assertEqual(self.manager.recovery_status(self.ref), [])

    def test_uncertain_close_blocks_replay_even_in_new_manager(self):
        self.source.close_error = RuntimeError('fixture timed out after sending close')
        result = self.manager.continue_conversation(self.ref, self.b['id'])
        self.assertEqual(result['status'], 'recovery_required')
        journal = self.journal()
        self.assertIsNone(journal['close_proof'])
        recovered = HandoffManager(self.root, self.store, admin_factory=lambda p: self.admins[p['id']])
        again = recovered.continue_conversation({**self.ref, 'thread_id': self.child}, self.b['id'])
        self.assertEqual(again['status'], 'recovery_required')
        self.assertEqual(self.count(self.source, 'thread/managedCloseIdle'), 1)
        self.assertEqual(authority.read(self.home, self.tid)['epoch'], 1)

    def test_proven_unsent_close_is_retryable(self):
        self.source.close_error = AdminError('busy', uncertain=False)
        result = self.manager.continue_conversation(self.ref, self.b['id'])
        self.assertEqual(result['status'], 'blocked', result)
        self.assertEqual(self.journal()['status'], 'blocked_before_close')
        self.assertEqual(self.source.loaded, {self.tid, self.child, self.peer})
        self.assertEqual(self.manager.recovery_status(self.ref), [])
        self.source.close_error = None
        recovered = HandoffManager(self.root, self.store, admin_factory=lambda p: self.admins[p['id']])
        self.assertEqual(recovered.continue_conversation(self.ref, self.b['id'])['status'], 'ready')

    def test_admin_close_error_after_dispatch_still_blocks_retry(self):
        self.source.close_error = AdminError('rpc_error', uncertain=True)
        result = self.manager.continue_conversation(self.ref, self.b['id'])
        self.assertEqual(result['status'], 'recovery_required', result)
        self.assertEqual(self.journal()['status'], 'recovery_required')
        self.assertEqual(self.manager.continue_conversation(self.ref, self.b['id'])['status'], 'recovery_required')
        self.assertEqual(self.count(self.source, 'thread/managedCloseIdle'), 1)

    def test_incomplete_subtree_release_blocks_cas(self):
        self.source.close_scope = [self.tid]
        result = self.manager.continue_conversation(self.ref, self.b['id'])
        self.assertEqual(result['status'], 'recovery_required')
        self.assertEqual(result['code'], 'release_unverified')
        self.assertEqual(authority.read(self.home, self.child)['epoch'], 1)

    def test_target_runtime_replacement_after_close_blocks_cas(self):
        self.source.on_close = lambda: self.target.identity['runtime'].update(process_created=99999)
        result = self.manager.continue_conversation(self.ref, self.b['id'])
        self.assertEqual(result['status'], 'recovery_required')
        self.assertEqual(result['code'], 'runtime_changed')
        self.assertEqual(authority.read(self.home, self.tid)['epoch'], 1)

    def test_partial_cas_journals_child_grant_and_never_activates(self):
        real_replace = authority.os.replace
        root_path = self.home / 'managed-authority' / (self.tid + '.json')
        def fail_root(source, target):
            if Path(target) == root_path:
                raise OSError('fixture disk full')
            return real_replace(source, target)
        with patch.object(authority.os, 'replace', side_effect=fail_root):
            result = self.manager.continue_conversation(self.ref, self.b['id'])
        self.assertEqual(result['status'], 'recovery_required')
        self.assertEqual(authority.read(self.home, self.child)['epoch'], 2)
        self.assertEqual(authority.read(self.home, self.tid)['epoch'], 1)
        self.assertEqual(self.journal()['changed_grants'][0]['thread_id'], self.child)
        self.assertEqual(self.count(self.target, 'thread/managedReloadBinding'), 0)

    def test_reopened_recorder_blocks_cas_despite_prior_proof(self):
        guard = authority._writer_guard(self.home, self.child)
        self.source.on_close = lambda: guard.__enter__()
        try:
            result = self.manager.continue_conversation(self.ref, self.b['id'])
        finally:
            guard.__exit__(None, None, None)
        self.assertEqual(result['status'], 'recovery_required')
        self.assertEqual(authority.read(self.home, self.child)['epoch'], 1)
        self.assertEqual(self.count(self.target, 'thread/managedReloadBinding'), 0)

    def test_activation_timeout_retains_authority_and_blocks_automatic_retry(self):
        self.target.activation_error = RuntimeError('fixture uncertain activation')
        result = self.manager.continue_conversation(self.ref, self.b['id'])
        self.assertEqual(result['status'], 'recovery_required')
        self.assertEqual(authority.read(self.home, self.tid)['owner_profile_id'], self.b['id'])
        self.assertEqual(len(self.journal()['changed_grants']), 2)
        again = self.manager.continue_conversation(self.ref, self.b['id'])
        self.assertEqual(again['status'], 'recovery_required')
        self.assertEqual(self.count(self.target, 'thread/managedReloadBinding'), 1)

    def test_changed_activation_scope_never_claims_ready(self):
        self.target.activation_scope = [self.tid]
        result = self.manager.continue_conversation(self.ref, self.b['id'])
        self.assertEqual(result['status'], 'recovery_required')
        self.assertEqual(result['code'], 'activation_unverified')

    def test_journal_failure_after_close_intent_still_blocks_replay(self):
        real_write = self.manager._write
        def fail_after_intent(path, value):
            if value['status'] not in ('preparing', 'close_requested'):
                raise OSError('fixture storage unavailable')
            real_write(path, value)
        with patch.object(self.manager, '_write', side_effect=fail_after_intent):
            result = self.manager.continue_conversation(self.ref, self.b['id'])
        self.assertEqual(result['status'], 'recovery_required')
        self.assertEqual(self.journal()['status'], 'close_requested')
        self.assertEqual(self.manager.preview(self.ref, self.b['id'])['status'], 'recovery_required')


if __name__ == '__main__':
    unittest.main()
