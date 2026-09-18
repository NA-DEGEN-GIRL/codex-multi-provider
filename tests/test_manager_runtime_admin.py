import base64
import json
from multiprocessing.connection import Client
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.runtime_admin import (AdminClient, AdminError, AdminRpcBroker, AdminServer,
    MaintenanceBarrier, _decode, _MAX_REQUEST, _VERSION, validate_request, sanitize_result)


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.thread_id = str(uuid4())

    def test_admin_is_strictly_not_general_rpc(self):
        cases = [
            ('account/read', {}), ('turn/start', {'threadId': self.thread_id}),
            ('thread/managedCloseIdle', {'threadId': self.thread_id, 'force': True}),
            ('thread/managedCloseIdle', {'threadId': self.thread_id + '\nturn/start'}),
            ('thread/managedCloseIdle', {'threadId': '../elsewhere'}),
            ('thread/read', {'threadId': self.thread_id, 'includeTurns': True}),
            ('thread/loaded/list', {'limit': True}), ('thread/loaded/list', {'limit': 100000}),
            ('thread/loaded/list', {'cursor': '\nsecret'}),
            ([], {}), ('thread/read', []),
        ]
        for method, params in cases:
            with self.subTest(method=method, params=params):
                with self.assertRaises(AdminError):
                    validate_request(method, params)
        self.assertEqual(validate_request('thread/read', {'threadId': self.thread_id}),
                         {'threadId': self.thread_id, 'includeTurns': False})

    def test_json_rejects_duplicates_nan_and_pickle(self):
        for body in (b'{"method":1,"method":2}', b'{"deadline":NaN}', b'[]',
                     b'\x80\x04pickle-is-not-json', b'{"x":' + b'[' * 2000):
            with self.subTest(body=body[:40]):
                with self.assertRaises(AdminError):
                    _decode(body)

    def test_single_reader_broker_matches_and_suppresses_private_replies(self):
        outgoing = []
        broker = None
        def send(message, deadline, lease):
            outgoing.append(message)
            self.assertFalse(broker.consume_runtime({'id': 'frontend-1', 'result': {}}))
            broker.consume_runtime({'id': message['id'], 'result': {
                'threadId': self.thread_id, 'closedThreadIds': [self.thread_id],
                'writerReleaseVerified': True, 'accessToken': 'NEVER-RETURN'}})
        broker = AdminRpcBroker(send)
        result = broker.request('thread/managedCloseIdle', {'threadId': self.thread_id})
        self.assertEqual(result, {'threadId': self.thread_id,
            'closedThreadIds': [self.thread_id], 'writerReleaseVerified': True})
        self.assertEqual(len(outgoing), 1)
        self.assertTrue(broker.is_reserved_request(outgoing[0]))
        self.assertTrue(broker.consume_runtime({'id': outgoing[0]['id'], 'result': {'secret': 'late'}}))

    def test_timeout_does_not_retry_and_late_release_is_not_a_new_success(self):
        outgoing = []
        broker = AdminRpcBroker(lambda message, deadline, lease: outgoing.append(message))
        with self.assertRaises(AdminError) as caught:
            broker.request('thread/managedCloseIdle', {'threadId': self.thread_id}, timeout=.01)
        self.assertEqual(caught.exception.code, 'timeout')
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(len(outgoing), 1)
        self.assertTrue(broker.consume_runtime({'id': outgoing[0]['id'], 'result': {
            'threadId': self.thread_id, 'closedThreadIds': [self.thread_id],
            'writerReleaseVerified': True}}))
        self.assertFalse(broker._pending)

    def test_error_payload_and_mismatched_release_cannot_escape(self):
        broker = None
        def send_error(message, deadline, lease):
            broker.consume_runtime({'id': message['id'], 'error': {
                'code': -32000, 'message': 'SECRET-TOKEN', 'data': {'apiKey': 'PRIVATE'}}})
        broker = AdminRpcBroker(send_error)
        with self.assertRaises(AdminError) as caught:
            broker.request('thread/managedCloseIdle', {'threadId': self.thread_id})
        self.assertEqual(caught.exception.rpc_code, -32000)
        self.assertTrue(caught.exception.uncertain)
        self.assertNotIn('SECRET', str(caught.exception))
        for result in (
            {'threadId': str(uuid4()), 'closedThreadIds': [self.thread_id], 'writerReleaseVerified': True},
            {'threadId': self.thread_id, 'closedThreadIds': [], 'writerReleaseVerified': True},
            {'threadId': self.thread_id, 'closedThreadIds': [self.thread_id], 'writerReleaseVerified': False},
        ):
            with self.assertRaises(AdminError) as caught:
                sanitize_result('thread/managedCloseIdle', {'threadId': self.thread_id}, result)
            self.assertTrue(caught.exception.uncertain)

    def test_thread_read_removes_turns_previews_paths_and_account_fields(self):
        result = sanitize_result('thread/read', {'threadId': self.thread_id}, {'thread': {
            'id': self.thread_id, 'status': {'type': 'active', 'activeFlags': ['waitingOnApproval']},
            'preview': 'SENSITIVE-PROMPT', 'turns': [{'text': 'SENSITIVE'}],
            'path': 'PRIVATE-PATH', 'account': 'PRIVATE-ACCOUNT', 'apiKey': 'PRIVATE-KEY'}})
        self.assertEqual(result, {'thread': {'id': self.thread_id,
            'status': {'type': 'active', 'activeFlags': ['waitingOnApproval']}}})

    def test_close_unblocks_pending_mutation_as_uncertain(self):
        sent = threading.Event()
        broker = AdminRpcBroker(lambda message, deadline, lease: sent.set())
        outcome = []
        def request():
            try:
                broker.request('thread/managedCloseIdle', {'threadId': self.thread_id})
            except AdminError as error:
                outcome.append(error)
        worker = threading.Thread(target=request)
        worker.start()
        self.assertTrue(sent.wait(1))
        broker.close()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome[0].code, 'closed')
        self.assertTrue(outcome[0].uncertain)

    def test_exact_reload_grant_echo_required(self):
        params = {'threadId': self.thread_id, 'hostId': 'local',
            'sourceStoreId': 'manager:' + str(uuid4()), 'ownerProfileId': str(uuid4()),
            'ownershipEpoch': 3, 'recordRevision': 4}
        self.assertEqual(validate_request('thread/managedReloadBinding', params), params)
        value = {**params, 'activatedThreadIds': [self.thread_id], 'bindingReloaded': True}
        self.assertEqual(sanitize_result('thread/managedReloadBinding', params, value), value)
        with self.assertRaises(AdminError) as caught:
            sanitize_result('thread/managedReloadBinding', params, {**value, 'ownershipEpoch': 2})
        self.assertTrue(caught.exception.uncertain)

    def test_runtime_path_restored_before_shim_metadata_is_stripped(self):
        from manager_core.runtime_proxy import runtime_environment
        result = runtime_environment({'Path': 'shim;original', 'PATH': 'another-shim',
            'CODEX_MANAGER_SSH_ORIGINAL_PATH': 'original', 'CODEX_MANAGER_REAL_RUNTIME': 'real'})
        self.assertEqual(result['PATH'], 'original')
        self.assertNotIn('Path', result)
        self.assertNotIn('CODEX_MANAGER_SSH_ORIGINAL_PATH', result)

    def test_idle_observation_remains_advisory_and_rejects_incomplete_scope(self):
        params = {'threadId': self.thread_id}
        observation = {'threadId': self.thread_id, 'hostId': 'local', 'sourceStoreId': 'manager:' + str(uuid4()),
            'observedThreadIds': [self.thread_id], 'idle': True, 'blockers': [], 'proofScope': 'advisory',
            'writerReleaseVerified': True}
        result = sanitize_result('thread/managedIdleStatus', params, observation)
        self.assertTrue(result['idle'])
        self.assertNotIn('writerReleaseVerified', result)
        cold = sanitize_result('thread/managedIdleStatus', params, {**observation, 'idle': False,
            'blockers': [{'threadId': self.thread_id, 'kind': 'coldDescendantRequiresWriterClaim'}]})
        self.assertFalse(cold['idle'])
        self.assertEqual(cold['proofScope'], 'advisory')
        pending = sanitize_result('thread/managedIdleStatus', params, {**observation, 'idle': False,
            'blockers': [{'threadId': self.thread_id, 'kind': 'internalActorWorkOrUnverifiedCleanup'}]})
        self.assertEqual(pending['blockers'], [{'threadId': self.thread_id,
            'kind': 'internalActorWorkOrUnverifiedCleanup'}])
        for changed in ({'observedThreadIds': []}, {'proofScope': 'release'},
                        {'blockers': [{'threadId': self.thread_id, 'kind': 'activeTurn'}]}):
            with self.assertRaises(AdminError) as caught:
                sanitize_result('thread/managedIdleStatus', params, {**observation, **changed})
            self.assertFalse(caught.exception.uncertain)


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.barrier = MaintenanceBarrier(str(uuid4()))
        self.txid = str(uuid4())
        self.observer = {'initialized': True, 'connected': True, 'stream_complete': True}

    def acquire(self):
        return self.barrier.request('manager/maintenance/acquire', {'transactionId': self.txid}, self.observer, True)

    def test_lease_is_idempotent_for_owner_and_not_reused_after_restart(self):
        lease = self.acquire()
        self.assertEqual(self.acquire()['leaseToken'], lease['leaseToken'])
        with self.assertRaises(AdminError):
            self.barrier.request('manager/maintenance/acquire', {'transactionId': str(uuid4())}, self.observer, True)
        with self.assertRaises(AdminError):
            self.barrier.request('manager/maintenance/release', {'transactionId': self.txid, 'leaseToken': str(uuid4())}, self.observer, True)
        restarted = MaintenanceBarrier(self.barrier.generation)
        self.assertFalse(restarted.status(self.observer, True)['held'])
        released = self.barrier.request('manager/maintenance/release',
            {'transactionId': self.txid, 'leaseToken': lease['leaseToken']}, self.observer, True)
        self.assertFalse(released['held'])
        self.assertNotIn('leaseToken', released)

    def test_new_work_is_blocked_but_existing_approvals_and_interrupts_pass(self):
        self.barrier.observe_client({'id': 1, 'method': 'command/exec', 'params': {}})
        self.barrier.observe_runtime({'id': 'approval', 'method': 'item/commandExecution/requestApproval'})
        lease = self.acquire()
        self.assertEqual(lease['pendingMutationCount'], 1)
        self.assertEqual(lease['pendingApprovalCount'], 1)
        for method in ('turn/start', 'thread/start', 'thread/fork', 'thread/resume', 'command/exec',
                       'fs/writeFile', 'mcpServer/tool/call', 'future/unknownMethod'):
            self.assertTrue(self.barrier.blocks({'id': 2, 'method': method}))
        for message in ({'id': 'approval', 'result': {'decision': 'decline'}},
                        {'id': 3, 'method': 'turn/interrupt'}, {'id': 4, 'method': 'thread/read'}):
            self.assertFalse(self.barrier.blocks(message))
        self.barrier.observe_client({'id': 'approval', 'result': {}})
        self.barrier.observe_runtime({'id': 1, 'result': {}})
        status = self.barrier.status(self.observer, True)
        self.assertEqual(status['pendingMutationCount'], 0)
        self.assertEqual(status['pendingApprovalCount'], 0)
        self.assertNotIn('safeToRestart', status)
        self.assertNotIn('leaseToken', status)

    def test_unknown_auth_incomplete_stream_or_uninitialized_cannot_acquire(self):
        for field in ('connected', 'initialized', 'stream_complete'):
            with self.assertRaises(AdminError):
                self.barrier.request('manager/maintenance/acquire', {'transactionId': self.txid},
                                     {**self.observer, field: False}, True)
        with self.assertRaises(AdminError):
            self.barrier.request('manager/maintenance/acquire', {'transactionId': self.txid}, self.observer, False)


