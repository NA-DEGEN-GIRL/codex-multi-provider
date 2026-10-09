"""Warm navigation does not rebuild discarded cross-profile authority manifests."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.instances import Instances
from manager_core.store import Store


class NavigationEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root)
        self.profile = self.store.add_profile('navigation fixture')
        self.instances = Instances(self.root, self.store, Mock())
        self.runtime = self.root / 'runtime.exe'
        self.runtime.touch()

    def environment(self, shared, *, canonical=True):
        capabilities = {'managed_store_binding': True, 'canonical_record_storage': canonical}
        with patch('desktop_launch.child_environment', return_value={}), \
                patch('manager_core.runtime_build.resolve', return_value={
                    'runtime': str(self.runtime), 'capabilities': capabilities}), \
                patch('manager_core.workspace_seed.ensure'), \
                patch('manager_core.shared_catalog.environment', return_value=shared), \
                patch('manager_core.managed_sources.manifest', return_value=self.root/'legacy.json') as manifest:
            value = self.instances.environment(self.profile)
        return value, manifest

    def test_actual_canonical_home_skips_cross_profile_authority_scan(self):
        common = str(self.root / 'common')
        value, manifest = self.environment({'CODEX_RECORD_HOME': common})
        manifest.assert_not_called()
        self.assertEqual(common, value['CODEX_RECORD_HOME'])
        self.assertNotIn('CODEX_MANAGER_MANAGED_SOURCES', value)
        self.assertEqual(self.profile['id'], value['CODEX_MANAGER_SHARED_WRITER_ID'])

    def test_capability_without_canonical_home_retains_fallback(self):
        value, manifest = self.environment({})
        manifest.assert_called_once_with(self.store, self.profile)
        self.assertEqual(str(self.root/'legacy.json'), value['CODEX_MANAGER_MANAGED_SOURCES'])

    def test_legacy_runtime_retains_authority_manifest(self):
        value, manifest = self.environment({}, canonical=False)
        manifest.assert_called_once_with(self.store, self.profile)
        self.assertEqual(str(self.root/'legacy.json'), value['CODEX_MANAGER_MANAGED_SOURCES'])

    def navigation(self, shared, capabilities):
        with patch('manager_core.runtime_build.resolve', return_value={
                    'runtime': str(self.runtime), 'capabilities': capabilities}), \
                patch('manager_core.shared_catalog.environment', return_value=shared), \
                patch('manager_core.managed_sources.manifest', return_value=self.root/'legacy.json'), \
                patch('desktop_launch.child_environment', side_effect=AssertionError('launch environment rebuilt')), \
                patch('manager_core.workspace_seed.ensure', side_effect=AssertionError('workspace seeded')):
            return self.instances.navigation_environment(self.profile)

    def test_task_link_authorization_reads_only_the_record_keys(self):
        self.instances.providers.environment.side_effect = AssertionError('provider keys decrypted')
        common = str(self.root / 'common')
        shared = {'CODEX_RECORD_HOME': common, 'CODEX_MANAGER_SHARED_EXECUTION': '1'}
        self.assertEqual(self.navigation(shared, {'managed_store_binding': True, 'canonical_record_storage': True}),
                         {**shared, 'CODEX_MANAGER_SHARED_WRITER_ID': self.profile['id']})
        catalog = {'CODEX_MANAGER_SHARED_CATALOG': str(self.root / 'catalog.json')}
        self.assertEqual(self.navigation(catalog, {'shared_record_catalog': True, 'shared_record_execution': True}),
                         {**catalog, 'CODEX_MANAGER_SHARED_EXECUTION': '1', 'CODEX_MANAGER_SHARED_WRITER_ID': self.profile['id']})
        self.assertEqual(self.navigation({}, {'managed_store_binding': True}),
                         {'CODEX_MANAGER_MANAGED_SOURCES': str(self.root/'legacy.json')})
        # The launch environment has the same record keys for each case.
        value, _ = self.environment(shared)
        self.assertEqual({k: value[k] for k in shared}, shared)
        self.store.mutate(lambda data: self.store.profile(self.profile['id'], data).update(runtime_channel='packaged'))
        self.profile = self.store.profile(self.profile['id'])
        self.assertEqual(self.navigation(shared, {}), {})


if __name__ == '__main__':
    unittest.main()
