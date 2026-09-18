import json
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.app_transport import RuntimeObserver
from manager_core.catalog_projection import CatalogProjectionIds


class ProjectionAccountingTests(unittest.TestCase):
    def test_membership_storage_failure_does_not_hide_work_or_break_rpc_observation(self):
        predicate = CatalogProjectionIds(None)
        self.addCleanup(predicate.close)
        observer = RuntimeObserver(str(uuid4()), read_only_projection=predicate)
        thread = dict(id=str(uuid4()), path=None, canAcceptDirectInput=False,
            status={'type': 'active'}, turns=[{'id': 'turn', 'status': 'inProgress'}],
            extra={'managedRecord': dict(hostId='local', sourceStoreId='manager:' + str(uuid4()),
                                        canonicalThreadId=str(uuid4()))})
        observer.consume('server', {'method': 'thread/started', 'params': {'thread': thread}})
        predicate.origins._db.close()  # Deterministic storage failure; no live state involved.
        observer.consume('server', {'method': 'thread/started', 'params': {'thread': thread}})
        self.assertFalse(predicate(thread['id']))
        self.assertFalse(observer.snapshot()['stream_complete'])
        self.assertEqual(observer.snapshot()['active_turn_count'], 1)

    def test_windows_v3_observer_learns_only_native_metadata_and_releases_membership(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'catalog.json'
            path.write_text(json.dumps(dict(version=3, hostId='local', sources=[], legacySources=[])))
            predicate = CatalogProjectionIds(path)
            self.addCleanup(predicate.close)
            observer = RuntimeObserver(str(uuid4()), read_only_projection=predicate)
            identity, native = str(uuid4()), str(uuid4())
            thread = dict(id=identity, path=None, canAcceptDirectInput=False,
                status={'type': 'active'}, turns=[{'id': 'turn', 'status': 'inProgress'}],
                extra={'managedRecord': dict(hostId='local', sourceStoreId='manager:' + str(uuid4()),
                                             canonicalThreadId=native)})
            observer.consume('server', {'method': 'thread/started', 'params': {'thread': thread}})
            self.assertTrue(predicate(identity))
            self.assertFalse(predicate(native))
            self.assertEqual(observer.snapshot()['active_turn_count'], 0)
            observer.consume('server', {'method': 'turn/started', 'params': {
                'threadId': native, 'turn': thread['turns'][0]}})
            self.assertEqual(observer.snapshot()['active_turn_count'], 1)
            observer.consume('server', {'method': 'thread/deleted', 'params': {'threadId': identity}})
            self.assertFalse(predicate(identity))
            predicate.close()
            self.assertIsNone(predicate.origins._db)

    def test_foreign_progress_does_not_count_as_local_work_but_real_children_still_do(self):
        projection, native, child = (str(uuid4()) for _ in range(3))
        observer = RuntimeObserver(str(uuid4()), read_only_projection=lambda identity: identity == projection)
        for identity in (projection, native):
            observer.consume('client', {'id': identity, 'method': 'thread/resume', 'params': {'threadId': identity}})
            observer.consume('server', {'id': identity, 'result': {'thread': {'id': identity,
                'canAcceptDirectInput': False, 'status': {'type': 'active'},
                'turns': [{'id': 'turn', 'status': 'inProgress'}]}}})
            observer.consume('server', {'method': 'item/started', 'params': {'threadId': identity,
                'item': {'id': 'spawn', 'type': 'collabAgentToolCall', 'agentsStates': {child: {'status': 'running'}}}}})
            state = observer.snapshot()
            if identity == projection:
                self.assertEqual((state['active_turn_count'], state['active_tool_count'], state['active_child_count']), (0, 0, 0))
            else:
                self.assertEqual((state['active_turn_count'], state['active_tool_count'], state['active_child_count']), (1, 1, 1))
                self.assertEqual(state['active_thread_ids'], [native])
            self.assertFalse(state['safe_to_restart'])

    def test_projected_history_does_not_suppress_requests_for_user_approval(self):
        projection = str(uuid4())
        observer = RuntimeObserver(str(uuid4()), read_only_projection=lambda identity: identity == projection)
        observer.consume('server', {'id': 8, 'method': 'item/tool/requestUserInput', 'params': {'threadId': projection}})
        self.assertEqual(observer.snapshot()['pending_server_request_count'], 1)

    def test_manifest_membership_refresh_and_failure_never_hide_unverified_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'catalog.json'
            projection, second, native = (str(uuid4()) for _ in range(3))
            manifest = {'version': 1, 'hostId': 'local', 'entries': [{'projectionThreadId': projection, 'threadId': native}]}
            path.write_text(json.dumps(manifest), encoding='utf-8')
            predicate = CatalogProjectionIds(path)
            self.assertTrue(predicate(projection)); self.assertFalse(predicate(native))
            manifest['entries'].append({'projectionThreadId': second, 'threadId': str(uuid4())})
            path.write_text(json.dumps(manifest), encoding='utf-8')
            self.assertTrue(predicate(second))
            path.write_text('{broken', encoding='utf-8')
            self.assertFalse(predicate(projection))
            manifest['entries'][0]['projectionThreadId'] = native
            path.write_text(json.dumps(manifest), encoding='utf-8')
            self.assertFalse(predicate(native))
