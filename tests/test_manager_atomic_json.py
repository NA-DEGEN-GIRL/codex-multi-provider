"""Real Windows reader handles must not turn status polling into write failure."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from manager_core.store import atomic_json, Store


@unittest.skipUnless(os.name == 'nt', 'Windows file sharing semantics')
class WindowsPublicationTests(unittest.TestCase):
    def test_concurrent_state_readers_and_writers_never_see_partial_or_denied_state(self):
        store = Store(self.path.parent)
        store.add_profile('fixture')
        def read_many():
            for _ in range(120):
                value = store.read()
                self.assertEqual(value['profiles'][0]['alias'], 'fixture')
        def write_many():
            for index in range(80):
                store.mutate(lambda data: data.update(counter=index))
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(read_many) for _ in range(4)] + [pool.submit(write_many)]
            for future in futures: future.result(timeout=30)
        self.assertEqual(store.read()['counter'], 79)

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'status.json'
        atomic_json(self.path, {'version': 1})

    def test_current_reader_finishes_old_snapshot_then_writer_publishes_new_snapshot(self):
        reader = self.path.open('rb')
        denied = threading.Event()
        replace = os.replace
        def observed_replace(source, target):
            try:
                return replace(source, target)
            except PermissionError:
                denied.set()
                raise
        try:
            with ThreadPoolExecutor(max_workers=1) as pool, patch('manager_core.store.os.replace', observed_replace):
                future = pool.submit(atomic_json, self.path, {'version': 2})
                try:
                    self.assertTrue(denied.wait(3), 'The real reader must deny deletion before retry')
                    self.assertEqual(json.load(reader), {'version': 1})
                finally:
                    reader.close()
                future.result(timeout=3)
        finally:
            reader.close()
        self.assertEqual(json.loads(self.path.read_text()), {'version': 2})
        self.assertFalse(list(self.path.parent.glob('*.tmp')))

    def test_persistent_reader_lock_has_bounded_failure_and_preserves_old_document(self):
        with self.path.open('rb') as reader:
            with self.assertRaises(PermissionError):
                atomic_json(self.path, {'version': 2})
            self.assertEqual(json.load(reader), {'version': 1})
        self.assertEqual(json.loads(self.path.read_text()), {'version': 1})
        self.assertFalse(list(self.path.parent.glob('*.tmp')))