@unittest.skipUnless(os.name == 'nt', 'Windows AF_PIPE and user DPAPI are required')
class NamedPipeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.profile_id, self.generation, self.thread_id = [str(uuid4()) for _ in range(3)]
        self.calls = []
        def dispatch(method, params, timeout, lease):
            self.calls.append((method, params))
            if method == 'thread/managedCloseIdle':
                return {'threadId': params['threadId'], 'closedThreadIds': [params['threadId']],
                        'writerReleaseVerified': True, 'apiKey': 'DO-NOT-RETURN'}
            return {'data': [self.thread_id], 'nextCursor': None, 'accessToken': 'DO-NOT-RETURN'}
        self.server = AdminServer(self.root, self.profile_id, self.generation, os.getpid(), dispatch).start()
        self.client = AdminClient(self.root, self.profile_id, self.generation)

    def tearDown(self):
        self.server.close()
        self.temporary.cleanup()

    def test_real_named_pipe_dpapi_roundtrip_persists_no_plain_auth_or_rpc(self):
        result = self.client.request('thread/managedCloseIdle', {'threadId': self.thread_id}, 3)
        self.assertTrue(result['writerReleaseVerified'])
        descriptor = self.server.path.read_text(encoding='utf-8')
        self.assertNotIn(base64.b64encode(self.server._key).decode(), descriptor)
        self.assertNotIn(self.thread_id, descriptor)
        self.assertNotIn('DO-NOT-RETURN', descriptor)
        self.assertNotIn('apiKey', json.dumps(result))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.client.request('thread/loaded/list', {}, 3),
                         {'data': [self.thread_id], 'nextCursor': None})
        identities = self.client.identities()
        self.assertEqual(set(identities), {'runtime', 'proxy', 'generation'})
        self.assertEqual(identities['runtime']['pid'], os.getpid())

    def test_wrong_key_cannot_dispatch_and_valid_client_still_works(self):
        with self.assertRaises(Exception):
            Client(self.server._address, family='AF_PIPE', authkey=b'wrong-authentication-key')
        self.assertFalse(self.calls)
        self.client.request('thread/loaded/list', {}, 3)
        self.assertEqual(len(self.calls), 1)

    def test_descriptor_generation_and_process_birth_are_validated(self):
        descriptor = json.loads(self.server.path.read_text(encoding='utf-8'))
        descriptor['runtime']['created'] += 1
        self.server.path.write_text(json.dumps(descriptor), encoding='utf-8')
        with self.assertRaises(AdminError) as caught:
            self.client.request('thread/loaded/list', {}, 1)
        self.assertEqual(caught.exception.code, 'stale_runtime')
        self.assertFalse(self.calls)
        other = AdminClient(self.root, self.profile_id, str(uuid4()))
        with self.assertRaises(AdminError):
            other.request('thread/loaded/list', {}, 1)

    def test_connected_request_with_wrong_generation_is_rejected(self):
        connection = Client(self.server._address, family='AF_PIPE', authkey=self.server._key)
        try:
            envelope = {'version': _VERSION, 'profile_id': self.profile_id,
                'generation': str(uuid4()), 'runtime': self.server.runtime,
                'request_id': str(uuid4()), 'deadline': time.monotonic() + 2,
                'method': 'thread/managedCloseIdle', 'params': {'threadId': self.thread_id}, 'maintenance_lease': None}
            connection.send_bytes(json.dumps(envelope).encode())
            self.assertTrue(connection.poll(3))
            response = json.loads(connection.recv_bytes())
            self.assertEqual(response['error']['code'], 'stale_runtime')
            self.assertFalse(self.calls)
        finally:
            connection.close()

    def test_expired_request_never_dispatches(self):
        connection = Client(self.server._address, family='AF_PIPE', authkey=self.server._key)
        try:
            envelope = {'version': _VERSION, 'profile_id': self.profile_id,
                'generation': self.generation, 'runtime': self.server.runtime,
                'request_id': str(uuid4()), 'deadline': time.monotonic() - 1,
                'method': 'thread/managedCloseIdle', 'params': {'threadId': self.thread_id}, 'maintenance_lease': None}
            connection.send_bytes(json.dumps(envelope).encode())
            self.assertTrue(connection.poll(3))
            response = json.loads(connection.recv_bytes())
            self.assertEqual(response['error']['code'], 'timeout')
            self.assertFalse(response['error']['uncertain'])
            self.assertFalse(self.calls)
        finally:
            connection.close()

    def test_oversized_authenticated_frame_never_dispatches(self):
        connection = Client(self.server._address, family='AF_PIPE', authkey=self.server._key)
        try:
            try:
                connection.send_bytes(b'x' * (_MAX_REQUEST + 1))
                self.assertTrue(connection.poll(3))
                with self.assertRaises((OSError, EOFError)):
                    connection.recv_bytes()
            except BrokenPipeError:
                pass  # The server can close the oversized frame before send finishes.
            self.assertFalse(self.calls)
        finally:
            connection.close()

    def test_no_pickle_connection_methods_are_used(self):
        with (patch('multiprocessing.connection._ConnectionBase.send', side_effect=AssertionError('pickle send')),
              patch('multiprocessing.connection._ConnectionBase.recv', side_effect=AssertionError('pickle recv'))):
            self.client.request('thread/loaded/list', {}, 3)

    def test_client_timeout_is_uncertain_and_never_retries(self):
        release = threading.Event()
        entered = threading.Event()
        def slow(method, params, timeout, lease):
            self.calls.append((method, params))
            entered.set()
            release.wait(2)
            return {'threadId': self.thread_id, 'closedThreadIds': [self.thread_id], 'writerReleaseVerified': True}
        self.server._dispatch = slow
        try:
            with self.assertRaises(AdminError) as caught:
                self.client.request('thread/managedCloseIdle', {'threadId': self.thread_id}, .15)
            self.assertTrue(entered.is_set())
            self.assertEqual(caught.exception.code, 'timeout')
            self.assertTrue(caught.exception.uncertain)
            self.assertEqual(len(self.calls), 1)
        finally:
            release.set()

    def test_malformed_acknowledgement_after_send_is_uncertain(self):
        class MalformedConnection:
            def send_bytes(self, value): pass
            def poll(self, timeout): return True
            def recv_bytes(self, limit): return b'{"ok":true,"ok":false}'
            def close(self): pass
        with (patch('manager_core.runtime_admin._connect_pipe', return_value=MalformedConnection()),
              patch('multiprocessing.connection.answer_challenge'),
              patch('multiprocessing.connection.deliver_challenge')):
            with self.assertRaises(AdminError) as caught:
                self.client.request('thread/managedCloseIdle', {'threadId': self.thread_id}, 1)
        self.assertEqual(caught.exception.code, 'invalid_response')
        self.assertTrue(caught.exception.uncertain)
        self.assertFalse(self.calls)

    def test_stalled_authentication_is_closed_at_deadline_without_sending(self):
        closed = threading.Event()
        sent = []
        class StalledConnection:
            def send_bytes(self, value): sent.append(value)
            def close(self): closed.set()
        def stalled_auth(connection, key):
            closed.wait(2)
            raise EOFError()
        with (patch('manager_core.runtime_admin._connect_pipe', return_value=StalledConnection()),
              patch('multiprocessing.connection.answer_challenge', side_effect=stalled_auth),
              patch('multiprocessing.connection.deliver_challenge')):
            with self.assertRaises(AdminError) as caught:
                self.client.request('thread/managedCloseIdle', {'threadId': self.thread_id}, .05)
        self.assertEqual(caught.exception.code, 'timeout')
        self.assertFalse(caught.exception.uncertain)
        self.assertTrue(closed.wait(1))
        self.assertFalse(sent)


