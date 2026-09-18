"""Headless, real-pipe SSH/auth pump tests using only local synthetic peers.

The production native bootstrap owns each fixture tree. The apparent SSH peer is
another Python process: no network, GUI, account file or real credential is used.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import queue
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
BOOTSTRAP = ROOT / 'manager/SshProxy/bin/Release/net10.0-windows/ssh.exe'
MARKER = bytes.fromhex('0001277f80c8feff')
BANNER = b'fixture login banner\xff\x00\n'
REQUEST = (b'GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n'
           b'Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n'
           b'Sec-WebSocket-Version: 13\r\n\r\n')
RESPONSE = (b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n'
            b'Connection: Upgrade\r\nSec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n\r\n')
NOW = 1_800_000_000
SIZE = 2 * 1024 * 1024


def fixture_token():
    claims = {'exp': NOW + 3600,
              'https://api.openai.com/auth': {'chatgpt_account_id': 'pump-fixture-account'}}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    return 'fixture.' + payload + '.SENSITIVE-pump-fixture-only'


def frame(message, *, masked=False):
    """Independent fixture encoder, not the production WebSocket codec."""
    payload = json.dumps(message, ensure_ascii=False, separators=(',', ':')).encode()
    mask_bit = 128 if masked else 0
    size = len(payload)
    if size < 126:
        header = bytes((0x81, size | mask_bit))
    elif size < 65536:
        header = bytes((0x81, 126 | mask_bit)) + struct.pack('!H', size)
    else:
        header = bytes((0x81, 127 | mask_bit)) + struct.pack('!Q', size)
    if not masked:
        return header + payload
    key = b'\x13\x37\x55\xaa'
    return header + key + bytes(value ^ key[index % 4] for index, value in enumerate(payload))


def read_exact(stream, size, record=None):
    result = bytearray()
    while len(result) < size:
        part = stream.read(size - len(result))
        if not part:
            raise EOFError('Fixture peer ended before the requested bytes arrived.')
        if record is not None:
            record.append(part)
        result.extend(part)
    return bytes(result)


def read_headers(stream, record=None):
    header = bytearray()
    while not header.endswith(b'\r\n\r\n'):
        if len(header) > 32768:
            raise ValueError('Fixture HTTP header limit.')
        header.extend(read_exact(stream, 1, record))
    return bytes(header)


def read_frame(stream, *, masked, record=None):
    # EOF is normal only between frames, never in a partially delivered frame.
    first = read_exact(stream, 1, record)[0]
    try:
        return _read_frame_body(stream, first, masked=masked, record=record)
    except EOFError:
        raise ValueError('Fixture peer ended inside a WebSocket frame.') from None


def _read_frame_body(stream, first, *, masked, record=None):
    second = read_exact(stream, 1, record)[0]
    if first != 0x81 or bool(second & 128) != masked:
        raise ValueError('Unexpected fixture frame type or direction.')
    size = second & 127
    if size == 126:
        size = struct.unpack('!H', read_exact(stream, 2, record))[0]
    elif size == 127:
        size = struct.unpack('!Q', read_exact(stream, 8, record))[0]
    if size > 4 * 1024 * 1024:
        raise ValueError('Fixture message size limit.')
    key = read_exact(stream, 4, record) if masked else None
    payload = read_exact(stream, size, record)
    if masked:
        payload = bytes(value ^ key[index % 4] for index, value in enumerate(payload))
    return json.loads(payload)


def send(stream, message, *, masked=False):
    stream.write(frame(message, masked=masked))
    stream.flush()


def runtime_fixture(directory, scenario):
    directory = Path(directory)
    (directory / 'runtime.pid').write_text(str(os.getpid()))
    source, target = sys.stdin.buffer, sys.stdout.buffer
    # Real binary marker split across writes, including non-UTF-8 login output.
    target.write(BANNER + MARKER[:3])
    target.flush()
    target.write(MARKER[3:])
    target.flush()
    if read_headers(source) != REQUEST:
        raise ValueError('Fixture HTTP request changed.')
    target.write(RESPONSE)
    target.flush()
    initialize = read_frame(source, masked=True)
    if initialize['method'] != 'initialize' or not initialize['params']['capabilities']['experimentalApi']:
        raise ValueError('Experimental auth capability was not enabled.')
    send(target, {'id': initialize['id'], 'result': {'userAgent': 'fixture-runtime'}})
    # App-server accepts account binding immediately after initialize. Clients
    # that also send the optional notification can race it with that binding.
    handshake = [read_frame(source, masked=True), read_frame(source, masked=True)]
    initialized = next((m for m in handshake if m.get('method') == 'initialized'), {})
    login = next((m for m in handshake if m.get('method') == 'account/login/start'), {})
    if (initialized['method'] != 'initialized' or login['method'] != 'account/login/start'
            or login['params']['accessToken'] != fixture_token()
            or login['params']['chatgptAccountId'] != 'pump-fixture-account'):
        raise ValueError('Synthetic fixture account was not injected.')
    # Even an internal response echoing a credential must disappear from the UI.
    send(target, {'id': login['id'], 'result': {'type': 'chatgptAuthTokens',
                                             'ignoredEcho': fixture_token()}})
    send(target, {'method': 'fixture/ready', 'params': {'accountInjected': True}})
    report = {'accountInjected': True, 'experimentalApi': True}
    if scenario == 'duplex':
        # Deliberately do not read stdin until this entire output has been written.
        # The client first sends >pipe-buffer input. A pump holding a global state
        # lock during that write cannot drain this output and deadlocks here.
        import ctypes
        import msvcrt
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.PeekNamedPipe.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                           ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
        kernel32.PeekNamedPipe.restype = ctypes.c_int
        available = ctypes.c_uint32()
        deadline = time.monotonic() + 15
        while not available.value:
            if not kernel32.PeekNamedPipe(msvcrt.get_osfhandle(source.fileno()), None, 0,
                                         None, ctypes.byref(available), None):
                raise OSError('Fixture could not inspect its local input pipe.')
            if time.monotonic() > deadline:
                raise TimeoutError('The pump never attempted its large child-stdin write.')
            time.sleep(0.01)
        ready = directory / 'input-pipe-ready.json'
        temporary = ready.with_suffix('.tmp')
        temporary.write_text(json.dumps({'bytesAvailable': available.value}))
        temporary.replace(ready)
        release = directory / 'release-output'
        deadline = time.monotonic() + 15
        while not release.exists():
            if time.monotonic() > deadline:
                raise TimeoutError('Fixture output gate was not released.')
            time.sleep(0.01)
        send(target, {'method': 'fixture/large', 'params': {'data': 'R' * SIZE}})
        message = read_frame(source, masked=True)
        data = message['params']['data']
        if message['id'] != 2 or data != 'C' * SIZE:
            raise ValueError('Fixture request bytes changed.')
        digest = hashlib.sha256(data.encode()).hexdigest()
        report.update(receivedBytes=len(data), receivedSha256=digest)
        send(target, {'id': 2, 'result': {'receivedBytes': len(data), 'sha256': digest}})
        if source.read() != b'':
            raise ValueError('Unexpected data after fixture request.')
        send(target, {'method': 'fixture/after-eof', 'params': {'complete': True}})
        code = 0
    elif scenario == 'half-close':
        message = read_frame(source, masked=True)
        if message['id'] != 2 or message['method'] != 'fixture/late':
            raise ValueError('Unexpected late-response request.')
        if source.read() != b'':
            raise ValueError('Unexpected data before fixture half-close.')
        time.sleep(0.15)
        send(target, {'id': 2, 'result': {'afterEof': True, 'value': '한글'}})
        time.sleep(0.05)
        send(target, {'method': 'fixture/after-eof', 'params': {'complete': True}})
        report['sawInputEofBeforeResponse'] = True
        code = 23
    elif scenario == 'idle':
        message = read_frame(source, masked=True)
        if message.get('method') != 'fixture/ping':
            raise ValueError('Unexpected independent connection request.')
        send(target, {'id': message['id'], 'result': {'pong': True}})
        if source.read() != b'':
            raise ValueError('Unexpected independent connection trailing data.')
        code = 0
    elif scenario == 'protocol-failure':
        message = read_frame(source, masked=True)
        if message.get('id') != 2:
            raise ValueError('Unexpected failure fixture request.')
        # Server frames must be unmasked. The invalid payload itself must never
        # appear on frontend stdout or in diagnostic/audit files.
        send(target, {'method': 'SENSITIVE-invalid-protocol-payload'}, masked=True)
        time.sleep(60)
        raise AssertionError('The failing local transport was not terminated.')
    else:
        raise ValueError('Unknown fixture scenario.')
    (directory / 'runtime-report.json').write_text(json.dumps(report))
    return code


def pump_driver(directory, scenario):
    from manager_core import proxy_auth, ssh_shim
    directory = Path(directory)
    original_auth = proxy_auth.AuthProxy
    original_popen = subprocess.Popen
    original_write = os.write
    children = []

    def fixture_auth(**_):
        return original_auth(source_home=None,
                             token_loader=lambda: proxy_auth.AuthTokens(fixture_token(), 'pump-fixture-account'),
                             clock=lambda: NOW)

    def capture_child(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        children.append(child)
        return child

    def observe_write(fd, data):
        watched = (scenario == 'duplex' and children and not children[0].stdin.closed
                   and fd == children[0].stdin.fileno() and len(data) > SIZE)
        if watched:
            (directory / 'large-write-entered').touch()
        count = original_write(fd, data)
        if watched:
            (directory / 'large-write-returned').touch()
        return count

    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith('CODEX_')}
    with patch.object(proxy_auth, 'AuthProxy', fixture_auth), \
            patch.object(ssh_shim.subprocess, 'Popen', capture_child), \
            patch.object(ssh_shim.os, 'write', observe_write):
        code = ssh_shim._proxy_with_auth(
            Path(sys.executable), ['-u', str(Path(__file__).resolve()), '--fixture-runtime', str(directory), scenario],
            environment, {}, directory / 'ssh-bindings.json',
            {'operation': 'native-proxy', 'alias': 'fixture-no-network'}, MARKER)
    report = {'exitCode': code, 'childExitedBeforePumpReturned': bool(children) and children[0].poll() is not None}
    (directory / 'driver-report.json').write_text(json.dumps(report))
    return code


class FixtureClient:
    def __init__(self, directory, scenario):
        self.directory = directory
        self.raw = []
        self.messages = []
        self.inbox = queue.Queue()
        self.reader_error = None
        environment = {key: value for key, value in os.environ.items()
                       if not key.upper().startswith('CODEX_')}
        environment.update(CODEX_MANAGER_SSH_PYTHON=sys.executable,
                           CODEX_MANAGER_SSH_SCRIPT=str(Path(__file__).resolve()), PYTHONUTF8='1')
        self.process = subprocess.Popen([str(BOOTSTRAP), '--pump-driver', str(directory), scenario],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            stream = self.process.stdout
            prefix = read_exact(stream, len(BANNER) + len(MARKER), self.raw)
            if prefix != BANNER + MARKER:
                raise ValueError('The native marker prefix changed.')
            if read_headers(stream, self.raw) != RESPONSE:
                raise ValueError('The HTTP response changed.')
            while True:
                message = read_frame(stream, masked=False, record=self.raw)
                self.messages.append(message)
                self.inbox.put(message)
        except EOFError:
            self.inbox.put(None)
        except Exception as error:
            self.reader_error = error
            # Still capture every byte if parsing fails. Otherwise an invalid
            # header could hide a leaked secret in an unread payload from tests.
            while True:
                remainder = self.process.stdout.read(65536)
                if not remainder:
                    break
                self.raw.append(remainder)
            self.inbox.put(error)

    def open(self):
        self.process.stdin.write(REQUEST)
        send(self.process.stdin, {'id': 1, 'method': 'initialize', 'params': {}}, masked=True)
        self.wait_for(lambda message: message.get('id') == 1)
        send(self.process.stdin, {'method': 'initialized'}, masked=True)
        self.wait_for(lambda message: message.get('method') == 'fixture/ready')

    def wait_for(self, predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while True:
            try:
                message = self.inbox.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                raise AssertionError('The fixture pump stalled waiting for a protocol message.') from None
            if message is None:
                raise AssertionError('The fixture pump ended before the expected response.')
            if isinstance(message, Exception):
                raise AssertionError('Fixture reader rejected output.') from message
            if predicate(message):
                return message

    def finish(self, timeout=15):
        code = self.process.wait(timeout=timeout)
        self.reader.join(timeout=5)
        if self.reader.is_alive():
            raise AssertionError('Fixture stdout did not close.')
        if self.reader_error is not None:
            raise AssertionError('Fixture reader rejected output.') from self.reader_error
        return code, self.process.stderr.read()

    def cleanup(self):
        if self.process.poll() is None:
            # The production bootstrap's Job Object kills only this fixture tree.
            self.process.kill()
        self.process.wait(timeout=5)
        self.reader.join(timeout=5)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None and not stream.closed:
                stream.close()


@unittest.skipUnless(os.name == 'nt', 'Windows native bootstrap integration')
class PumpIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not BOOTSTRAP.is_file():
            raise RuntimeError('Build manager/SshProxy in Release before running pump integration fixtures.')
        cls.work = (ROOT / 'work/ssh-pump-tests').resolve()
        cls.work.mkdir(parents=True, exist_ok=True)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pump fixture # 한글 ', dir=self.work)
        self.directory = Path(self.temp.name).resolve()
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.cleanup()
        self.assertTrue(self.directory.is_relative_to(self.work))
        self.temp.cleanup()

    def client(self, scenario, *, name=None):
        directory = self.directory / name if name else self.directory
        directory.mkdir(exist_ok=True)
        result = FixtureClient(directory, scenario)
        self.clients.append(result)
        result.open()
        return result

    def assert_private_auth_hidden(self, client, stderr):
        wire = b''.join(client.raw)
        self.assertTrue(wire.startswith(BANNER + MARKER + RESPONSE))
        self.assertNotIn(fixture_token().encode(), wire + stderr)
        self.assertNotIn(b'codex-manager-auth:', wire)
        self.assertNotIn(b'account/login/start', wire)
        self.assertEqual(stderr, b'')
        audit = (client.directory / 'ssh-routing.jsonl').read_bytes()
        self.assertNotIn(fixture_token().encode(), audit)
        self.assertNotIn(b'accessToken', audit)
        report = json.loads((client.directory / 'driver-report.json').read_text())
        self.assertTrue(report['childExitedBeforePumpReturned'])

    def test_duplex_backpressure_preserves_two_mebibytes_each_direction(self):
        client = self.client('duplex')
        send(client.process.stdin, {'id': 2, 'method': 'fixture/large', 'params': {'data': 'C' * SIZE}}, masked=True)
        # The child reports real unread bytes in its stdin pipe, so this gate is
        # based on an observed write attempt rather than a scheduling delay.
        gate = self.directory / 'input-pipe-ready.json'
        deadline = time.monotonic() + 15
        while not gate.exists():
            if time.monotonic() > deadline:
                self.fail('The child never observed the pending large input write.')
            time.sleep(0.01)
        available = json.loads(gate.read_text())['bytesAvailable']
        self.assertGreater(available, 0)
        # PeekNamedPipe may count the entire pending synchronous WriteFile buffer,
        # even when its writer is still blocked. Observe call entry/return too.
        self.assertTrue((self.directory / 'large-write-entered').exists())
        self.assertFalse((self.directory / 'large-write-returned').exists())
        (self.directory / 'release-output').touch()
        notification = client.wait_for(lambda message: message.get('method') == 'fixture/large')
        self.assertEqual(notification['params']['data'], 'R' * SIZE)
        response = client.wait_for(lambda message: message.get('id') == 2)
        self.assertEqual(response['result']['receivedBytes'], SIZE)
        self.assertEqual(response['result']['sha256'], hashlib.sha256(('C' * SIZE).encode()).hexdigest())
        client.process.stdin.close()
        client.wait_for(lambda message: message.get('method') == 'fixture/after-eof')
        code, stderr = client.finish()
        self.assertEqual(code, 0, stderr.decode(errors='replace'))
        self.assert_private_auth_hidden(client, stderr)

    def test_input_half_close_delivers_pending_responses_and_nonzero_exit(self):
        client = self.client('half-close')
        send(client.process.stdin, {'id': 2, 'method': 'fixture/late'}, masked=True)
        client.process.stdin.close()
        response = client.wait_for(lambda message: message.get('id') == 2)
        self.assertEqual(response['result'], {'afterEof': True, 'value': '한글'})
        client.wait_for(lambda message: message.get('method') == 'fixture/after-eof')
        code, stderr = client.finish()
        self.assertEqual(code, 23, stderr.decode(errors='replace'))
        self.assert_private_auth_hidden(client, stderr)
        report = json.loads((self.directory / 'runtime-report.json').read_text())
        self.assertTrue(report['sawInputEofBeforeResponse'])

    def test_invalid_protocol_terminates_only_its_local_transport(self):
        # A second real ssh.exe bootstrap catches process-name-wide cleanup, not
        # merely a kill that accidentally affects a generic Python sentinel.
        sentinel = self.client('idle', name='independent-connection')
        client = self.client('protocol-failure')
        send(client.process.stdin, {'id': 2, 'method': 'fixture/fail'}, masked=True)
        code, stderr = client.finish()
        self.assertEqual(code, 125, stderr.decode(errors='replace'))
        self.assertIsNone(sentinel.process.poll(), 'The independent SSH fixture must remain running.')
        self.assert_private_auth_hidden(client, stderr)
        wire = b''.join(client.raw)
        self.assertNotIn(b'SENSITIVE-invalid-protocol-payload', wire)
        audit = (self.directory / 'ssh-routing.jsonl').read_text()
        self.assertIn('ssh_auth_transport_failed', audit)
        self.assertNotIn('SENSITIVE-invalid-protocol-payload', audit)
        send(sentinel.process.stdin, {'id': 7, 'method': 'fixture/ping'}, masked=True)
        pong = sentinel.wait_for(lambda message: message.get('id') == 7)
        self.assertEqual(pong['result'], {'pong': True})
        sentinel.process.stdin.close()
        code, stderr = sentinel.finish()
        self.assertEqual(code, 0, stderr.decode(errors='replace'))
        self.assert_private_auth_hidden(sentinel, stderr)


if __name__ == '__main__':
    arguments = sys.argv[1:]
    if arguments[:1] == ['--']:
        arguments.pop(0)
    if arguments[:1] == ['--fixture-runtime']:
        raise SystemExit(runtime_fixture(*arguments[1:]))
    if arguments[:1] == ['--pump-driver']:
        raise SystemExit(pump_driver(*arguments[1:]))
    unittest.main(verbosity=2)
