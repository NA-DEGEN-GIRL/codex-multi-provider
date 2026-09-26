"""Digest caches stay separate, and content stamps reuse one Windows prototype."""
from collections import OrderedDict
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core import desktop_publication as publication


class DigestCacheTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.files = [self.root / name for name in ('a.bin', 'b.bin', 'c.bin')]
        for path in self.files:
            path.write_bytes(path.name.encode())
        publication._hashes.clear()
        self.addCleanup(publication._hashes.clear)

    def settled(self):
        return patch.object(publication, '_now_ns', side_effect=lambda: time.time_ns() + 2_000_000_000)

    def test_private_cache_is_bounded_and_never_evicts_shared_digests(self):
        own = OrderedDict()
        with self.settled():
            publication._hash(self.files[0])
            shared = list(publication._hashes)
            for path in self.files:
                publication._hash(path, own, 2)
        self.assertEqual(list(publication._hashes), shared)
        self.assertEqual([key[0] for key in own], [str(path.absolute()) for path in self.files[1:]])

    @unittest.skipUnless(os.name == 'nt', 'NTFS ChangeTime is Windows-only')
    def test_change_time_detects_restored_mtime_without_per_call_prototypes(self):
        path = self.files[0]
        with patch.object(publication.ctypes, 'WinDLL', side_effect=AssertionError('per-call prototype')):
            with path.open('rb') as stream:
                first = publication._content_stamp(stream)
            before = path.stat()
            time.sleep(.05)  # a later clock tick
            path.write_bytes(b'x' * before.st_size)
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            with path.open('rb') as stream:
                second = publication._content_stamp(stream)
        self.assertEqual(first[:4], second[:4])
        self.assertNotEqual(first[4], second[4])


if __name__ == '__main__':
    unittest.main()
