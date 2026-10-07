"""Last-known-good and known-bad runtime pointers; temporary release folders only."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from activate_manager_runtime import activate
from manager_core.store import atomic_json
from rollback_manager_runtime import mark_bad, mark_good, rollback
from test_manager_runtime_activation import (THREADS_CRLF, handoff_evidence, migrated_store,
                                             stage_release)


class RuntimeRollbackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.runtimes = self.root / 'artifacts/manager-runtime'
        self.current = self.runtimes / 'current.json'
        self.good = self.runtimes / 'last-known-good.json'
        self.bad = self.runtimes / 'known-bad.json'

    def activated(self, name, content=None, **fields):
        candidate, digest = stage_release(self.root, name, content, **fields)
        activate(candidate, handoff_evidence(self.root, digest, name), self.root)
        return candidate, digest

    def pointer(self, path):
        return json.loads(path.read_text(encoding='utf-8'))

    def test_mark_good_records_only_a_verified_activatable_current_runtime(self):
        with self.assertRaisesRegex(ValueError, 'No managed runtime is active'):
            mark_good(self.root)
        candidate, digest = self.activated('20261001-000000-aaaaaa')
        self.assertEqual(mark_good(self.root), dict(last_known_good=candidate.parent.name, sha256=digest))
        recorded = self.pointer(self.good)
        self.assertEqual(recorded['sha256'], digest)
        self.assertIn('marked_good_at', recorded)
        before = self.good.read_bytes()
        (candidate.parent / 'codex.exe').write_bytes(b'changed after activation')
        with self.assertRaisesRegex(ValueError, 'current runtime no longer verifies'):
            mark_good(self.root)
        # A runtime activated before migrations were recorded cannot be re-checked by a rollback.
        legacy, _ = stage_release(self.root, '20261002-000000-bbbbbb')
        manifest = self.pointer(legacy)
        manifest.pop('migrations')
        atomic_json(self.current, manifest)
        with self.assertRaisesRegex(ValueError, 'no embedded migrations'):
            mark_good(self.root)
        self.assertEqual(self.good.read_bytes(), before)

    def test_rollback_reactivates_last_known_good_and_records_the_replaced_runtime(self):
        good, good_digest = self.activated('20261001-000000-aaaaaa')
        mark_good(self.root)
        broken, broken_digest = self.activated('20261002-000000-bbbbbb')
        result = rollback(self.root)
        self.assertEqual((result['sha256'], result['replaced'], result['changed']),
                         (good_digest, broken.parent.name, True))
        self.assertFalse(result['running_profiles_restarted'])
        current = self.pointer(self.current)
        self.assertEqual(current['sha256'], good_digest)
        self.assertEqual(current['rolled_back_from'], broken.parent.name)
        self.assertNotIn('marked_good_at', current)
        self.assertEqual(self.pointer(self.runtimes / 'previous.json')['sha256'], broken_digest)
        entries = self.pointer(self.bad)['releases']
        self.assertEqual([(entry['release'], entry['sha256']) for entry in entries],
                         [(broken.parent.name, broken_digest)])
        # The replaced runtime cannot come back by accident.
        with self.assertRaisesRegex(ValueError, 'known-bad.json'):
            activate(broken, handoff_evidence(self.root, broken_digest, 'again'), self.root)
        self.assertEqual(rollback(self.root)['changed'], False)
        self.assertEqual(len(self.pointer(self.bad)['releases']), 1)
        # Explicitly kept: a later rollback leaves known-bad.json alone.
        self.activated('20261003-000000-cccccc')
        self.assertNotIn('known_bad', rollback(self.root, keep_replaced=True))
        self.assertEqual(len(self.pointer(self.bad)['releases']), 1)
        self.assertEqual(self.pointer(self.current)['sha256'], good_digest)

    def test_rollback_refuses_a_missing_changed_bad_or_incompatible_last_known_good(self):
        with self.assertRaisesRegex(ValueError, 'No last-known-good runtime'):
            rollback(self.root)
        good, good_digest = self.activated('20261001-000000-aaaaaa')
        mark_good(self.root)
        _, active_digest = self.activated('20261002-000000-bbbbbb')
        active = self.current.read_bytes()
        backup = self.root / 'backup'
        shutil.copytree(good.parent, backup)
        # Deleted, then changed release files.
        shutil.rmtree(good.parent)
        with self.assertRaisesRegex(ValueError, 'last-known-good runtime no longer verifies'):
            rollback(self.root)
        shutil.copytree(backup, good.parent)
        (good.parent / 'codex.exe').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'last-known-good runtime no longer verifies'):
            rollback(self.root)
        (good.parent / 'codex.exe').write_bytes((backup / 'codex.exe').read_bytes())
        # Stores migrated by an LF-built runtime since it was marked.
        home = self.root / 'record-home'
        atomic_json(self.root / 'work/control-center/canonical-storage.json', dict(version=1, home=str(home)))
        store = migrated_store(home / 'state_5.sqlite', hashlib.sha384(THREADS_CRLF.replace(b'\r\n', b'\n')).digest())
        with self.assertRaisesRegex(ValueError, '^This runtime would refuse existing records'):
            rollback(self.root)
        migrated_store(store, hashlib.sha384(THREADS_CRLF).digest())
        atomic_json(self.bad, dict(version=1, releases=[dict(sha256=good_digest, reason='fixture')]))
        with self.assertRaisesRegex(ValueError, 'known-bad.json'):
            rollback(self.root)
        self.assertEqual(self.current.read_bytes(), active)
        self.bad.unlink()
        self.assertEqual(rollback(self.root)['sha256'], good_digest)
        self.assertEqual(self.pointer(self.runtimes / 'previous.json')['sha256'], active_digest)

    def test_mark_bad_records_a_deleted_previous_release_by_digest(self):
        first, first_digest = self.activated('20261001-000000-aaaaaa')
        self.activated('20261002-000000-bbbbbb')
        shutil.rmtree(first.parent)
        result = mark_bad(self.root, 'previous', 'exited at startup')
        self.assertTrue(result['added'])
        self.assertEqual((result['known_bad']['release'], result['known_bad']['sha256'], result['known_bad']['reason']),
                         (first.parent.name, first_digest, 'exited at startup'))
        self.assertFalse(mark_bad(self.root, 'previous')['added'])
        # The same binary staged again under a new folder is still refused.
        restaged, digest = stage_release(self.root, '20261003-000000-cccccc', first.parent.name.encode())
        self.assertEqual(digest, first_digest)
        with self.assertRaisesRegex(ValueError, 'known-bad.json'):
            activate(restaged, handoff_evidence(self.root, digest, 'restaged'), self.root)
        self.assertFalse(mark_bad(self.root, restaged.parent.name)['added'])
        for target in ('../outside', 'last-known-good', 'missing-release'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                mark_bad(self.root, target)


if __name__ == '__main__':
    unittest.main()
