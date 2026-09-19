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

    def test_unchanged_content_is_hashed_once_but_modification_invalidates(self):
        with patch.object(publication.hashlib, 'file_digest', wraps=hashlib.file_digest) as digest:
            first = publication._hash(self.file)
            self.assertEqual(publication._hash(self.file), first)
            self.assertEqual(digest.call_count, 1)
            original = self.file.stat()
            self.file.write_bytes(b'modified')  # Same size, with mtime restored.
            os.utime(self.file, ns=(original.st_atime_ns, original.st_mtime_ns))
            self.assertNotEqual(publication._hash(self.file), first)
            self.assertEqual(digest.call_count, 2)

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
