"""SSH workers edit shared records only with a compatible, selected runtime."""
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from remote_helpers import launch, managed_sources


class RemoteSharedExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.profile = self.base / 'profiles' / str(uuid4())
        self.runtime = self.base / 'runtime' / 'fixture'
        self.runtime.mkdir(parents=True)
        self.binary = self.runtime / 'codex'
        self.binary.write_bytes(b'CODEX_MANAGER_SHARED_EXECUTION\0CODEX_MANAGER_SHARED_WRITER_ID\0CODEX_MANAGER_SHARED_ROUTES')
        self.revision = 'b' * 64
        definition = self.profile / 'definitions' / self.revision
        definition.mkdir(parents=True)
        (self.profile / 'credentials').mkdir()
        (self.profile / 'credentials' / (self.revision + '.json')).write_text('{}')
        self.descriptor = dict(revision=self.revision, profile_id=self.profile.name,
                               runtime=str(self.runtime), definition=str(definition),
                               managed_sources=True, source_catalog=True)

    def environment(self):
        descriptor_path = self.profile / 'definitions' / (self.revision + '.json')
        descriptor_path.write_text(json.dumps(self.descriptor), encoding='utf-8')
        common = types.SimpleNamespace(reconcile_skills=MagicMock())
        fcntl = types.SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=MagicMock())
        inherited = {'CODEX_MANAGER_RECORD_CATALOG': '/untrusted/catalog.json',
                     'CODEX_MANAGER_SHARED_EXECUTION': '1',
                     'CODEX_MANAGER_SHARED_WRITER_ID': str(uuid4()),
                     'CODEX_RECORD_HOME': '/untrusted/records',
                     'CODEX_RECORD_SHARED_APPEND': '1',
                     'CODEX_HOME': '/untrusted/auth'}
        with patch.dict(launch.os.environ, inherited), \
             patch.dict(sys.modules, {'common': common, 'fcntl': fcntl,
                                     'managed_sources': managed_sources}), \
             patch.object(launch.os, 'execve', side_effect=RuntimeError('fixture exec')) as execute:
            with self.assertRaisesRegex(RuntimeError, 'fixture exec'):
                launch.run(self.profile, self.revision, ['app-server'])
        return execute.call_args.args[2]

    def test_shared_worker_uses_selected_auth_and_writer_without_canonical_home_override(self):
        foreign = self.base / 'profiles' / str(uuid4())
        (foreign / 'codex').mkdir(parents=True)
        private = foreign / 'codex/auth.json'
        private.write_bytes(b'foreign-auth-sentinel')
        managed_sources.generate(foreign, atomic=launch._atomic)
        env = self.environment()
        self.assertEqual(env['CODEX_HOME'], str(self.profile / 'codex'))
        self.assertEqual(env['CODEX_MANAGER_SHARED_EXECUTION'], '1')
        self.assertEqual(env['CODEX_MANAGER_SHARED_WRITER_ID'], self.profile.name)
        self.assertEqual(env['CODEX_RECORD_SHARED_APPEND'], '1')
        self.assertEqual(env['CODEX_MANAGER_SHARED_ROUTES'], str(self.base / 'shared-record-routes.json'))
        self.assertNotIn('CODEX_MANAGER_RECORD_CATALOG', env)
        self.assertNotIn('CODEX_MANAGER_MANAGED_SOURCES', env)
        self.assertNotIn('CODEX_RECORD_HOME', env)
        catalog = json.loads(Path(env['CODEX_MANAGER_SHARED_CATALOG']).read_text())
        self.assertEqual({s['sourceStoreId'] for s in catalog['sources']},
                         {'manager:' + p.name for p in (self.profile, foreign)})
        self.assertEqual(private.read_bytes(), b'foreign-auth-sentinel')
        self.assertFalse((self.profile / 'codex/auth.json').exists())

    def test_older_catalog_runtime_does_not_inherit_shared_write_authority(self):
        for content in (b'', b'old catalog runtime', b'CODEX_MANAGER_SHARED_EXECUTION',
                        b'CODEX_MANAGER_SHARED_EXECUTION\0CODEX_MANAGER_SHARED_WRITER_ID'):
            with self.subTest(content=content):
                self.binary.write_bytes(content)
                env = self.environment()
                self.assertIn('CODEX_MANAGER_SHARED_CATALOG', env)
                self.assertIn('CODEX_MANAGER_MANAGED_SOURCES', env)
                for name in ('CODEX_MANAGER_SHARED_EXECUTION', 'CODEX_MANAGER_SHARED_WRITER_ID',
                             'CODEX_RECORD_SHARED_APPEND', 'CODEX_MANAGER_RECORD_CATALOG'):
                    self.assertNotIn(name, env)

    def test_without_catalog_descriptor_no_shared_authority_is_enabled(self):
        for managed in (True, False):
            with self.subTest(managed=managed):
                self.descriptor.update(managed_sources=managed, source_catalog=False)
                env = self.environment()
                for name in ('CODEX_MANAGER_SHARED_CATALOG', 'CODEX_MANAGER_SHARED_EXECUTION',
                             'CODEX_MANAGER_SHARED_WRITER_ID', 'CODEX_RECORD_SHARED_APPEND'):
                    self.assertNotIn(name, env)

    def test_writer_identity_requires_canonical_profile_uuid(self):
        with self.assertRaises(ValueError):
            launch.shared_execution_environment(self.runtime, 'foreign-writer')


if __name__ == '__main__':
    unittest.main()
