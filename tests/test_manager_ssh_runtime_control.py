import copy
import json
import unittest
from uuid import uuid4

from manager_core.notification_policy import NotificationPolicy
from manager_core.proxy_auth import AuthProxyResult
from manager_core.runtime_admin import AdminError
from manager_core.ssh_runtime_control import SshRuntimeControl, endpoint_id
from test_manager_websocket_auth import decode_frames


class Auth:
    state = 'ready'

    def process(self, direction, message):
        result = AuthProxyResult()
        getattr(result, 'runtime' if direction == 'frontend' else 'frontend').append(message)
        return result

    def poll(self):
        return AuthProxyResult()


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.profile_id, self.generation, self.thread_id = (str(uuid4()) for _ in range(3))
        self.sent = []
        self.control = SshRuntimeControl(Auth(), self.profile_id, self.generation, 1, self.sent.append)
        self.addCleanup(self.control.close)
        self.control.process('frontend', {'id': 1, 'method': 'initialize', 'params': {}})
        self.control.process('runtime', {'id': 1, 'result': {}})
        self.control.process('frontend', {'method': 'initialized'})

    def test_transport_freeze_rejects_new_work_and_release_restores_it(self):
        tx = str(uuid4())
        status = self.control.dispatch('manager/maintenance/acquire', {'transactionId': tx}, 1, None)
        self.assertTrue(status['held'])
        request = {'id': 2, 'method': 'turn/start', 'params': {'threadId': self.thread_id, 'input': []}}
        self.assertEqual(self.control.process('frontend', request).frontend[0]['error']['code'], -32044)
        self.assertEqual(self.control.maintenance.status(self.control.observer.snapshot(), True)['pendingMutationCount'], 0)
        self.control.dispatch('manager/maintenance/release', {'transactionId': tx, 'leaseToken': status['leaseToken']}, 1, None)
        self.assertEqual(self.control.process('frontend', request).runtime, [request])

    def test_private_rpc_uses_existing_masked_stream_and_response_is_not_delivered_to_gui(self):
        responses = []
        def send(frame):
            decoded = decode_frames([frame])[0]
            self.assertTrue(decoded['masked'])
            request = json.loads(decoded['payload'])
            responses.append(self.control.process('runtime', {'id': request['id'],
                'result': {'data': [self.thread_id], 'nextCursor': None}}))
        self.control.send_frame = send
        result = self.control.dispatch('thread/loaded/list', {}, 1, None)
        self.assertEqual(result['data'], [self.thread_id])
        self.assertEqual(responses[0].frontend, [])
        self.assertEqual(responses[0].runtime, [])

    def test_unknown_or_closed_stream_cannot_authorize_admin_work(self):
        self.control.observer.gap()
        with self.assertRaises(AdminError):
            self.control.dispatch('thread/loaded/list', {}, 1, None)
        self.assertEqual(self.sent, [])

    def test_admin_identity_isolated_by_both_profile_and_host(self):
        first = endpoint_id(self.profile_id, 'remote-dev')
        self.assertNotEqual(first, endpoint_id(self.profile_id, 'remote-c'))
        self.assertNotEqual(first, endpoint_id(str(uuid4()), 'remote-dev'))
        with self.assertRaises(ValueError):
            endpoint_id(self.profile_id, '../remote-dev')


class NotificationTests(unittest.TestCase):
    def test_native_opt_out_events_still_update_observer_without_reaching_frontend(self):
        control = SshRuntimeControl(Auth(), str(uuid4()), str(uuid4()), 1, lambda frame: None)
        self.addCleanup(control.close)
        original = {'id': 1, 'method': 'initialize', 'params': {'capabilities': {
            'optOutNotificationMethods': ['process/exited', 'thread/closed'], 'experimentalApi': False}}}
        saved = copy.deepcopy(original)
        forwarded = control.process('frontend', original).runtime[0]
        self.assertEqual(original, saved)
        self.assertEqual(forwarded['params']['capabilities']['optOutNotificationMethods'], [])
        self.assertTrue(forwarded['params']['capabilities']['experimentalApi'])
        control.process('runtime', {'id': 1, 'result': {}})
        control.process('frontend', {'method': 'initialized'})
        self.assertTrue(control.observer.snapshot()['stream_complete'])
        control.process('frontend', {'id': 2, 'method': 'process/spawn', 'params': {'processHandle': 'handle'}})
        self.assertEqual(control.observer.snapshot()['active_process_count'], 1)
        outcome = control.process('runtime', {'method': 'process/exited', 'params': {'processHandle': 'handle'}})
        self.assertEqual(outcome.frontend, [])
        self.assertEqual(control.observer.snapshot()['active_process_count'], 0)
        approval = {'id': 3, 'method': 'process/exited', 'params': {}}
        self.assertEqual(control.process('runtime', approval).frontend, [approval])

    def test_invalid_preferences_and_duplicate_initialize_do_not_silently_replace_subscription(self):
        for value in (False, 0, '', {}, ['x'] * 257, [None]):
            with self.subTest(value=str(value)[:12]), self.assertRaises(ValueError):
                NotificationPolicy().to_runtime({'method': 'initialize', 'params': {
                    'capabilities': {'optOutNotificationMethods': value}}})
        policy = NotificationPolicy()
        policy.to_runtime({'method': 'initialize', 'params': {'capabilities': {'optOutNotificationMethods': ['thread/closed']}}})
        duplicate = {'method': 'initialize', 'params': {}}
        self.assertEqual(policy.to_runtime(duplicate), duplicate)
        self.assertFalse(policy.to_frontend({'method': 'thread/closed'}))
