import errno
import json
import os
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

if os.name == 'nt':
    from multiprocessing.connection import Client, Listener
    from manager_core import rust_service


@unittest.skipUnless(os.name == 'nt', 'Windows named pipe broker client')
class BrokerRequestTests(unittest.TestCase):
    """Drive rust_service.request against a private fixture pipe, never the manager's."""

    def setUp(self):
        self.name = 'CodexControlCenter.service.fixture-' + uuid4().hex
        self.enterContext(patch.dict(os.environ, CODEX_MANAGER_SERVICE_PIPE=self.name,
                                     CODEX_MANAGER_BROKER_TOKEN='fixture-token'))
        self.sleeps, self.clock = [], None

    def serve(self, reply):
        """Accept one request; reply(connection, request) writes the answer."""
        listener = Listener('\\\\.\\pipe\\' + self.name, family='AF_PIPE')
        received, release = [], threading.Event()
        def run():
            with listener.accept() as connection:
                request = json.loads(connection.recv_bytes())
                received.append(request)
                reply(connection, request)
                # Keep the server end open until the client has finished reading.
                release.wait(5)
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        def cleanup():
            release.set()
            worker.join(5)
            listener.close()
        self.addCleanup(cleanup)
        return received, release

    def timing(self, *, fake_clock=False):
        """Record the client's waits; a fake clock makes deadline tests instant."""
        self.clock = 0.0 if fake_clock else None
        def sleep(seconds):
            self.sleeps.append(seconds)
            if fake_clock:
                self.clock += seconds
            else:
                time.sleep(seconds)
        monotonic = (lambda: self.clock) if fake_clock else time.monotonic
        return patch.object(rust_service, 'time', SimpleNamespace(sleep=sleep, monotonic=monotonic))

    def assertBackoff(self, limit):
        expected, delay = [], .0005
        for _ in self.sleeps:
            expected.append(delay)
            delay = min(delay * 2, limit)
        self.assertEqual(self.sleeps, expected)

    @staticmethod
    def answer(request, **fields):
        return (json.dumps(dict(id=request['id'], **fields)) + '\n').encode()

    def test_delayed_reply_backs_off_from_half_a_millisecond_and_keeps_the_request_shape(self):
        identity = dict(process_id=4321, executable_path='C:\\fixture\\Codex.exe', process_created=99)
        def reply(connection, request):
            threading.Event().wait(.05)
            connection.send_bytes(self.answer(request, ok=True, result=identity))
        received, _ = self.serve(reply)
        # The kernel32 prototype is cached at import, not rebuilt per request.
        with self.timing(), patch('ctypes.WinDLL', side_effect=AssertionError('kernel32 rebuilt per request')):
            self.assertEqual(rust_service.request('process.identity', pid=4321), identity)
        self.assertEqual(received[0]['command'], 'process.identity')
        self.assertEqual(received[0]['version'], 27)
        self.assertEqual(received[0]['args'], dict(pid=4321, broker_token='fixture-token'))
        self.assertGreaterEqual(len(self.sleeps), 5)
        self.assertBackoff(.01)

    def test_partial_reply_restarts_the_backoff_and_is_reassembled(self):
        def reply(connection, request):
            encoded = self.answer(request, ok=True, result=dict(stopped=True))
            threading.Event().wait(.03)
            connection.send_bytes(encoded[:10])
            threading.Event().wait(.03)
            connection.send_bytes(encoded[10:])
        self.serve(reply)
        with self.timing():
            self.assertEqual(rust_service.request('process.stop', profile_id='p', generation='g'), dict(stopped=True))
        self.assertGreaterEqual(self.sleeps.count(.0005), 2)
        self.assertLessEqual(max(self.sleeps), .01)

    def test_silent_service_times_out_after_the_same_deadline(self):
        received, _ = self.serve(lambda connection, request: None)
        with self.timing(fake_clock=True), self.assertRaisesRegex(RuntimeError, '응답이 지연되었습니다. 중복 실행하지 않았습니다'):
            rust_service.request('process.identity', pid=1)
        self.assertEqual(len(received), 1)
        self.assertGreaterEqual(sum(self.sleeps), 20)
        self.assertLess(sum(self.sleeps), 20.02)
        self.assertBackoff(.01)

    def test_missing_service_reports_the_connection_failure(self):
        # A missing pipe means the service is gone: no retry.
        with self.timing(fake_clock=True), self.assertRaisesRegex(RuntimeError, 'Rust 관리 서비스에 연결하지 못했습니다'):
            rust_service.request('process.identity', pid=1)
        self.assertEqual(self.sleeps, [])
        for error in (PermissionError(errno.EACCES, 'denied'), OSError(errno.EIO, 'I/O error')):
            with self.subTest(error=type(error).__name__), self.timing(fake_clock=True), \
                    patch.object(rust_service, 'open', create=True, side_effect=error) as opened, \
                    self.assertRaisesRegex(RuntimeError, 'Rust 관리 서비스에 연결하지 못했습니다'):
                rust_service.request('process.identity', pid=1)
            self.assertEqual(opened.call_count, 1)
            self.assertEqual(self.sleeps, [])

    def test_busy_pipe_is_retried_before_anything_is_sent(self):
        address = '\\\\.\\pipe\\' + self.name
        listener = Listener(address, family='AF_PIPE')
        self.addCleanup(listener.close)
        # Occupy the only listening instance, as another client being accepted does.
        blocker = Client(address, family='AF_PIPE')
        self.addCleanup(blocker.close)
        with self.assertRaises(OSError) as raised:
            open(address, 'r+b', buffering=0).close()
        self.assertEqual((raised.exception.errno, getattr(raised.exception, 'winerror', None)), (errno.EINVAL, None))
        received, release = [], threading.Event()
        def run():
            threading.Event().wait(.1)
            # accept() listens on a new instance before returning the blocker's.
            with listener.accept(), listener.accept() as connection:
                request = json.loads(connection.recv_bytes())
                received.append(request)
                connection.send_bytes(self.answer(request, ok=True, result=dict(stopped=True)))
                release.wait(5)
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 5)
        self.addCleanup(release.set)
        with self.timing():
            self.assertEqual(rust_service.request('process.stop', profile_id='p', generation='g'), dict(stopped=True))
        self.assertEqual([r['command'] for r in received], ['process.stop'])
        # The connect backoff (from 1 ms) ran before the reply backoff (from .5 ms).
        self.assertEqual(self.sleeps[:5], [.001, .002, .004, .008, .016])

    def test_busy_pipe_stops_retrying_after_two_seconds(self):
        for error in (OSError(errno.EINVAL, 'Invalid argument'), OSError(0, 'All pipe instances are busy', None, 231)):
            with self.subTest(winerror=getattr(error, 'winerror', None)):
                self.sleeps = []
                with self.timing(fake_clock=True), patch.object(rust_service, 'open', create=True, side_effect=error) as opened, \
                        self.assertRaisesRegex(RuntimeError, 'Rust 관리 서비스에 연결하지 못했습니다'):
                    rust_service.request('process.identity', pid=1)
                self.assertGreater(opened.call_count, 10)
                self.assertGreaterEqual(sum(self.sleeps), 2)
                self.assertLess(sum(self.sleeps), 2.06)
                self.assertLessEqual(max(self.sleeps), .05)

    def test_error_replies_keep_their_messages(self):
        cases = [
            ('mismatched', lambda c, r: c.send_bytes(b'{"id":"other","ok":true,"result":1}\n'), '관리 서비스 응답이 다릅니다'),
            ('refused', lambda c, r: c.send_bytes(self.answer(r, ok=False, error=dict(message='fixture refused'))),
             'fixture refused'),
            ('closed', lambda c, r: c.close(), 'Rust 관리 서비스 연결이 종료되었습니다'),
        ]
        for label, reply, message in cases:
            with self.subTest(label):
                self.name = 'CodexControlCenter.service.fixture-' + uuid4().hex
                os.environ['CODEX_MANAGER_SERVICE_PIPE'] = self.name
                self.serve(reply)
                with self.assertRaisesRegex(RuntimeError, message):
                    rust_service.request('process.identity', pid=1)
        os.environ['CODEX_MANAGER_SERVICE_PIPE'] = 'unrelated-pipe'
        with self.assertRaisesRegex(RuntimeError, '연결 이름이 올바르지 않습니다'):
            rust_service.request('process.identity', pid=1)


if __name__ == '__main__':
    unittest.main()
