"""Staged login CLI digests must be reused without weakening the integrity check."""
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core import desktop_publication as publication
from manager_core.login_probe import verification_runtime

PAYLOAD = bytes(range(256)) * 512


class LoginRuntimeDigestCacheTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / 'WindowsApps/app/resources/codex.exe'
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(PAYLOAD)
        self.app = {'InstallLocation': str(self.root / 'WindowsApps'), 'Version': 'fixture'}
        self.staged = self.root / 'artifacts/login-runtime/fixture/codex.exe'
        publication._hashes.clear()

    def digest_counter(self):
        """Count real SHA-256 passes without changing the hashing itself."""
        return patch.object(publication.hashlib, 'file_digest', wraps=hashlib.file_digest)

    def changed_ns(self, path):
        with path.open('rb') as stream:
            changed = publication._change_time_ns(publication._content_stamp(stream))
        self.assertIsNotNone(changed, 'content stamp unavailable')
        return changed

    def cache_clock(self, paths, seconds):
        """Freeze the cache clock relative to the affected content stamps.

        The shared cache only accepts stamps that are settled one second past
        the wall clock; moving the clock keeps the tests deterministic instead
        of waiting for real ticks.
        """
        stamps = [self.changed_ns(path) for path in paths]
        base = max(stamps) if seconds >= 0 else min(stamps)
        return patch.object(publication, '_now_ns',
                            return_value=base + int(seconds * 1_000_000_000))

    def rewrite(self, path, body):
        """Rewrite in place with the same byte count and the mtime restored."""
        original = path.stat()
        path.write_bytes(body)
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))

    def test_unchanged_source_and_staged_copy_reuse_digests(self):
        runtime = verification_runtime(self.root, self.app)
        self.assertEqual(runtime, self.staged)
        self.assertEqual(runtime.read_bytes(), PAYLOAD)
        with self.cache_clock([self.source, runtime], 2), self.digest_counter() as digest:
            self.assertEqual(verification_runtime(self.root, self.app), runtime)
            self.assertEqual(digest.call_count, 2)
            self.assertEqual(verification_runtime(self.root, self.app), runtime)
            self.assertEqual(digest.call_count, 2)
        self.assertEqual(list(runtime.parent.glob('*.tmp')), [])

    def test_first_use_stages_and_verifies_before_publishing(self):
        with self.digest_counter() as digest:
            runtime = verification_runtime(self.root, self.app)
            self.assertEqual(digest.call_count, 3)
        self.assertTrue(runtime.is_file())
        self.assertEqual(runtime.read_bytes(), PAYLOAD)
        self.assertEqual(list(runtime.parent.glob('*.tmp')), [])

    def test_same_size_restored_mtime_tamper_is_rejected(self):
        runtime = verification_runtime(self.root, self.app)
        with self.cache_clock([self.source, runtime], 0):
            verification_runtime(self.root, self.app)
            self.assertEqual(list(publication._hashes), [])  # fresh stamps are never stored
        self.rewrite(runtime, PAYLOAD[::-1])
        with self.digest_counter() as digest:
            with self.assertRaisesRegex(RuntimeError, '무결성'):
                verification_runtime(self.root, self.app)
            self.assertGreaterEqual(digest.call_count, 1)    # rehashed, not answered from cache
        self.assertEqual(self.source.read_bytes(), PAYLOAD)

    def test_atomic_replacement_of_staged_copy_is_rehashed(self):
        runtime = verification_runtime(self.root, self.app)
        replacement = runtime.with_name('codex.fixture-replacement')
        old = runtime.stat()
        replacement.write_bytes(PAYLOAD[::-1])
        os.utime(replacement, ns=(old.st_atime_ns, old.st_mtime_ns))
        os.replace(replacement, runtime)
        with self.assertRaisesRegex(RuntimeError, '무결성'):
            verification_runtime(self.root, self.app)

    def test_source_update_is_detected_and_fresh_copy_matches(self):
        runtime = verification_runtime(self.root, self.app)
        updated = PAYLOAD[::-1]
        replacement = self.source.with_name('codex-source-replacement')
        old = self.source.stat()
        replacement.write_bytes(updated)
        os.utime(replacement, ns=(old.st_atime_ns, old.st_mtime_ns))
        os.replace(replacement, self.source)
        with self.assertRaisesRegex(RuntimeError, '무결성'):
            verification_runtime(self.root, self.app)
        runtime.unlink()
        self.assertEqual(verification_runtime(self.root, self.app).read_bytes(), updated)

    def test_future_stamp_is_never_reused_and_still_verifies(self):
        runtime = verification_runtime(self.root, self.app)
        with self.cache_clock([self.source, runtime], -30), self.digest_counter() as digest:
            self.assertEqual(verification_runtime(self.root, self.app), runtime)
            self.assertEqual(verification_runtime(self.root, self.app), runtime)
            self.assertEqual(digest.call_count, 4)           # no reuse while the stamps are future
            self.assertEqual(list(publication._hashes), [])

    def test_missing_content_stamp_disables_reuse_and_still_rejects_tamper(self):
        runtime = verification_runtime(self.root, self.app)
        with patch.object(publication, '_content_stamp', return_value=None), \
             self.digest_counter() as digest:
            verification_runtime(self.root, self.app)
            verification_runtime(self.root, self.app)
            self.assertEqual(digest.call_count, 4)
        self.rewrite(runtime, PAYLOAD[::-1])
        with patch.object(publication, '_content_stamp', return_value=None):
            with self.assertRaisesRegex(RuntimeError, '무결성'):
                verification_runtime(self.root, self.app)


if __name__ == '__main__':
    unittest.main()
