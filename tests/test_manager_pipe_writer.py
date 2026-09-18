import io
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.pipe_writer import PipeWriter


class RetainedBytes(io.BytesIO):
    def close(self): pass


class PipeWriterTests(unittest.TestCase):
    def test_blocked_writer_keeps_admission_bounded_and_drops_expired_admin(self):
        entered, release = threading.Event(), threading.Event()
        class Slow(RetainedBytes):
            def write(self, body):
                entered.set()
                if not release.wait(5): raise OSError('fixture timed out')
                return super().write(body)
        stream = Slow()
        failures = []
        writer = PipeWriter(stream, lambda: failures.append(True), limit=10)
        try:
            writer.write(b'first')
            self.assertTrue(entered.wait(2))
            writer.write(b'late', lambda: False)
            self.assertEqual(writer.snapshot()['pending_bytes'], 9)
            with self.assertRaises(OSError): writer.write(b'overflow')
            writer.close()
            release.set()
            writer.thread.join(2)
            self.assertFalse(writer.thread.is_alive())
            self.assertEqual(stream.getvalue(), b'first')
            self.assertFalse(failures)
        finally:
            release.set(); writer.close(); writer.thread.join(2)
