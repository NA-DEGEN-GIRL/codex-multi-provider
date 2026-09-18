"""Instance identity, protocol bounds and real Windows named-pipe transport."""
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import threading
import time
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.native_app_bridge import AppBridge, AppBridgeError, NativePipe, MAX_FRAME, encode_frame
from manager_core.instances import process_identity
from manager_core.store import atomic_json


class DescriptorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.profile = dict(id=str(uuid4()), generation=str(uuid4()), process_id=101,
                            process_created=202, executable_path=r'C:\fixture\ChatGPT.exe')
        self.identity = {k: self.profile[k] for k in ('process_id', 'process_created', 'executable_path')}
        self.path = self.root / 'work/control-center/instances' / self.profile['id'] / 'native-app-bridge.json'
        self.descriptor = dict(version=1, profile_id=self.profile['id'], generation=self.profile['generation'],
                               pipe_path=r'\\.\pipe\codex-manager-fixture', app=self.identity)
        atomic_json(self.path, self.descriptor)

    def test_other_profile_generation_pid_birth_or_executable_is_rejected(self):
        for field, value in [('profile_id', str(uuid4())), ('generation', str(uuid4())),
                ('app', {**self.identity, 'process_id': 303}),
                ('app', {**self.identity, 'process_created': 404}),
                ('app', {**self.identity, 'executable_path': r'C:\other\ChatGPT.exe'})]:
            with self.subTest(field=field, value=value):
                atomic_json(self.path, {**self.descriptor, field: value})
                with self.assertRaises(AppBridgeError): AppBridge(self.root, self.profile)

    def test_oversized_descriptor_is_rejected_before_parse(self):
        self.path.write_bytes(b'x' * 16385)
        with self.assertRaises(ValueError): AppBridge(self.root, self.profile)

    def test_call_uses_verified_context_and_only_navigation_read_allowlist(self):
        requests = []
        expected = self.identity
        class Pipe:
            def __init__(self, path, *, expected_identity):
                assert expected_identity == expected
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def request(self, method, params, *, timeout):
                requests.append((method, params))
                return {'success': True, 'contentItems': [{'type': 'inputText', 'text': '{"navigated":true}'}]}
        bridge = AppBridge(self.root, self.profile, pipe_factory=Pipe)
        context, target = str(uuid4()), str(uuid4())
        self.assertEqual(bridge.call('navigate_to_codex_page', {'threadId': target}, context), {'navigated': True})
        self.assertEqual(requests[0][1]['threadId'], context)
        self.assertEqual(requests[0][1]['arguments']['threadId'], target)
        self.assertEqual(requests[0][1]['namespace'], 'codex_app')
        for method in ('send_message_to_thread', 'create_thread', 'handoff_thread'):
            with self.assertRaises(ValueError): bridge.call(method, {}, context)
        self.assertEqual(len(requests), 1)

    def test_frame_is_little_endian_utf8_and_bounded(self):
        message = {'text': '작업 이동'}
        frame = encode_frame(message)
        self.assertEqual(struct.unpack('<I', frame[:4])[0], len(frame) - 4)
        self.assertEqual(json.loads(frame[4:]), message)
        with self.assertRaises(ValueError): encode_frame({'text': 'x' * MAX_FRAME})
        with self.assertRaises(ValueError): encode_frame({'value': float('nan')})


@unittest.skipUnless(os.name == 'nt', 'Windows app uses named pipes.')
class WindowsPipeTests(unittest.TestCase):
    def serve(self, action):
        from multiprocessing.connection import Listener
        path = r'\\.\pipe\codex-manager-fixture-' + uuid4().hex
        listener = Listener(path, family='AF_PIPE')
        done = threading.Event()
        errors = []
        def run():
            try:
                with listener.accept() as connection: action(connection)
            except Exception as error:
                errors.append(error)
            finally:
                listener.close()
                done.set()
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(lambda: self.assertTrue(done.wait(3), 'Fixture server did not finish.'))
        return path, errors

    def test_native_roundtrip_partial_frames_and_actual_server_identity(self):
        def serve(connection):
            frame = connection.recv_bytes(MAX_FRAME)
            self.assertEqual(struct.unpack('<I', frame[:4])[0], len(frame) - 4)
            message = json.loads(frame[4:])
            response = encode_frame(dict(jsonrpc='2.0', id=message['id'], result={'tools': [{'name': 'fixture'}]}))
            for chunk in (response[:2], response[2:7], response[7:]): connection.send_bytes(chunk)
        path, errors = self.serve(serve)
        with NativePipe(path, expected_identity=process_identity(os.getpid())) as pipe:
            self.assertEqual(pipe.identity['process_id'], os.getpid())
            self.assertEqual(pipe.request('tools/list'), {'tools': [{'name': 'fixture'}]})
        self.assertEqual(errors, [])

    def test_wrong_server_birth_rejected_before_sending_a_request(self):
        received = []
        def serve(connection):
            try: received.append(connection.recv_bytes())
            except EOFError: pass
        path, _ = self.serve(serve)
        expected = {**process_identity(os.getpid()), 'process_created': 0}
        with self.assertRaises(AppBridgeError) as raised: NativePipe(path, expected_identity=expected)
        self.assertEqual(raised.exception.code, 'app_identity_changed')
        self.assertEqual(received, [])

    def test_response_timeout_cancels_overlapped_read(self):
        def serve(connection):
            connection.recv_bytes()
            time.sleep(.25)
        path, _ = self.serve(serve)
        with NativePipe(path) as pipe:
            started = time.monotonic()
            with self.assertRaises(AppBridgeError) as raised: pipe.request('tools/list', timeout=.05)
            self.assertEqual(raised.exception.code, 'app_response_timeout')
            self.assertTrue(raised.exception.uncertain)
            self.assertLess(time.monotonic() - started, 1)

    def test_oversized_response_is_rejected_before_body_read(self):
        path, _ = self.serve(lambda connection: (connection.recv_bytes(), connection.send_bytes(struct.pack('<I', MAX_FRAME + 1))))
        with NativePipe(path) as pipe:
            with self.assertRaises(AppBridgeError) as raised: pipe.request('tools/list')
            self.assertEqual(raised.exception.code, 'app_response_unverified')

    def test_missing_pipe_is_sanitized(self):
        with self.assertRaises(AppBridgeError) as raised:
            NativePipe(r'\\.\pipe\codex-manager-missing-' + uuid4().hex, timeout=.05)
        self.assertEqual(raised.exception.code, 'app_pipe_unavailable')


if __name__ == '__main__': unittest.main()
