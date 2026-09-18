import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from activate_manager_runtime import activate, SHARED_CHECKS
from manager_core.store import atomic_json


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
