import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core import desktop_publication as publication


class DesktopValidationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.file = self.root / 'app.bin'
        self.file.write_bytes(b'original')
        publication._hashes.clear()

    def stamp(self, path=None):
        with (path or self.file).open('rb') as stream:
            return publication._content_stamp(stream)

    def cache_clock(self, seconds, path=None):
        """Freeze the cache clock relative to the file's content stamp."""
        changed = publication._change_time_ns(self.stamp(path))
        self.assertIsNotNone(changed, 'content stamp unavailable')
        return patch.object(publication, '_now_ns',
                            return_value=changed + int(seconds * 1_000_000_000))

    def later_stamp(self, stamp=None, seconds=5):
        """A later write's stamp, in the platform units of the stamp field."""
        ticks = int(seconds * 1_000_000_000)
        stamp = self.stamp() if stamp is None else stamp
        return stamp[:4] + (stamp[4] + (ticks // 100 if os.name == 'nt' else ticks),)

    def test_unchanged_content_is_hashed_once_but_modification_invalidates(self):
        with self.cache_clock(2), \
             patch.object(publication.hashlib, 'file_digest', wraps=hashlib.file_digest) as digest:
            first = publication._hash(self.file)
            self.assertEqual(publication._hash(self.file), first)
            self.assertEqual(digest.call_count, 1)
        original = self.file.stat()
        self.file.write_bytes(b'modified')  # Same size, with mtime restored.
        os.utime(self.file, ns=(original.st_atime_ns, original.st_mtime_ns))
        with patch.object(publication, '_content_stamp', return_value=self.later_stamp()), \
             patch.object(publication.hashlib, 'file_digest', wraps=hashlib.file_digest) as digest:
            self.assertNotEqual(publication._hash(self.file), first)
            self.assertEqual(digest.call_count, 1)

    def test_settled_stamp_is_cached_and_reused(self):
        with self.cache_clock(2), \
             patch.object(publication.hashlib, 'file_digest', wraps=hashlib.file_digest) as digest:
            first = publication._hash(self.file)
            self.assertEqual(publication._hash(self.file), first)
            self.assertEqual(digest.call_count, 1)
            self.assertEqual(len(publication._hashes), 1)

    def test_fresh_or_future_stamps_are_never_cached_or_reused(self):
        with self.cache_clock(0), \
             patch.object(publication.hashlib, 'file_digest', wraps=hashlib.file_digest) as digest:
            first = publication._hash(self.file)
            self.assertEqual(publication._hash(self.file), first)
            self.assertEqual(digest.call_count, 2)
            self.assertEqual(list(publication._hashes), [])
        with self.cache_clock(-30), \
             patch.object(publication.hashlib, 'file_digest', wraps=hashlib.file_digest) as digest:
            publication._hash(self.file)
            publication._hash(self.file)
            self.assertEqual(digest.call_count, 2)
            self.assertEqual(list(publication._hashes), [])

    def test_cache_eligibility_converts_platform_change_time_units(self):
        now = 1_800_000_000_000_000_000
        second = 1_000_000_000
        if os.name == 'nt':
            epoch = publication._FILETIME_UNIX_EPOCH_100NS
            settled, boundary = (now - 3 * second) // 100 + epoch, (now - second) // 100 + epoch
            fresh, future = now // 100 + epoch, (now + 60 * second) // 100 + epoch
        else:
            settled, boundary = now - 3 * second, now - second
            fresh, future = now, now + 60 * second
        stamp = lambda changed: (1, 2, 3, 4, changed)
        self.assertTrue(publication._cache_eligible(stamp(settled), now_ns=now))
        self.assertTrue(publication._cache_eligible(stamp(boundary), now_ns=now))
        self.assertFalse(publication._cache_eligible(stamp(fresh), now_ns=now))
        self.assertFalse(publication._cache_eligible(stamp(future), now_ns=now))
        self.assertFalse(publication._cache_eligible(None, now_ns=now))
        self.assertFalse(publication._cache_eligible((1, 2, 3, 4), now_ns=now))
        self.assertFalse(publication._cache_eligible(stamp(0), now_ns=now))
        self.assertFalse(publication._cache_eligible(stamp('invalid'), now_ns=now))

    def test_same_tick_rewrite_cannot_reuse_an_uncached_digest(self):
        with self.cache_clock(0), \
             patch.object(publication.hashlib, 'file_digest', wraps=hashlib.file_digest) as digest:
            first = publication._hash(self.file)
            self.assertEqual(publication._hash(self.file), first)
            self.assertEqual(digest.call_count, 2)
            self.assertEqual(list(publication._hashes), [])
        frozen = self.stamp()
        original = self.file.stat()
        self.file.write_bytes(b'modified')  # Same size and mtime: the tick may not advance.
        os.utime(self.file, ns=(original.st_atime_ns, original.st_mtime_ns))
        with patch.object(publication, '_content_stamp', return_value=frozen), \
             self.cache_clock(0), \
             patch.object(publication.hashlib, 'file_digest', wraps=hashlib.file_digest) as digest:
            self.assertNotEqual(publication._hash(self.file), first)
            self.assertEqual(digest.call_count, 1)
            self.assertEqual(list(publication._hashes), [])

    def test_atomic_replacement_cannot_reuse_old_digest(self):
        first = publication._hash(self.file)
        replacement = self.root / 'replacement'
        replacement.write_bytes(b'replaced')
        old = self.file.stat()
        os.utime(replacement, ns=(old.st_atime_ns, old.st_mtime_ns))
        replacement.replace(self.file)
        self.assertNotEqual(publication._hash(self.file), first)

    def test_unavailable_change_stamp_requires_full_hash_and_mid_read_change_fails(self):
        with patch.object(publication, '_content_stamp', return_value=None), \
             patch.object(publication.hashlib, 'file_digest', wraps=hashlib.file_digest) as digest:
            publication._hash(self.file)
            publication._hash(self.file)
            self.assertEqual(digest.call_count, 2)
        with patch.object(publication, '_content_stamp', side_effect=[('before',), ('after',)]):
            with self.assertRaises(OSError):
                publication._hash(self.file)

    def test_cached_hash_keeps_manifest_file_and_content_checks(self):
        metadata = dict(source={}, files=publication._inventory(self.root),
                        hashes={'app.bin': publication._hash(self.file)})
        marker = self.root / 'manifest.json'
        marker.write_text(json.dumps(metadata))
        self.assertIsNotNone(publication.validated(self.root, marker.name))
        old = self.file.stat()
        self.file.write_bytes(b'modified')
        os.utime(self.file, ns=(old.st_atime_ns, old.st_mtime_ns))
        self.assertIsNone(publication.validated(self.root, marker.name))
        self.file.unlink()
        self.assertIsNone(publication.validated(self.root, marker.name))

    def test_manifest_cannot_hash_an_unchecked_path(self):
        metadata = dict(source={}, files=publication._inventory(self.root),
                        hashes={'../outside': 'unexpected'})
        (self.root / 'manifest.json').write_text(json.dumps(metadata))
        self.assertIsNone(publication.validated(self.root, 'manifest.json'))


if __name__ == '__main__':
    unittest.main()
