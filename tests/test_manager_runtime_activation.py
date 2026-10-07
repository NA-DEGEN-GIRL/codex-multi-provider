from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from activate_manager_runtime import activate, SHARED_CHECKS
from manager_core.store import atomic_json

THREADS_CRLF = b'CREATE TABLE threads (\r\n    id TEXT PRIMARY KEY\r\n);\r\n'


def migrated_store(path, checksum):
    """A store sqlx migrated with `checksum` for (1, 'threads')."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute('CREATE TABLE _sqlx_migrations (version INTEGER PRIMARY KEY, description TEXT NOT NULL, '
                           'installed_on TEXT NOT NULL, success BOOLEAN NOT NULL, checksum BLOB NOT NULL, '
                           'execution_time INTEGER NOT NULL)')
        connection.execute("INSERT INTO _sqlx_migrations VALUES (1, 'threads', '2026-10-01 00:00:00', 1, ?, 1000)",
                           (checksum,))
        connection.commit()
    return path


class RuntimeActivationTests(unittest.TestCase):
    def test_shared_execution_requires_both_accounts_and_paginated_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / 'artifacts/manager-runtime/releases/shared'
            release.mkdir(parents=True)
            binary = release / 'codex.exe'; binary.write_bytes(b'shared fixture')
            digest = hashlib.sha256(binary.read_bytes()).hexdigest()
            candidate = release / 'candidate.json'
            atomic_json(candidate, dict(runtime=str(binary), sha256=digest,
                capabilities={'shared_record_execution': True}, files={'codex.exe': digest}))
            evidence = root / 'artifacts/results/shared/report.json'
            report = dict(status='PASS', finished_at='complete', runtime_sha256=digest,
                validation_kind='shared_record_execution_v1', history_mode='paginated', checks={k:True for k in SHARED_CHECKS})
            for key in SHARED_CHECKS:
                atomic_json(evidence, {**report, 'checks': {k:v for k,v in report['checks'].items() if k != key}})
                with self.assertRaises(ValueError): activate(candidate, evidence, root)
            atomic_json(evidence, {**report, 'validation_kind': 'old_handoff', 'parent_thread_id': 'root',
                'transfers': [dict(writer_release_verified=True, binding_reloaded=True, root_thread_id='root') for _ in range(2)]})
            with self.assertRaises(ValueError): activate(candidate, evidence, root)
            atomic_json(evidence, report)
            self.assertEqual(activate(candidate, evidence, root)['validation'], 'experimental-headless-shared-editing')

    def test_only_completed_matching_runtime_evidence_changes_pointer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / 'artifacts/manager-runtime/releases/test'
            release.mkdir(parents=True)
            binary = release / 'codex.exe'
            binary.write_bytes(b'isolated fixture')
            digest = hashlib.sha256(binary.read_bytes()).hexdigest()
            candidate = release / 'candidate.json'
            atomic_json(candidate, dict(runtime=str(binary), sha256=digest,
                                        files={'codex.exe': digest}))
            evidence = root / 'artifacts/results/fixture/report.json'
            report = dict(status='PASS', finished_at='complete', runtime_sha256=digest,
                          checks={'proof': True}, parent_thread_id='root', transfers=[
                              dict(writer_release_verified=True, binding_reloaded=True,
                                   root_thread_id='root') for _ in range(2)])
            pointer = root / 'artifacts/manager-runtime/current.json'
            for changes in (dict(status='RUNNING'), dict(runtime_sha256='different'),
                            dict(transfers=[]), dict(checks={'proof': False})):
                atomic_json(evidence, {**report, **changes})
                with self.assertRaises(ValueError):
                    activate(candidate, evidence, root)
                self.assertFalse(pointer.exists())
            atomic_json(evidence, report)
            result = activate(candidate, evidence, root)
            self.assertFalse(result['running_profiles_restarted'])
            self.assertEqual(json.loads(pointer.read_text())['validation'],
                             'experimental-headless-handoff')
            original_pointer = pointer.read_bytes()
            binary.write_bytes(b'changed after validation')
            with self.assertRaises(RuntimeError):
                activate(candidate, evidence, root)
            self.assertEqual(pointer.read_bytes(), original_pointer)

    def test_release_whose_migrations_differ_from_the_live_stores_is_not_activated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            release = root / 'artifacts/manager-runtime/releases/cross'
            release.mkdir(parents=True)
            binary = release / 'codex.exe'
            binary.write_bytes(b'cross-built fixture')
            digest = hashlib.sha256(binary.read_bytes()).hexdigest()
            migrations = [dict(directory='migrations', version=1, description='threads',
                               sha384=hashlib.sha384(THREADS_CRLF).hexdigest())]
            candidate = release / 'candidate.json'
            atomic_json(candidate, dict(runtime=str(binary), sha256=digest, files={'codex.exe': digest},
                                        migrations=migrations))
            evidence = root / 'artifacts/results/fixture/report.json'
            atomic_json(evidence, dict(status='PASS', finished_at='complete', runtime_sha256=digest,
                                       checks={'proof': True}, parent_thread_id='root', transfers=[
                                           dict(writer_release_verified=True, binding_reloaded=True,
                                                root_thread_id='root') for _ in range(2)]))
            home = root / 'record-home'
            atomic_json(root / 'work/control-center/canonical-storage.json', dict(version=1, home=str(home)))
            pointer = root / 'artifacts/manager-runtime/current.json'
            previous = pointer.with_name('previous.json')
            # The stores matched when it was staged and first activated.
            store = migrated_store(home / 'state_5.sqlite', bytes.fromhex(migrations[0]['sha384']))
            self.assertEqual(activate(candidate, evidence, root)['sha256'], digest)
            active = pointer.read_bytes()
            self.assertFalse(previous.exists())
            # Since then a runtime built from LF sources migrated a store (here: a profile home).
            lf = hashlib.sha384(THREADS_CRLF.replace(b'\r\n', b'\n')).digest()
            for path in (store, root / 'work/control-center/profiles/alpha/codex/state_5.sqlite'):
                with self.subTest(store=path.parent.name):
                    migrated_store(path, lf)
                    with self.assertRaisesRegex(ValueError, '^This runtime would refuse existing records') as raised:
                        activate(candidate, evidence, root)
                    self.assertIn('%s: 1 threads' % path, str(raised.exception))
                    # Refused before anything is switched or backed up.
                    self.assertEqual(pointer.read_bytes(), active)
                    self.assertFalse(previous.exists())
                    migrated_store(path, bytes.fromhex(migrations[0]['sha384']))
            # Matching stores again: the switch proceeds.
            self.assertEqual(activate(candidate, evidence, root)['sha256'], digest)
            self.assertEqual(json.loads(previous.read_text(encoding='utf-8'))['sha256'], digest)
            self.assertEqual(json.loads(pointer.read_text(encoding='utf-8'))['migrations'], migrations)
