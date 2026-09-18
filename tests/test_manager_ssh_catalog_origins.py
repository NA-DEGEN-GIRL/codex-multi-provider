import copy
from pathlib import Path
import sys
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.ssh_runtime_control import SshRuntimeControl
from test_manager_ssh_runtime_control import Auth


class SharedCatalogActivityTests(unittest.TestCase):
    def setUp(self):
        self.frames = []
        self.control = SshRuntimeControl(Auth(), str(uuid4()), str(uuid4()), 1, self.frames.append)
        self.addCleanup(self.control.close)
        self.control.process('frontend', {'id': 1, 'method': 'initialize', 'params': {}})
        self.control.process('runtime', {'id': 1, 'result': {}})
        self.projection = {
            'id': str(uuid4()), 'path': None, 'canAcceptDirectInput': False,
            'status': {'type': 'active'},
            'turns': [{'id': str(uuid4()), 'status': 'inProgress'}],
            'extra': {'managedRecord': {
                'hostId': 'local', 'sourceStoreId': 'manager:' + str(uuid4()),
                'canonicalThreadId': str(uuid4())}}}

    def response(self, method, result):
        self.control.process('frontend', {'id': 2, 'method': method, 'params': {}})
        message = {'id': 2, 'result': result}
        self.assertEqual(self.control.process('runtime', message).frontend, [message])

    def notify(self, method, params):
        message = {'method': method, 'params': params}
        self.assertEqual(self.control.process('runtime', message).frontend, [message])

    def test_listing_opening_and_live_record_events_are_passive_but_native_work_is_active(self):
        self.response('thread/list', {'data': [self.projection], 'nextCursor': None})
        self.response('thread/resume', {'thread': self.projection})
        identity = self.projection['id']
        events = [
            ('thread/status/changed', {'status': {'type': 'active'}}),
            ('turn/started', {'turn': self.projection['turns'][0]}),
            ('item/started', {'item': {'id': str(uuid4()), 'type': 'commandExecution'}}),
            ('item/started', {'item': {'type': 'subAgentActivity', 'kind': 'started',
                                     'agentThreadId': str(uuid4())}}),
            ('thread/queue/changed', {}),
        ]
        for method, params in events:
            self.notify(method, {'threadId': identity, **params})
        snapshot = self.control.observer.snapshot()
        self.assertTrue(snapshot['stream_complete'])
        self.assertTrue(snapshot['no_activity_observed'])
        self.assertEqual(snapshot['queue_state_unknown_count'], 0)
        self.assertEqual(self.frames, [])
        canonical = self.projection['extra']['managedRecord']['canonicalThreadId']
        self.notify('turn/started', {'threadId': canonical,
                                   'turn': {'id': str(uuid4()), 'status': 'inProgress'}})
        self.assertEqual(self.control.observer.snapshot()['active_turn_count'], 1)

    def test_runtime_membership_notification_learns_origin_and_deletion_forgets_it(self):
        self.notify('thread/started', {'thread': self.projection})
        self.assertTrue(self.control.catalog_origins(self.projection['id']))
        self.assertTrue(self.control.observer.snapshot()['no_activity_observed'])
        self.notify('thread/deleted', {'threadId': self.projection['id']})
        self.assertFalse(self.control.catalog_origins(self.projection['id']))
        ordinary = {**self.projection, 'extra': None, 'canAcceptDirectInput': True}
        self.response('thread/read', {'thread': ordinary})
        self.assertEqual(self.control.observer.snapshot()['active_turn_count'], 1)

    def test_malformed_origin_cannot_hide_runtime_activity(self):
        thread = copy.deepcopy(self.projection)
        thread['canAcceptDirectInput'] = True
        self.response('thread/list', {'data': [thread]})
        snapshot = self.control.observer.snapshot()
        self.assertFalse(snapshot['stream_complete'])
        self.assertFalse(snapshot['no_activity_observed'])
        self.assertFalse(self.control.catalog_origins(thread['id']))

    def test_large_catalog_retains_open_record_membership_and_approval_accounting(self):
        first = self.projection['id']
        records = [self.projection]
        for _ in range(5000):
            records.append({**self.projection, 'id': str(uuid4())})
        for offset in range(0, len(records), 200):
            self.response('thread/list', {'data': records[offset:offset + 200]})
        self.notify('turn/started', {'threadId': first, 'turn': self.projection['turns'][0]})
        self.assertTrue(self.control.catalog_origins(first))
        self.assertTrue(self.control.observer.snapshot()['stream_complete'])
        self.assertTrue(self.control.observer.snapshot()['no_activity_observed'])
        self.control.process('runtime', {'id': 42, 'method': 'item/tool/requestUserInput',
                                        'params': {'threadId': first}})
        self.assertEqual(self.control.observer.snapshot()['pending_server_request_count'], 1)
        self.notify('turn/started', {'threadId': str(uuid4()), 'turn': self.projection['turns'][0]})
        self.assertEqual(self.control.observer.snapshot()['active_turn_count'], 1)

    def test_user_and_tool_payloads_cannot_enroll_a_passive_record(self):
        self.control.process('frontend', {'id': 3, 'method': 'turn/start', 'params': {
            'threadId': self.projection['id'], 'input': [self.projection]}})
        self.control.process('runtime', {'id': 3, 'result': {}})
        self.notify('item/completed', {'threadId': self.projection['id'], 'item': {
            'id': str(uuid4()), 'type': 'dynamicToolCall', 'content': self.projection}})
        self.assertFalse(self.control.catalog_origins(self.projection['id']))
        self.notify('turn/started', {'threadId': self.projection['id'],
                                   'turn': self.projection['turns'][0]})
        self.assertEqual(self.control.observer.snapshot()['active_turn_count'], 1)
