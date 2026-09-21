"""Bounded RFC 6455 JSON-message adapter for account-bound SSH transports.

Input starts at the HTTP upgrade, after the native SSH synchronization marker.
The caller preserves that marker and serializes feed/poll/eof calls. No socket,
credential storage, or logging belongs to this module. Compression is refused:
the native Codex SSH client already requests perMessageDeflate=false.

One app-server message may legitimately reach the native client's own bound:
the Rust SSH client accepts 128 MiB per message (remote.rs ``128 << 20``), so
this bridge must not impose a smaller transport limit on the same wire
messages. A smaller bound may still be configured for tighter deployments.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import json
import os
import struct


# Single source of truth for the largest app-server message this transport
# accepts, matching the native SSH client. Never raise this bound: a larger
# frame is refused by the native side anyway and only wastes bridge memory.
MAX_APP_SERVER_MESSAGE_BYTES = 128 * 1024 * 1024


class WebSocketProtocolError(ValueError):
    """Static protocol diagnostics; never include data received from either peer."""


@dataclass
class BridgeResult:
    runtime: list[bytes] = field(default_factory=list, repr=False)
    frontend: list[bytes] = field(default_factory=list, repr=False)
    events: list[dict] = field(default_factory=list)


def encode_frame(payload: bytes, opcode=1, masked=False, fin=True, mask_key=None) -> bytes:
    size = len(payload)
    first = (0x80 if fin else 0) | opcode
    mask_bit = 0x80 if masked else 0
    if size < 126:
        header = bytes((first, mask_bit | size))
    elif size <= 65535:
        header = bytes((first, mask_bit | 126)) + struct.pack('!H', size)
    else:
        header = bytes((first, mask_bit | 127)) + struct.pack('!Q', size)
    if not masked:
        return header + payload
    key = os.urandom(4) if mask_key is None else mask_key
    if not isinstance(key, bytes) or len(key) != 4:
        raise ValueError('A WebSocket mask must contain four bytes.')
    return header + key + bytes(value ^ key[index % 4] for index, value in enumerate(payload))


def _tokens(value):
    return {part.strip().casefold() for part in value.split(',') if part.strip()}


def _headers(raw):
    try:
        lines = raw[:-4].decode('latin-1').split('\r\n')
        values = {}
        for line in lines[1:]:
            if not line or line[0].isspace() or ':' not in line:
                raise ValueError()
            name, value = line.split(':', 1)
            if not name or any(not (char.isascii() and (char.isalnum() or char in "!#$%&'*+-.^_`|~")) for char in name):
                raise ValueError()
            name = name.casefold()
            value = value.strip(' \t')
            if any(ord(char) < 32 and char != '\t' for char in value) or '\x7f' in value:
                raise ValueError()
            if name in values:
                raise ValueError()
            values[name] = value
        return lines[0], values
    except (ValueError, IndexError, UnicodeError):
        raise WebSocketProtocolError('invalid_http_headers') from None


class WebSocketAuthBridge:
    def __init__(self, auth, *, max_message_bytes=MAX_APP_SERVER_MESSAGE_BYTES,
                 max_header_bytes=32768, mask_factory=os.urandom):
        if (not isinstance(max_message_bytes, int) or isinstance(max_message_bytes, bool)
                or not isinstance(max_header_bytes, int) or isinstance(max_header_bytes, bool)
                or not 1 <= max_message_bytes <= MAX_APP_SERVER_MESSAGE_BYTES
                or max_header_bytes < 128):
            raise ValueError('Invalid WebSocket buffer limit.')
        self.auth = auth
        self.max_message_bytes = max_message_bytes
        self.max_header_bytes = max_header_bytes
        self.mask_factory = mask_factory
        self.state = 'handshake'
        self._buffers = {'frontend': bytearray(), 'runtime': bytearray()}
        self._http_done = {'frontend': False, 'runtime': False}
        self._fragment = {'frontend': None, 'runtime': None}
        self._closed = set()
        self._ended = set()
        self._accept = None

    def _fail(self, reason):
        self.state = 'failed'
        for value in self._buffers.values():
            value.clear()
        self._fragment = {'frontend': None, 'runtime': None}
        raise WebSocketProtocolError(reason)

    def _ensure(self, direction):
        if direction not in self._buffers:
            raise ValueError('WebSocket direction must be frontend or runtime.')
        if self.state == 'failed':
            raise WebSocketProtocolError('transport_already_failed')
        if direction in self._ended:
            self._fail('data_after_transport_eof')

    def feed(self, direction, data):
        self._ensure(direction)
        if not isinstance(data, bytes):
            raise TypeError('WebSocket input must be bytes.')
        result = BridgeResult()
        buffer = self._buffers[direction]
        # Callers read bounded chunks (normally 64KiB). Refuse unbounded batches
        # before copying their contents into the parser's own storage.
        if len(buffer) + len(data) > max(self.max_message_bytes + 14, self.max_header_bytes) + 65536:
            self._fail('websocket_buffer_limit')
        buffer.extend(data)
        if not self._http_done[direction]:
            boundary = buffer.find(b'\r\n\r\n')
            if boundary < 0:
                if len(buffer) > self.max_header_bytes:
                    self._fail('http_header_limit')
                return result
            boundary += 4
            if boundary > self.max_header_bytes:
                self._fail('http_header_limit')
            raw = bytes(buffer[:boundary])
            del buffer[:boundary]
            self._upgrade(direction, raw)
            self._http_done[direction] = True
            getattr(result, 'runtime' if direction == 'frontend' else 'frontend').append(raw)
        if all(self._http_done.values()):
            if self.state == 'handshake':
                self.state = 'open'
            # A client may coalesce its first frame with its request. Do not
            # release application messages until the server accepted the upgrade.
            self._drain('frontend', result)
            self._drain('runtime', result)
        return result

    def _upgrade(self, direction, raw):
        try:
            first, headers = _headers(raw)
        except WebSocketProtocolError:
            self._fail('invalid_http_headers')
        if 'websocket' not in _tokens(headers.get('upgrade', '')) or 'upgrade' not in _tokens(headers.get('connection', '')):
            self._fail('websocket_upgrade_required')
        if headers.get('sec-websocket-extensions') or headers.get('sec-websocket-protocol'):
            self._fail('unsupported_websocket_negotiation')
        if headers.get('transfer-encoding') or headers.get('content-length', '0') != '0':
            self._fail('http_upgrade_body_not_supported')
        parts = first.split(' ')
        if direction == 'frontend':
            if len(parts) != 3 or parts[0] != 'GET' or parts[2] != 'HTTP/1.1' or not parts[1].startswith('/') or not headers.get('host'):
                self._fail('invalid_websocket_request')
            if headers.get('sec-websocket-version') != '13':
                self._fail('unsupported_websocket_version')
            key = headers.get('sec-websocket-key', '')
            try:
                if len(base64.b64decode(key, validate=True)) != 16:
                    raise ValueError()
            except (ValueError, UnicodeError):
                self._fail('invalid_websocket_key')
            self._accept = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode('ascii')).digest()).decode('ascii')
        else:
            if len(parts) < 2 or parts[:2] != ['HTTP/1.1', '101'] or self._accept is None:
                self._fail('websocket_upgrade_rejected')
            if headers.get('sec-websocket-accept') != self._accept:
                self._fail('websocket_accept_mismatch')

    def _drain(self, direction, result):
        buffer = self._buffers[direction]
        while len(buffer) >= 2:
            first, second = buffer[0], buffer[1]
            fin, opcode, masked = bool(first & 0x80), first & 0x0F, bool(second & 0x80)
            if first & 0x70:
                self._fail('websocket_reserved_bits')
            if opcode not in (0, 1, 2, 8, 9, 10):
                self._fail('websocket_reserved_opcode')
            if masked != (direction == 'frontend'):
                self._fail('websocket_mask_direction')
            marker, size, position = second & 0x7F, second & 0x7F, 2
            if marker == 126:
                if len(buffer) < 4:
                    return
                size, position = struct.unpack('!H', buffer[2:4])[0], 4
                if size < 126:
                    self._fail('websocket_nonminimal_length')
            elif marker == 127:
                if len(buffer) < 10:
                    return
                size, position = struct.unpack('!Q', buffer[2:10])[0], 10
                if size < 65536 or size >= 1 << 63:
                    self._fail('websocket_invalid_long_length')
            if opcode >= 8 and (not fin or size > 125):
                self._fail('websocket_invalid_control_frame')
            if size > self.max_message_bytes:
                self._fail('websocket_message_limit')
            fragment = self._fragment[direction]
            if opcode == 0 and fragment is not None and len(fragment) + size > self.max_message_bytes:
                self._fail('websocket_message_limit')
            if masked:
                position += 4
            end = position + size
            if len(buffer) < end:
                return
            if opcode >= 8:
                # Control frames are at most 125 bytes and stay byte-exact on
                # the wire, including the client's own mask.
                raw = bytes(buffer[:end])
                del buffer[:end]
                payload = raw[position:]
                if masked:
                    key = raw[position - 4:position]
                    payload = bytes(value ^ key[index % 4] for index, value in enumerate(payload))
                if opcode == 8:
                    self._close_payload(payload)
                    self._closed.add(direction)
                    self.state = 'closing'
                getattr(result, 'runtime' if direction == 'frontend' else 'frontend').append(raw)
                continue
            # A data message may be tens of MiB: unmask into one payload copy
            # and release the wire bytes instead of retaining a second copy.
            key = bytes(buffer[position - 4:position]) if masked else None
            payload = bytes(buffer[position:end])
            del buffer[:end]
            if masked:
                payload = bytes(value ^ key[index % 4] for index, value in enumerate(payload))
            # Opposite-direction data may already be in flight when one peer
            # starts the close handshake. Only data after that sender's own
            # Close frame violates the wire protocol.
            if direction in self._closed:
                self._fail('websocket_data_after_close')
            if opcode == 2:
                self._fail('binary_app_server_message_not_supported')
            fragment = self._fragment[direction]
            if opcode == 0:
                if fragment is None:
                    self._fail('websocket_unexpected_continuation')
                if len(fragment) + len(payload) > self.max_message_bytes:
                    self._fail('websocket_message_limit')
                fragment.extend(payload)
                if fin:
                    self._fragment[direction] = None
                    self._message(direction, bytes(fragment), result)
            elif fragment is not None:
                self._fail('websocket_interleaved_data_message')
            elif fin:
                self._message(direction, payload, result)
            else:
                self._fragment[direction] = bytearray(payload)

    def _close_payload(self, payload):
        if len(payload) == 1:
            self._fail('websocket_invalid_close_payload')
        if payload:
            code = struct.unpack('!H', payload[:2])[0]
            if code not in (1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011, 1012, 1013, 1014) and not 3000 <= code <= 4999:
                self._fail('websocket_invalid_close_code')
            try:
                payload[2:].decode('utf-8')
            except UnicodeError:
                self._fail('websocket_invalid_close_reason')

    def _message(self, direction, payload, result):
        try:
            message = json.loads(payload.decode('utf-8'), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(message, dict):
                raise ValueError()
        except (ValueError, UnicodeError, RecursionError):
            self._fail('invalid_app_server_json')
        outcome = self.auth.process(direction, message)
        self._outcome(outcome, result)

    def _outcome(self, outcome, result):
        result.events.extend(outcome.events)
        for destination in ('runtime', 'frontend'):
            sender = 'frontend' if destination == 'runtime' else 'runtime'
            if sender in self._closed:
                # Never inject new authentication/application frames after we
                # already forwarded this sender's Close frame.
                continue
            for message in getattr(outcome, destination):
                try:
                    body = json.dumps(message, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')
                except (ValueError, UnicodeError, TypeError, RecursionError):
                    self._fail('invalid_outgoing_app_server_json')
                if len(body) > self.max_message_bytes:
                    self._fail('websocket_outgoing_message_limit')
                masked = destination == 'runtime'
                frame = encode_frame(body, masked=masked,
                                     mask_key=self.mask_factory(4) if masked else None)
                getattr(result, destination).append(frame)

    def poll(self):
        result = BridgeResult()
        if self.state == 'open':
            self._outcome(self.auth.poll(), result)
        return result

    def eof(self, direction):
        self._ensure(direction)
        if self._buffers[direction] or self._fragment[direction] is not None:
            self._fail('truncated_websocket_stream')
        if not self._http_done[direction]:
            self._fail('truncated_http_upgrade')
        self._ended.add(direction)
        # A byte-stream half-close is not a WebSocket Close frame. Preserve
        # already outstanding responses arriving on the opposite direction.
        if len(self._ended) == 2:
            self.state = 'closing'
        return BridgeResult()
