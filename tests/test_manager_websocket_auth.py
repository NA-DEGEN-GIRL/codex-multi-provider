"""RFC6455 byte fixtures and synthetic auth only; no sockets or real credentials.

The fixture encoder/decoder are independent of the production frame codec.
Reference: https://www.rfc-editor.org/rfc/rfc6455 (sections 4, 5, 7, 8).
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.proxy_auth import AuthProxy, AuthTokens
from manager_core.websocket_auth import (MAX_APP_SERVER_MESSAGE_BYTES, WebSocketAuthBridge,
                                         WebSocketProtocolError)


REQUEST = (
    b'GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n'
    b'Connection: keep-alive, Upgrade\r\n'
    b'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n'
    b'Sec-WebSocket-Version: 13\r\n\r\n'
)
RESPONSE = (
    b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n'
    b'Connection: Upgrade\r\n'
    b'Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n\r\n'
)


def frame(payload=b'', *, opcode=1, fin=True, masked=False, key=b'\x11\x23\x45\x67'):
    """Build test wire bytes, without importing the implementation encoder."""
    if isinstance(payload, dict):
        payload = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
    first = (0x80 if fin else 0) | opcode
    mask_bit = 0x80 if masked else 0
    length = len(payload)
    if length < 126:
        header = bytes([first, mask_bit | length])
    elif length < 65536:
        header = bytes([first, mask_bit | 126]) + struct.pack('!H', length)
    else:
        header = bytes([first, mask_bit | 127]) + struct.pack('!Q', length)
    if masked:
        return header + key + bytes(value ^ key[index % 4] for index, value in enumerate(payload))
    return header + payload


def decode_frames(chunks):
    """Decode output in tests; require complete, canonically sized frames."""
    raw = b''.join(chunks)
    result = []
    offset = 0
    while offset < len(raw):
        first, second = raw[offset:offset + 2]
        offset += 2
        length = second & 127
        if length == 126:
            length = struct.unpack('!H', raw[offset:offset + 2])[0]
            offset += 2
            assert length >= 126
        elif length == 127:
            length = struct.unpack('!Q', raw[offset:offset + 8])[0]
            offset += 8
            assert length >= 65536
        masked = bool(second & 128)
        key = raw[offset:offset + 4] if masked else None
        offset += 4 if masked else 0
        payload = raw[offset:offset + length]
        assert len(payload) == length
        offset += length
        if masked:
            payload = bytes(value ^ key[index % 4] for index, value in enumerate(payload))
        result.append({'opcode': first & 15, 'fin': bool(first & 128), 'masked': masked,
                       'key': key, 'payload': payload})
    return result


def messages(chunks):
    return [json.loads(item['payload']) for item in decode_frames(chunks) if item['opcode'] == 1]


class PassthroughAuth:
    bound = True

    def __init__(self):
        self.seen = []

    def process(self, direction, message):
        self.seen.append((direction, message))
        return SimpleNamespace(runtime=[message] if direction == 'frontend' else [],
                               frontend=[message] if direction == 'runtime' else [], events=[])

    def poll(self):
        return SimpleNamespace(runtime=[], frontend=[], events=[])


class WebSocketFixtureTests(unittest.TestCase):
    def setUp(self):
        self.auth = PassthroughAuth()
        self.bridge = WebSocketAuthBridge(self.auth)

    def opened(self, bridge=None):
        bridge = bridge or self.bridge
        output = bridge.feed('frontend', REQUEST)
        self.assertEqual(b''.join(output.runtime), REQUEST)
        self.assertEqual(output.frontend, [])
        output = bridge.feed('runtime', RESPONSE)
        self.assertEqual(b''.join(output.frontend), RESPONSE)
        self.assertEqual(output.runtime, [])
        self.assertEqual(bridge.state, 'open')
        return bridge

    def rejected(self, direction, raw, bridge=None):
        bridge = bridge or self.bridge
        with self.assertRaises(WebSocketProtocolError) as raised:
            bridge.feed(direction, raw)
        self.assertEqual(bridge.state, 'failed')
        self.assertNotIn('SENSITIVE', str(raised.exception))
        self.assertNotIn('SENSITIVE', repr(raised.exception))

    def test_exact_http_handshake_is_transparent(self):
        self.opened()
        self.assertEqual(self.auth.seen, [])

    def test_one_byte_handshake_reads_and_case_insensitive_headers(self):
        request = REQUEST.replace(b'Upgrade: websocket', b'uPgRaDe: WebSocket')
        runtime, frontend = [], []
        for value in request:
            result = self.bridge.feed('frontend', bytes([value]))
            runtime.extend(result.runtime)
        for value in RESPONSE:
            result = self.bridge.feed('runtime', bytes([value]))
            frontend.extend(result.frontend)
        self.assertEqual(b''.join(runtime), request)
        self.assertEqual(b''.join(frontend), RESPONSE)
        self.assertEqual(self.bridge.state, 'open')

    def test_response_and_first_frame_in_same_read(self):
        message = {'method': 'fixture/notice', 'params': {'value': 7}}
        self.bridge.feed('frontend', REQUEST)
        output = self.bridge.feed('runtime', RESPONSE + frame(message))
        raw = b''.join(output.frontend)
        self.assertTrue(raw.startswith(RESPONSE))
        self.assertEqual(messages([raw[len(RESPONSE):]]), [message])

    def test_client_frame_coalesced_with_request_waits_for_valid_response(self):
        message = {'id': 1, 'method': 'initialize'}
        output = self.bridge.feed('frontend', REQUEST + frame(message, masked=True))
        self.assertEqual(b''.join(output.runtime), REQUEST)
        self.assertEqual(self.auth.seen, [])
        output = self.bridge.feed('runtime', RESPONSE)
        self.assertEqual(b''.join(output.frontend), RESPONSE)
        self.assertEqual(messages(output.runtime), [message])

    def test_failed_handshake_never_releases_coalesced_client_json(self):
        self.bridge.feed('frontend', REQUEST + frame({'method': 'SENSITIVE'}, masked=True))
        self.rejected('runtime', RESPONSE.replace(b'101 Switching Protocols', b'403 Forbidden'))
        self.assertEqual(self.auth.seen, [])
        with self.assertRaises(WebSocketProtocolError):
            self.bridge.feed('runtime', RESPONSE)
        self.assertEqual(self.bridge.poll().runtime, [])

    def test_invalid_handshake_does_not_reach_auth(self):
        requests = [REQUEST.replace(b'GET /', b'POST /'),
                    REQUEST.replace(b'HTTP/1.1', b'HTTP/1.0'),
                    REQUEST.replace(b'websocket', b'http'),
                    REQUEST.replace(b'keep-alive, Upgrade', b'keep-alive'),
                    REQUEST.replace(b': 13\r\n', b': 12\r\n'),
                    REQUEST.replace(b'dGhlIHNhbXBsZSBub25jZQ==', b'SENSITIVE-not-base64'),
                    REQUEST.replace(b'dGhlIHNhbXBsZSBub25jZQ==', b'YQ=='),
                    REQUEST.replace(b'\r\n\r\n', b'\r\nSec-WebSocket-Key: YQ==\r\n\r\n'),
                    REQUEST.replace(b'\r\n\r\n', b'\r\nSec-WebSocket-Version: 13\r\n\r\n')]
        for raw in requests:
            with self.subTest(raw=raw[:30]):
                self.rejected('frontend', raw, WebSocketAuthBridge(self.auth))
        self.assertEqual(self.auth.seen, [])

    def test_wrong_accept_and_non_upgrade_responses_fail_closed(self):
        responses = [RESPONSE.replace(b'101 Switching Protocols', b'200 OK'),
                     RESPONSE.replace(b's3pPLMBiTxaQ9kYGzzhZRbK+xOo=', b'SENSITIVE-invalid'),
                     RESPONSE.replace(b'Upgrade: websocket', b'Upgrade: h2c'),
                     RESPONSE.replace(b'Connection: Upgrade', b'Connection: close'),
                     RESPONSE.replace(b'\r\n\r\n', b'\r\nSec-WebSocket-Accept: duplicate\r\n\r\n')]
        for raw in responses:
            bridge = WebSocketAuthBridge(self.auth)
            bridge.feed('frontend', REQUEST)
            with self.subTest(raw=raw[:30]):
                self.rejected('runtime', raw, bridge)
        self.assertEqual(self.auth.seen, [])

    def test_no_unnegotiated_extension_or_subprotocol(self):
        for header in (b'Sec-WebSocket-Extensions: permessage-deflate',
                       b'Sec-WebSocket-Protocol: unexpected'):
            bridge = WebSocketAuthBridge(self.auth)
            bridge.feed('frontend', REQUEST)
            with self.subTest(header=header):
                self.rejected('runtime', RESPONSE.replace(b'\r\n\r\n', b'\r\n' + header + b'\r\n\r\n'), bridge)

    def test_header_size_is_bounded(self):
        bridge = WebSocketAuthBridge(self.auth, max_header_bytes=256)
        self.rejected('frontend', b'GET / HTTP/1.1\r\nSENSITIVE:' + b'a' * 256, bridge)

    def test_client_json_is_remasked_and_server_json_is_unmasked(self):
        calls = []
        def mask_factory(length):
            calls.append(length)
            return bytes([len(calls)]) * length
        bridge = self.opened(WebSocketAuthBridge(self.auth, mask_factory=mask_factory))
        for index in (1, 2):
            message = {'id': index, 'method': 'thread/list'}
            output = bridge.feed('frontend', frame(message, masked=True))
            decoded = decode_frames(output.runtime)
            self.assertEqual(len(decoded), 1)
            self.assertTrue(decoded[0]['masked'])
            self.assertEqual(decoded[0]['key'], bytes([index]) * 4)
            self.assertEqual(messages(output.runtime), [message])
        response = {'id': 1, 'result': {'data': []}}
        output = bridge.feed('runtime', frame(response))
        self.assertFalse(decode_frames(output.frontend)[0]['masked'])
        self.assertEqual(messages(output.frontend), [response])
        self.assertEqual(calls, [4, 4])

    def test_many_frames_in_one_read_preserve_message_order(self):
        self.opened()
        expected = [{'id': index, 'method': 'fixture/test'} for index in range(5)]
        output = self.bridge.feed('frontend', b''.join(frame(item, masked=True) for item in expected))
        self.assertEqual(messages(output.runtime), expected)

    def test_utf8_split_across_fragments_and_interleaved_ping(self):
        self.opened()
        payload = '{"value":"한글"}'.encode()
        split = payload.index('한'.encode()) + 1
        first = frame(payload[:split], masked=True, fin=False)
        self.assertEqual(self.bridge.feed('frontend', first).runtime, [])
        ping = frame(b'ping-fixture', opcode=9, masked=True)
        output = self.bridge.feed('frontend', ping)
        self.assertEqual(b''.join(output.runtime), ping)
        self.assertEqual(self.auth.seen, [])
        output = self.bridge.feed('frontend', frame(payload[split:], opcode=0, masked=True))
        self.assertEqual(messages(output.runtime), [{'value': '한글'}])
        self.assertEqual(len(self.auth.seen), 1)

    def test_one_byte_frame_reads_include_mask_and_extended_length_boundaries(self):
        self.opened()
        message = {'value': 'x' * 160}
        wire = frame(message, masked=True)
        output = []
        for value in wire:
            output.extend(self.bridge.feed('frontend', bytes([value])).runtime)
        self.assertEqual(messages(output), [message])

    def test_frame_length_transition_boundaries(self):
        self.opened()
        for length in (125, 126, 65535, 65536):
            # Object text is exactly the requested number of UTF-8 bytes.
            message = {'v': 'x' * (length - 8)}
            payload = json.dumps(message, separators=(',', ':')).encode()
            self.assertEqual(len(payload), length)
            with self.subTest(length=length):
                output = self.bridge.feed('frontend', frame(payload, masked=True))
                self.assertEqual(messages(output.runtime), [message])

    def test_wrong_mask_direction_is_rejected(self):
        for direction, masked in (('frontend', False), ('runtime', True)):
            bridge = self.opened(WebSocketAuthBridge(self.auth))
            with self.subTest(direction=direction):
                self.rejected(direction, frame({'method': 'SENSITIVE'}, masked=masked), bridge)

    def test_reserved_bits_and_reserved_opcodes_are_rejected(self):
        valid = frame({'method': 'SENSITIVE'}, masked=True)
        cases = [bytes([valid[0] | bit]) + valid[1:] for bit in (0x40, 0x20, 0x10)]
        cases += [frame(b'SENSITIVE', opcode=opcode, masked=True) for opcode in (3, 7, 11, 15)]
        for raw in cases:
            bridge = self.opened(WebSocketAuthBridge(self.auth))
            with self.subTest(first=raw[0]):
                self.rejected('frontend', raw, bridge)

    def test_nonminimal_lengths_and_signed_64bit_length_are_rejected(self):
        cases = [b'\x81\xfe\x00\x01' + b'1234' + b'x',
                 b'\x81\xff' + struct.pack('!Q', 126) + b'1234' + b'x' * 126,
                 b'\x81\xff' + struct.pack('!Q', 1 << 63)]
        for raw in cases:
            bridge = self.opened(WebSocketAuthBridge(self.auth))
            with self.subTest(header=raw[:10]):
                self.rejected('frontend', raw, bridge)

    def test_announced_oversized_frame_is_rejected_without_waiting_for_body(self):
        bridge = self.opened(WebSocketAuthBridge(self.auth, max_message_bytes=256))
        self.rejected('frontend', b'\x81\xfe\x01\x01', bridge)

    def test_fragmented_message_has_aggregate_size_limit(self):
        bridge = self.opened(WebSocketAuthBridge(self.auth, max_message_bytes=32))
        bridge.feed('frontend', frame(b'{"value":"' + b'x' * 10, masked=True, fin=False))
        self.rejected('frontend', frame(b'x' * 20 + b'"}', opcode=0, masked=True), bridge)

    def test_aggregate_limit_checks_announced_continuation_before_buffering_body(self):
        bridge = self.opened(WebSocketAuthBridge(self.auth, max_message_bytes=256))
        bridge.feed('frontend', frame(b'{"v":"' + b'x' * 200, masked=True, fin=False))
        self.rejected('frontend', b'\x80\xfe\x00\x80', bridge)

    def test_default_message_limit_matches_the_native_app_server_bound(self):
        # The native SSH client accepts 128 MiB per message; the bridge must not
        # be the smaller transport limit for the same wire messages.
        self.assertEqual(MAX_APP_SERVER_MESSAGE_BYTES, 128 * 1024 * 1024)
        self.assertGreater(MAX_APP_SERVER_MESSAGE_BYTES, 32 * 1024 * 1024)
        self.assertEqual(WebSocketAuthBridge(self.auth).max_message_bytes, MAX_APP_SERVER_MESSAGE_BYTES)
        # A tighter configured bound stays available for tests and deployments.
        self.assertEqual(WebSocketAuthBridge(self.auth, max_message_bytes=1024).max_message_bytes, 1024)
        with self.assertRaises(ValueError):
            WebSocketAuthBridge(self.auth, max_message_bytes=MAX_APP_SERVER_MESSAGE_BYTES + 1)
        with self.assertRaises(ValueError):
            WebSocketAuthBridge(self.auth, max_message_bytes=0)

    def test_configured_message_limit_boundary_is_inclusive(self):
        limit = 1024
        bridge = self.opened(WebSocketAuthBridge(self.auth, max_message_bytes=limit))
        message = {'v': 'x' * (limit - 8)}
        payload = json.dumps(message, separators=(',', ':')).encode()
        self.assertEqual(len(payload), limit)
        self.assertEqual(messages(bridge.feed('frontend', frame(payload, masked=True)).runtime), [message])
        self.assertEqual(bridge.state, 'open')

    def test_announced_size_above_a_limit_is_refused_before_any_body(self):
        # Header-only frames: neither a configured small limit nor the native
        # bound may buffer an announced oversized message.
        cases = ((1024, b'\x81\xff' + struct.pack('!Q', 1025)),
                 (MAX_APP_SERVER_MESSAGE_BYTES,
                  b'\x81\xff' + struct.pack('!Q', MAX_APP_SERVER_MESSAGE_BYTES + 1)))
        for limit, header in cases:
            bridge = self.opened(WebSocketAuthBridge(self.auth, max_message_bytes=limit))
            with self.subTest(limit=limit):
                with self.assertRaises(WebSocketProtocolError):
                    bridge.feed('runtime', header)
                self.assertEqual(bridge.state, 'failed')
                self.assertEqual(bridge.max_message_bytes, limit)
        self.assertEqual(self.auth.seen, [])

    def test_continuation_without_start_and_interleaved_data_are_rejected(self):
        bridge = self.opened(WebSocketAuthBridge(self.auth))
        self.rejected('frontend', frame(b'{}', opcode=0, masked=True), bridge)
        bridge = self.opened(WebSocketAuthBridge(self.auth))
        bridge.feed('frontend', frame(b'{', fin=False, masked=True))
        self.rejected('frontend', frame(b'{}', masked=True), bridge)

    def test_binary_message_cannot_bypass_json_auth_gate(self):
        self.opened()
        self.rejected('frontend', frame(b'{"method":"SENSITIVE"}', opcode=2, masked=True))
        self.assertEqual(self.auth.seen, [])

    def test_malformed_utf8_and_nonobject_json_cannot_bypass_auth(self):
        cases = [b'{"secret":"SENSITIVE\xff"}', b'SENSITIVE-not-json', b'[]', b'null', b'42']
        for payload in cases:
            bridge = self.opened(WebSocketAuthBridge(self.auth))
            with self.subTest(payload=payload[:8]):
                self.rejected('frontend', frame(payload, masked=True), bridge)
        self.assertEqual(self.auth.seen, [])

    def test_unencodable_json_surrogate_has_only_static_error_and_failed_state(self):
        self.opened()
        self.rejected('frontend', frame(b'{"v":"\\ud800SENSITIVE"}', masked=True))

    def test_ping_pong_close_controls_are_forwarded_without_auth_processing(self):
        self.opened()
        for direction, opcode, masked, payload in (
                ('frontend', 9, True, b'hello'), ('runtime', 10, False, b'hello'),
                ('runtime', 9, False, b'world'), ('frontend', 10, True, b'world'),
                ('frontend', 8, True, struct.pack('!H', 1000) + b'normal')):
            raw = frame(payload, opcode=opcode, masked=masked)
            output = self.bridge.feed(direction, raw)
            self.assertEqual(b''.join(getattr(output, 'runtime' if direction == 'frontend' else 'frontend')), raw)
        self.assertEqual(self.auth.seen, [])
        self.assertEqual(self.bridge.state, 'closing')

    def test_invalid_control_frames_are_rejected(self):
        cases = [frame(b'hello', opcode=9, fin=False, masked=True),
                 frame(b'x' * 126, opcode=10, masked=True),
                 frame(b'x', opcode=8, masked=True),
                 frame(struct.pack('!H', 1000) + b'SENSITIVE\xff', opcode=8, masked=True)]
        cases += [frame(struct.pack('!H', code), opcode=8, masked=True)
                  for code in (999, 1004, 1005, 1006, 1015, 5000)]
        for raw in cases:
            bridge = self.opened(WebSocketAuthBridge(self.auth))
            with self.subTest(header=raw[:2]):
                self.rejected('frontend', raw, bridge)

    def test_data_after_close_cannot_reach_auth(self):
        self.opened()
        self.bridge.feed('frontend', frame(b'', opcode=8, masked=True))
        self.rejected('frontend', frame({'method': 'SENSITIVE'}, masked=True))
        self.assertEqual(self.auth.seen, [])

    def test_client_close_preserves_inflight_unmasked_server_response(self):
        self.opened()
        self.bridge.feed('frontend', frame({'id': 1, 'method': 'thread/list'}, masked=True))
        client_close = frame(struct.pack('!H', 1000), opcode=8, masked=True)
        self.assertEqual(b''.join(self.bridge.feed('frontend', client_close).runtime), client_close)
        response = {'id': 1, 'result': {'data': []}}
        output = self.bridge.feed('runtime', frame(response))
        self.assertEqual(messages(output.frontend), [response])
        self.assertFalse(decode_frames(output.frontend)[0]['masked'])
        self.assertEqual(output.runtime, [])
        self.assertEqual(self.bridge.state, 'closing')
        server_close = frame(struct.pack('!H', 1000), opcode=8)
        self.assertEqual(b''.join(self.bridge.feed('runtime', server_close).frontend), server_close)

    def test_server_close_preserves_inflight_masked_client_data_until_client_close(self):
        self.opened()
        server_close = frame(struct.pack('!H', 1000), opcode=8)
        self.assertEqual(b''.join(self.bridge.feed('runtime', server_close).frontend), server_close)
        request = {'id': 2, 'method': 'thread/list'}
        output = self.bridge.feed('frontend', frame(request, masked=True))
        self.assertEqual(messages(output.runtime), [request])
        self.assertTrue(decode_frames(output.runtime)[0]['masked'])
        self.assertEqual(output.frontend, [])
        client_close = frame(struct.pack('!H', 1000), opcode=8, masked=True)
        self.assertEqual(b''.join(self.bridge.feed('frontend', client_close).runtime), client_close)
        self.rejected('frontend', frame({'id': 3, 'method': 'SENSITIVE'}, masked=True))

    def test_truncated_http_and_frame_eof_fail_closed(self):
        cases = [('handshake', b'GET / HTTP/1.1\r\n'),
                 ('frame', b'\x81'), ('frame', b'\x81\xfe\x01'),
                 ('frame', b'\x81\x85ab'), ('frame', frame(b'hello', masked=True)[:-1])]
        for stage, raw in cases:
            bridge = WebSocketAuthBridge(self.auth)
            if stage == 'frame':
                self.opened(bridge)
            bridge.feed('frontend', raw)
            with self.subTest(stage=stage, raw=raw):
                with self.assertRaises(WebSocketProtocolError):
                    bridge.eof('frontend')
                self.assertEqual(bridge.state, 'failed')

    def test_unfinished_fragment_eof_fails_closed(self):
        self.opened()
        self.bridge.feed('frontend', frame(b'{', fin=False, masked=True))
        with self.assertRaises(WebSocketProtocolError):
            self.bridge.eof('frontend')

    def test_frontend_half_close_preserves_pending_runtime_responses(self):
        self.opened()
        request = {'id': 1, 'method': 'thread/list'}
        self.bridge.feed('frontend', frame(request, masked=True))
        self.bridge.eof('frontend')
        self.assertEqual(self.bridge.state, 'open')
        response = {'id': 1, 'result': {'data': []}}
        output = self.bridge.feed('runtime', frame(response))
        self.assertEqual(messages(output.frontend), [response])
        self.bridge.eof('runtime')
        self.assertEqual(self.bridge.state, 'closing')

    def test_input_on_an_already_ended_direction_fails_closed(self):
        self.opened()
        self.bridge.eof('frontend')
        self.rejected('frontend', frame({'method': 'SENSITIVE'}, masked=True))


class WebSocketAuthIntegrationTests(unittest.TestCase):
    # Keep the framing contract tests above independent of AuthProxy internals.
    # This subclass only supplies a real synthetic auth end-to-end scenario.
    def synthetic(self):
        now = 1_800_000_000
        claims = {'exp': now + 3600, 'https://api.openai.com/auth': {'chatgpt_account_id': 'fixture-account'}}
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
        token = 'fixture.' + payload + '.SENSITIVE-fixture-only'
        loaded = []
        def loader():
            loaded.append(True)
            return AuthTokens(token, 'fixture-account')
        auth = AuthProxy(token_loader=loader, clock=lambda: now)
        bridge = WebSocketFixtureTests.opened(self, WebSocketAuthBridge(auth))
        return auth, bridge, token, loaded

    def test_initialize_auth_gate_and_masked_login_with_synthetic_token(self):
        auth, bridge, token, loaded = self.synthetic()
        request = {'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'fixture', 'version': '1'}}}
        output = bridge.feed('frontend', frame(request, masked=True))
        self.assertTrue(messages(output.runtime)[0]['params']['capabilities']['experimentalApi'])
        self.assertEqual(loaded, [])
        output = bridge.feed('runtime', frame({'id': 1, 'result': {'userAgent': 'fixture'}}))
        login = messages(output.runtime)[0]
        queued = {'id': 2, 'method': 'thread/list'}
        self.assertEqual(bridge.feed('frontend', frame(queued, masked=True)).runtime, [])
        self.assertEqual(login['method'], 'account/login/start')
        self.assertEqual(login['params']['accessToken'], token)
        self.assertTrue(all(item['masked'] for item in decode_frames(output.runtime)))
        self.assertEqual(len(loaded), 1)
        self.assertNotIn(token, json.dumps(output.events))
        self.assertNotIn(token.encode(), b''.join(output.frontend))
        # Native desktop also uses this transport without an initialized notice.
        config = {'id': 3, 'method': 'configRequirements/read'}
        self.assertEqual(bridge.feed('frontend', frame(config, masked=True)).runtime, [])
        output = bridge.feed('runtime', frame({'id': login['id'], 'result': {'type': 'chatgptAuthTokens'}}))
        self.assertEqual(output.frontend, [])
        self.assertEqual(messages(output.runtime), [queued, config])
        self.assertNotIn(token, json.dumps(output.events))
        self.assertEqual(auth.state, 'ready')

    def test_fragmented_internal_auth_rejection_never_reaches_frontend(self):
        auth, bridge, token, loaded = self.synthetic()
        bridge.feed('frontend', frame({'id': 1, 'method': 'initialize'}, masked=True))
        output = bridge.feed('runtime', frame({'id': 1, 'result': {}}))
        login = messages(output.runtime)[0]
        self.assertEqual(messages(bridge.feed('frontend', frame({'method': 'initialized'}, masked=True)).runtime), [{'method': 'initialized'}])
        bridge.feed('frontend', frame({'id': 2, 'method': 'turn/start'}, masked=True))
        payload = json.dumps({'id': login['id'], 'error': {'code': -1, 'message': token,
                                                         'data': {'accessToken': token}}}).encode()
        output = bridge.feed('runtime', frame(payload[:31], fin=False))
        self.assertEqual(output.frontend, [])
        ping = frame(b'ping-during-auth', opcode=9)
        output = bridge.feed('runtime', ping)
        self.assertEqual(b''.join(output.frontend), ping)
        output = bridge.feed('runtime', frame(payload[31:], opcode=0))
        response = messages(output.frontend)
        self.assertEqual(len(response), 1)
        self.assertEqual(response[0]['id'], 2)
        self.assertEqual(response[0]['error']['data']['status'], 'login_needed')
        self.assertEqual(output.runtime, [])
        self.assertNotIn(token, json.dumps(response + output.events))
        self.assertNotIn('accessToken', json.dumps(response + output.events))
        self.assertEqual(auth.state, 'login_needed')

    def test_internal_auth_refresh_injection_is_suppressed_after_client_close(self):
        auth, bridge, token, loaded = self.synthetic()
        bridge.feed('frontend', frame({'id': 1, 'method': 'initialize'}, masked=True))
        output = bridge.feed('runtime', frame({'id': 1, 'result': {}}))
        login = messages(output.runtime)[0]
        bridge.feed('runtime', frame({'id': login['id'], 'result': {'type': 'chatgptAuthTokens'}}))
        self.assertEqual(auth.state, 'ready')
        self.assertEqual(len(loaded), 1)
        bridge.feed('frontend', frame(struct.pack('!H', 1000), opcode=8, masked=True))
        output = bridge.feed('runtime', frame({'id': 12, 'method': 'account/chatgptAuthTokens/refresh',
                                              'params': {'previousAccountId': 'fixture-account'}}))
        self.assertEqual(len(loaded), 2)
        self.assertEqual(output.runtime, [])
        self.assertEqual(output.frontend, [])
        self.assertNotIn(token, json.dumps(output.events))
        self.assertNotIn('accessToken', json.dumps(output.events))
        self.assertEqual(bridge.state, 'closing')

    def ready(self):
        """Drive the synthetic login to ready; the token never reaches the frontend."""
        auth, bridge, token, loaded = self.synthetic()
        bridge.feed('frontend', frame({'id': 1, 'method': 'initialize',
            'params': {'clientInfo': {'name': 'fixture', 'version': '1'}}}, masked=True))
        output = bridge.feed('runtime', frame({'id': 1, 'result': {'userAgent': 'fixture'}}))
        login = messages(output.runtime)[0]
        self.assertEqual(login['method'], 'account/login/start')
        self.assertEqual(login['params']['accessToken'], token)
        self.assertEqual(messages(output.frontend), [{'id': 1, 'result': {'userAgent': 'fixture'}}])
        output = bridge.feed('runtime', frame({'id': login['id'], 'result': {'type': 'chatgptAuthTokens'}}))
        self.assertEqual(output.frontend, [])
        self.assertEqual(output.runtime, [])
        self.assertEqual(auth.state, 'ready')
        self.assertEqual(loaded, [True])
        return auth, bridge, token, loaded

    @staticmethod
    def app_server_line(size):
        """One compact JSON line of exactly ``size`` ASCII bytes."""
        prefix, suffix = b'{"id":9,"result":{"data":"', b'"}}'
        return prefix + b'x' * (size - len(prefix) - len(suffix)) + suffix

    def forwarded(self, chunks):
        """The single unmasked output frame payload, without copying it."""
        self.assertEqual(len(chunks), 1)
        chunk = chunks[0]
        self.assertEqual(chunk[1] & 0x7f, 127)
        self.assertFalse(chunk[1] & 0x80)
        length = struct.unpack('!Q', chunk[2:10])[0]
        self.assertEqual(len(chunk), 10 + length)
        return memoryview(chunk)[10:]

    def test_runtime_response_above_the_old_32mib_limit_is_forwarded(self):
        auth, bridge, token, loaded = self.ready()
        payload = self.app_server_line(33 * 1024 * 1024 + 1)
        self.assertGreater(len(payload), 32 * 1024 * 1024)
        digest = hashlib.sha256(payload).digest()
        # Split across two fragments: the cumulative bound follows the native
        # limit, so a >32 MiB message no longer fails the old bridge default.
        split = 16 * 1024 * 1024
        self.assertEqual(bridge.feed('runtime', frame(payload[:split], fin=False)).frontend, [])
        output = bridge.feed('runtime', frame(payload[split:], opcode=0))
        forwarded = self.forwarded(output.frontend)
        self.assertEqual(len(forwarded), len(payload))
        self.assertEqual(hashlib.sha256(forwarded).digest(), digest)
        self.assertEqual(output.runtime, [])
        self.assertEqual(bridge.state, 'open')
        self.assertEqual(auth.state, 'ready')
        # Login filtering still owns the transport after the large message: the
        # refreshed token goes only to the runtime and never to the frontend.
        refresh = bridge.feed('runtime', frame({'id': 12, 'method': 'account/chatgptAuthTokens/refresh',
                                               'params': {'previousAccountId': 'fixture-account'}}))
        self.assertEqual(refresh.frontend, [])
        self.assertEqual(messages(refresh.runtime)[0]['result']['accessToken'], token)
        self.assertNotIn(token, json.dumps(output.events))
        self.assertNotIn(token, json.dumps(refresh.events))

    def test_actual_65_920_144_byte_json_line_is_forwarded_unchanged(self):
        # The largest selected SSH thread JSONL line measured in production.
        auth, bridge, token, loaded = self.ready()
        size = 65_920_144
        payload = self.app_server_line(size)
        self.assertEqual(len(payload), size)
        self.assertLess(size, MAX_APP_SERVER_MESSAGE_BYTES)
        output = bridge.feed('runtime', frame(payload))
        forwarded = self.forwarded(output.frontend)
        self.assertEqual(len(forwarded), size)
        self.assertEqual(hashlib.sha256(forwarded).digest(), hashlib.sha256(payload).digest())
        self.assertEqual(output.runtime, [])
        self.assertEqual(bridge.state, 'open')
        self.assertEqual(auth.state, 'ready')
        self.assertEqual(loaded, [True])


if __name__ == '__main__':
    unittest.main()
