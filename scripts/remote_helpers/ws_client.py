"""Bounded WebSocket client over binary streams, without a public TCP port.

Used by headless checks and the private Unix maintenance connection. It never logs frames; JSON messages can
contain authentication credentials. The server must decline extensions and use
unmasked frames, while every client frame is masked as required by RFC 6455.
"""
import base64
import hashlib
import json
import os
import queue
import struct
import threading
import time


GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocketPipe:
    def __init__(self, reader, writer, *, max_message=8 * 1024 * 1024):
        self.reader = reader
        self.writer = writer
        self.max_message = max_message
        self._queue = queue.Queue(maxsize=128)
        self._buffer = bytearray()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._fragment = bytearray()
        self._fragment_type = None
        self._write_lock = threading.Lock()

    def _read_loop(self):
        try:
            method = getattr(self.reader, "read1", self.reader.read)
            while chunk := method(65536):
                self._queue.put(chunk)
        finally:
            self._queue.put(None)

    def _exact(self, count, deadline):
        while len(self._buffer) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("WebSocket read timed out")
            try:
                chunk = self._queue.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError("WebSocket read timed out") from None
            if chunk is None:
                raise EOFError("SSH WebSocket closed")
            self._buffer.extend(chunk)
        result = bytes(self._buffer[:count])
        del self._buffer[:count]
        return result

    def handshake(self, timeout=30):
        key = base64.b64encode(os.urandom(16)).decode()
        request = ("GET /rpc HTTP/1.1\r\nHost: codex-app-server\r\nUpgrade: websocket\r\n"
                   "Connection: Upgrade\r\nSec-WebSocket-Key: " + key + "\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.writer.write(request.encode("ascii"))
        self.writer.flush()
        deadline = time.monotonic() + timeout
        headers = bytearray()
        while not headers.endswith(b"\r\n\r\n"):
            if len(headers) >= 16384:
                raise ValueError("WebSocket upgrade headers too large")
            headers.extend(self._exact(1, deadline))
        lines = bytes(headers).decode("ascii").split("\r\n")
        if not lines[0].startswith("HTTP/1.1 101 "):
            raise ValueError("WebSocket upgrade rejected")
        fields = {}
        for line in lines[1:]:
            if line:
                name, value = line.split(":", 1)
                name = name.strip().lower()
                if name in fields:
                    raise ValueError("Duplicate WebSocket upgrade header")
                fields[name] = value.strip()
        expected = base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode()
        if (fields.get("sec-websocket-accept") != expected or fields.get("upgrade", "").lower() != "websocket"
                or "upgrade" not in [token.strip() for token in fields.get("connection", "").lower().split(",")]
                or "sec-websocket-extensions" in fields):
            raise ValueError("Invalid WebSocket upgrade response")

    def send_frame(self, opcode, payload=b""):
        if len(payload) > self.max_message or (opcode >= 8 and len(payload) > 125):
            raise ValueError("WebSocket outgoing frame too large")
        mask = os.urandom(4)
        size = len(payload)
        header = bytes((0x80 | opcode, 0x80 | size)) if size < 126 else (
            bytes((0x80 | opcode, 0x80 | 126)) + struct.pack("!H", size) if size < 65536 else
            bytes((0x80 | opcode, 0x80 | 127)) + struct.pack("!Q", size))
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        with self._write_lock:
            self.writer.write(header + mask + masked)
            self.writer.flush()

    def send_json(self, message):
        self.send_frame(1, json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    def receive_json(self, timeout=30):
        deadline = time.monotonic() + timeout
        while True:
            first, second = self._exact(2, deadline)
            if first & 0x70 or second & 0x80:
                raise ValueError("Unsupported WebSocket RSV or server mask")
            final, opcode, size = bool(first & 0x80), first & 15, second & 127
            if size == 126:
                size = struct.unpack("!H", self._exact(2, deadline))[0]
            elif size == 127:
                size = struct.unpack("!Q", self._exact(8, deadline))[0]
            if size > self.max_message or (opcode >= 8 and (not final or size > 125)):
                raise ValueError("Invalid WebSocket frame length")
            payload = self._exact(size, deadline)
            if opcode == 8:
                raise EOFError("WebSocket closed")
            if opcode == 9:
                self.send_frame(10, payload)
                continue
            if opcode == 10:
                continue
            if opcode == 1:
                if self._fragment_type is not None:
                    raise ValueError("Overlapping WebSocket messages")
                self._fragment_type = 1
            elif opcode != 0 or self._fragment_type is None:
                raise ValueError("Only WebSocket text messages are supported")
            self._fragment.extend(payload)
            if len(self._fragment) > self.max_message:
                raise ValueError("WebSocket message too large")
            if final:
                data = json.loads(self._fragment.decode("utf-8"))
                self._fragment.clear()
                self._fragment_type = None
                if not isinstance(data, dict):
                    raise ValueError("Expected one JSON-RPC object")
                return data
