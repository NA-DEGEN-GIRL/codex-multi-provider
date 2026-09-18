"""Preparation tests only: no account reads, model calls, runtime or GUI launch."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

SCRIPT_ROOT = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPT_ROOT))
spec = importlib.util.spec_from_file_location('manager_handoff_live_driver', SCRIPT_ROOT / 'test_manager_handoff_live.py')
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


class DriverPreparationTests(unittest.TestCase):
    def publication(self, root, *, capabilities=None):
        runtime = root / 'artifacts/manager-runtime/releases/fixture/codex.exe'
        bootstrap = root / 'artifacts/manager/releases/fixture/Codex.ControlCenter.RuntimeProxy.exe'
        runtime.parent.mkdir(parents=True)
        bootstrap.parent.mkdir(parents=True)
        runtime.write_bytes(b'not-an-executable-runtime-fixture')
        bootstrap.write_bytes(b'not-an-executable-bootstrap-fixture')
        pointer = {'runtime': str(runtime), 'version': 'fixture',
            'sha256': hashlib.sha256(runtime.read_bytes()).hexdigest(),
            'capabilities': capabilities if capabilities is not None else {name: True for name in driver.REQUIRED_CAPABILITIES}}
        (root / 'artifacts/manager-runtime/current.json').write_text(json.dumps(pointer), encoding='utf-8')
        (root / 'artifacts/manager/current.json').write_text(json.dumps(
            {'runtime_proxy': str(bootstrap), 'directory': str(bootstrap.parent)}), encoding='utf-8')
        return runtime, bootstrap

    def test_default_only_prints_plan_and_cannot_launch(self):
        output = io.StringIO()
        with (contextlib.redirect_stdout(output),
              patch.object(driver, 'run', side_effect=AssertionError('must not execute')),
              patch.object(driver, 'checked_release', side_effect=AssertionError('no preflight side effects')),
              patch.object(driver.Accounts, 'list', side_effect=AssertionError('no account reads'))):
            self.assertEqual(driver.main([]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result['status'], 'NOT_RUN')
        self.assertEqual(result['accounts'], ['02', '04'])

    def test_old_unversioned_binary_never_substitutes_for_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = root / 'artifacts/runtime/codex.exe'
            old.parent.mkdir(parents=True)
            old.write_bytes(b'old')
            with self.assertRaises(RuntimeError):
                driver.checked_release(root)

    def test_release_requires_all_verified_capabilities_and_matching_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, bootstrap = self.publication(root)
            result, native, digest = driver.checked_release(root)
            self.assertEqual(result['runtime'], str(runtime))
            self.assertEqual(native, bootstrap)
            self.assertEqual(digest, hashlib.sha256(bootstrap.read_bytes()).hexdigest())
            runtime.write_bytes(b'tampered-longer-runtime')
            with self.assertRaises(RuntimeError):
                driver.checked_release(root)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.publication(root, capabilities={'managed_store_binding': True})
            with self.assertRaises(RuntimeError):
                driver.checked_release(root)

    def test_select_accounts_never_falls_back_to_another_alias(self):
        first, second = {'id': str(uuid4()), 'alias': '02'}, {'id': str(uuid4()), 'alias': '04'}
        self.assertEqual(driver.select_accounts([{'id': str(uuid4()), 'alias': '01'}, first, second]), [first, second])
        for values in ([first], [first, first, second], [first, {'id': first['id'], 'alias': '04'}]):
            with self.assertRaises(RuntimeError):
                driver.select_accounts(values)

    def test_candidate_is_verified_without_switching_active_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, bootstrap = self.publication(root)
            active = root / 'artifacts/manager-runtime/current.json'
            candidate = runtime.parent / 'candidate.json'
            candidate.write_bytes(active.read_bytes())
            active.unlink()
            result, native, _ = driver.checked_release(root, candidate)
            self.assertEqual((result['runtime'], native), (str(runtime), bootstrap))
            self.assertFalse(active.exists())
            companion = runtime.parent / 'codex-code-mode-host.exe'
            companion.write_bytes(b'fixture')
            data = json.loads(candidate.read_text())
            data['files'] = {companion.name: hashlib.sha256(companion.read_bytes()).hexdigest()}
            candidate.write_text(json.dumps(data))
            companion.write_bytes(b'changed')
            with self.assertRaises(RuntimeError):
                driver.checked_release(root, candidate)

    def test_profiles_are_unlisted_and_only_new_record_sources_are_registered(self):
        class Registry:
            def generate(self, home, enabled, model_ids):
                return {'bindings': [{'reasoning_effort': 'max', 'wire_model_id': 'deepseek-flash'}] if enabled else []}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = driver.isolated_store(root, 'fixture-run')
            account = {'id': str(uuid4()), 'alias': '02', 'home': str(root / 'REAL-ACCOUNT-MUST-NOT-BE-CATALOGED')}
            profile, _ = driver.prepare_profile(store, account, 'f' * 64, Registry(), str(uuid4()))
            self.assertFalse((root / 'work/control-center/state.json').exists())
            self.assertEqual(profile['source_home'], account['home'])
            sources = store.read()['sources']
            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0]['home'], profile['home'])
            self.assertNotEqual(sources[0]['home'], account['home'])
            self.assertEqual(Path(profile['home']), store.directory / 'profiles' / profile['id'] / 'codex')

    def test_snapshot_detects_rollout_and_database_changes_without_copying_bodies(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            sessions = home / 'sessions'
            sessions.mkdir()
            (sessions / 'fresh.jsonl').write_text('PRIVATE-FIXTURE-BODY', encoding='utf-8')
            database = home / 'state_5.sqlite'
            database.write_bytes(b'fixture-db')
            (home / 'logs_2.sqlite').write_bytes(b'background-logs')
            before = driver.record_snapshot(home)
            self.assertNotIn('PRIVATE', json.dumps(before))
            self.assertIn('state_5.sqlite', before)
            self.assertNotIn('logs_2.sqlite', before)
            database.write_bytes(b'changed-db')
            self.assertNotEqual(before, driver.record_snapshot(home))


if __name__ == '__main__':
    unittest.main()
