"""Ordered, bounded pipe output; the protocol lock never waits for a reader."""
from collections import deque
import threading
import time


class PipeWriter:
    def __init__(self, stream, failed, *, limit=64 * 1024 * 1024):
        self.stream, self.failed, self.limit = stream, failed, limit
        self.condition = threading.Condition()
        self.queue = deque()
        self.bytes = 0
        self.error = None
        self.closed = False
        self.max_write_ms = 0
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def write(self, body, before_write=None):
        with self.condition:
            if self.error or self.closed:
                raise OSError('Runtime pipe is closed.')
            if self.bytes + len(body) > self.limit:
                raise OSError('Runtime pipe output backlog exceeds its bound.')
            self.bytes += len(body)
            self.queue.append((body, before_write))
            self.condition.notify()

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify()

    def snapshot(self):
        with self.condition:
            return dict(pending_bytes=self.bytes, max_write_ms=round(self.max_write_ms, 1),
                        failed=self.error is not None)

    def _run(self):
        try:
            while True:
                with self.condition:
                    self.condition.wait_for(lambda: self.queue or self.closed)
                    if not self.queue: break
                    body, before_write = self.queue.popleft()
                start = time.monotonic()
                if before_write is None or before_write():
                    self.stream.write(body)
                    self.stream.flush()
                with self.condition:
                    self.bytes -= len(body)
                    self.max_write_ms = max(self.max_write_ms, (time.monotonic() - start) * 1000)
        except (OSError, ValueError) as error:
            with self.condition:
                self.error = error
                self.queue.clear()
                self.bytes = 0
            self.failed()
        finally:
            try: self.stream.close()
            except (OSError, ValueError): pass