@unittest.skipUnless(os.name == 'nt', 'Windows AF_PIPE and user DPAPI are required')
class ProxyIntegrationTests(unittest.TestCase):
    def test_native_bootstrap_direct_script_entrypoint_imports_admin_package(self):
        script = Path(__file__).resolve().parents[1] / 'scripts/manager_core/runtime_proxy.py'
        result = subprocess.run([sys.executable, str(script), '--version'],
            env={**os.environ, 'CODEX_MANAGER_REAL_RUNTIME': sys.executable},
            text=True, encoding='utf-8', capture_output=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith('Python '))

    def test_admin_reuses_existing_stdio_and_maintenance_blocks_only_new_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile_id, generation, thread_id = [str(uuid4()) for _ in range(3)]
            observer = root / 'work/control-center/instances' / profile_id / 'runtime-state.json'
            fake = root / 'fake.py'
            fake.write_text('''import sys,json
for line in sys.stdin:
    message=json.loads(line)
    method=message.get('method')
    if 'id' not in message: continue
    if method=='initialize': result={'userAgent':'fixture'}
    elif method=='account/read': result={'account':{'type':'apiKey'}}
    elif method=='thread/managedCloseIdle':
        tid=message['params']['threadId']
        result={'threadId':tid,'closedThreadIds':[tid],'writerReleaseVerified':True,'apiKey':'SENSITIVE-ADMIN'}
    elif method=='thread/read':
        tid=message['params']['threadId']
        result={'thread':{'id':tid,'sessionId':tid,'parentThreadId':None,'forkedFromId':None,'status':{'type':'idle'},'preview':'SENSITIVE-PREVIEW'}}
    elif method=='thread/loaded/list': result={'data':[],'nextCursor':None}
    else: result={'forwarded':True}
    print(json.dumps({'id':message['id'],'result':result}),flush=True)
''', encoding='utf-8')
            bootstrap = root / 'bootstrap.py'
            bootstrap.write_text('''import sys,os
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from manager_core.runtime_proxy import proxy
environment={k:v for k,v in os.environ.items() if not k.startswith('CODEX_MANAGER_')}
environment.update(CODEX_MANAGER_ROOT=sys.argv[2],CODEX_MANAGER_GENERATION=sys.argv[4],CODEX_MANAGER_REAL_RUNTIME=sys.executable)
raise SystemExit(proxy(Path(sys.executable),[sys.argv[5],'app-server'],Path(sys.argv[6]),sys.argv[3],environment))
''', encoding='utf-8')
            process = subprocess.Popen([sys.executable, str(bootstrap),
                str(Path(__file__).resolve().parents[1] / 'scripts'), str(root), profile_id,
                generation, str(fake), str(observer)], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
                creationflags=subprocess.CREATE_NO_WINDOW)
            replies = queue.Queue()
            received = []
            def read_stdout():
                for line in process.stdout:
                    message = json.loads(line)
                    received.append(message)
                    replies.put(message)
            reader = threading.Thread(target=read_stdout, daemon=True)
            reader.start()
            def send(value):
                process.stdin.write(json.dumps(value) + '\n')
                process.stdin.flush()
            client = AdminClient(root, profile_id, generation)
            try:
                send({'id': 1, 'method': 'initialize', 'params': {}})
                self.assertEqual(replies.get(timeout=5)['id'], 1)
                send({'method': 'initialized'})
                send({'id': 2, 'method': 'account/read', 'params': {}})
                self.assertEqual(replies.get(timeout=5)['id'], 2)
                result = client.request('thread/read', {'threadId': thread_id}, 3)
                self.assertEqual(result['thread']['sessionId'], thread_id)
                self.assertIsNone(result['thread']['parentThreadId'])
                self.assertNotIn('preview', result['thread'])
                send({'id': 77, 'method': 'thread/managedCloseIdle', 'params': {'threadId': thread_id}})
                self.assertEqual(replies.get(timeout=5)['error']['code'], -32043)
                lease = client.request('manager/maintenance/acquire', {'transactionId': str(uuid4())}, 3)
                self.assertTrue(lease['frontendMutationBlocked'])
                other_client = AdminClient(root, profile_id, generation)
                with self.assertRaises(AdminError) as caught:
                    other_client.request('thread/managedCloseIdle', {'threadId': thread_id}, 3)
                self.assertEqual(caught.exception.code, 'busy')
                send({'id': 3, 'method': 'turn/start', 'params': {'threadId': thread_id}})
                self.assertEqual(replies.get(timeout=5)['error']['code'], -32044)
                proof = client.request('thread/managedCloseIdle', {'threadId': thread_id}, 3)
                self.assertTrue(proof['writerReleaseVerified'])
                status = client.request('manager/maintenance/status',
                    {'transactionId': lease['transactionId'], 'leaseToken': lease['leaseToken']}, 3)
                self.assertEqual(status['pendingMutationCount'], 0)
                self.assertNotIn('leaseToken', status)
                client.request('manager/maintenance/release',
                    {'transactionId': lease['transactionId'], 'leaseToken': lease['leaseToken']}, 3)
                send({'id': 4, 'method': 'turn/start', 'params': {'threadId': thread_id}})
                self.assertTrue(replies.get(timeout=5)['result']['forwarded'])
                self.assertEqual([message['id'] for message in received], [1, 2, 77, 3, 4])
                self.assertNotIn('SENSITIVE-ADMIN', json.dumps(received))
                process.stdin.close()
                self.assertEqual(process.wait(timeout=5), 0)
                reader.join(2)
                self.assertFalse(client.path.exists())
                saved = observer.read_text(encoding='utf-8')
                self.assertNotIn('SENSITIVE', saved)
                self.assertNotIn(lease['leaseToken'], saved)
                self.assertEqual(json.loads(saved)['admin_channel']['state'], 'closed')
                with self.assertRaises(AdminError):
                    client.request('thread/loaded/list', {}, 1)
            finally:
                if process.poll() is None:
                    process.stdin.close()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        process.wait(timeout=5)
                process.stdout.close()
                errors = process.stderr.read()
                process.stderr.close()
                self.assertEqual(errors, '')


if __name__ == '__main__':
    unittest.main()
