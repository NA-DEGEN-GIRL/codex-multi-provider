import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from remote_helpers.managed_sources import generate
from manager_core.store import atomic_json


class RemoteSourceTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name).resolve()/'profiles'
        self.one=self.root/str(uuid4())
        self.two=self.root/str(uuid4())
        for profile in (self.one,self.two):
            (profile/'codex').mkdir(parents=True)
            generate(profile,atomic=atomic_json)

    def grant(self,profile,thread=None,owner=None):
        thread=thread or str(uuid4())
        data=dict(version=1,host_id='local',store_id='manager:'+profile.name,
                  thread_id=thread,owner_profile_id=owner or profile.name,epoch=2,revision=3)
        path=profile/'codex/managed-authority'/(thread+'.json')
        atomic_json(path,data)
        return path,data

    def test_foreign_source_reference_retains_its_actual_owner_and_records(self):
        path,grant=self.grant(self.one)
        before=path.read_bytes()
        result=json.loads(generate(self.two,atomic=atomic_json).read_text())
        self.assertEqual(len(result['sources']),2)
        self.assertEqual(result['bindings'],[dict(threadId=grant['thread_id'],hostId='local',
            sourceStoreId=grant['store_id'],ownerProfileId=self.one.name,ownershipEpoch=2,recordRevision=3)])
        self.assertEqual(path.read_bytes(),before)
        self.assertFalse((self.two/'codex/managed-authority').exists())

    def test_new_ownership_is_reflected_without_reusing_old_manifest_authority(self):
        path,grant=self.grant(self.one)
        generate(self.two,atomic=atomic_json)
        grant.update(owner_profile_id=self.two.name,epoch=3,revision=4)
        atomic_json(path,grant)
        result=json.loads(generate(self.two,atomic=atomic_json).read_text())
        self.assertEqual(result['bindings'][0]['ownerProfileId'],self.two.name)
        self.assertEqual(result['bindings'][0]['ownershipEpoch'],3)

    def test_invalid_or_ambiguous_sources_preserve_previous_manifest(self):
        _,grant=self.grant(self.one)
        output=generate(self.two,atomic=atomic_json)
        before=output.read_bytes()
        self.grant(self.two,thread=grant['thread_id'])
        with self.assertRaises(ValueError):
            generate(self.two,atomic=atomic_json)
        self.assertEqual(output.read_bytes(),before)

    def test_corrupt_identity_never_gets_silently_remarked(self):
        marker=self.one/'codex/managed-source.json'
        atomic_json(marker,{'host_id':'different','store_id':'manager:'+self.one.name})
        before=marker.read_bytes()
        with self.assertRaises(ValueError):
            generate(self.one,atomic=atomic_json)
        self.assertEqual(marker.read_bytes(),before)

    def test_shared_catalog_enrollment_publishes_sources_without_copying_private_files(self):
        private_files = {}
        for profile in (self.one, self.two):
            for name in ('config.toml', 'auth.json'):
                path = profile / 'codex' / name
                path.write_bytes((profile.name + ':' + name).encode())
                private_files[path] = path.read_bytes()
        generate(self.one, atomic=atomic_json, shared_catalog=True)
        catalog = self.root.parent / 'catalog-sources.json'
        self.assertEqual(json.loads(catalog.read_text()), {
            'version': 2, 'hostId': 'local',
            'sources': [dict(hostId='local', sourceStoreId='manager:' + profile.name,
                             codexHome=str(profile / 'codex'))
                        for profile in sorted((self.one, self.two))]})
        self.assertEqual({path: path.read_bytes() for path in private_files}, private_files)
        previous = catalog.read_bytes()
        atomic_json(self.two / 'codex/managed-source.json', {'host_id': 'wrong'})
        with self.assertRaises(ValueError):
            generate(self.one, atomic=atomic_json, shared_catalog=True)
        self.assertEqual(catalog.read_bytes(), previous)

    def test_concurrent_profile_enrollment_keeps_every_source_in_shared_catalog(self):
        profiles = [self.root / str(uuid4()) for _ in range(8)]
        for profile in profiles:
            (profile / 'codex').mkdir(parents=True)
        with ThreadPoolExecutor(max_workers=len(profiles)) as pool:
            futures = [pool.submit(generate, profile, atomic=atomic_json, shared_catalog=True)
                       for profile in profiles]
            for future in futures:
                future.result(timeout=20)
        catalog = json.loads((self.root.parent / 'catalog-sources.json').read_text())
        self.assertEqual({source['sourceStoreId'] for source in catalog['sources']},
                         {'manager:' + profile.name for profile in [self.one, self.two, *profiles]})

    def test_mixed_catalog_enrolls_legacy_homes_without_managed_authority(self):
        user_home = self.root.parent / 'user'
        stock = user_home / '.codex'
        stock.mkdir(parents=True)
        registry = user_home / '.config/llm-usage/config.json'
        atomic_json(registry, dict(schema_version=3, accounts=[
            dict(provider='codex', alias='04', profile_dir=str(stock)),
            dict(provider='codex', alias='Unavailable', profile_dir=str(user_home / 'absent'))]))
        (stock / 'auth.json').write_bytes(b'Private fixture; discovery must not open this file.')
        before = (stock / 'auth.json').read_bytes()
        with patch('pathlib.Path.home', return_value=user_home), patch.dict('os.environ', {'XDG_CONFIG_HOME': str(user_home / '.config')}):
            manifest = json.loads(generate(self.one, atomic=atomic_json, shared_catalog=True, legacy_discovery=True).read_text())
        mixed = json.loads((self.root.parent / 'catalog-mixed-sources.json').read_text())
        self.assertEqual(mixed['version'], 3)
        self.assertEqual(mixed['sources'], [])
        self.assertEqual(mixed['managedSourcesPath'], str(self.root.parent / 'catalog-sources.json'))
        self.assertEqual([s['codexHome'] for s in mixed['legacySources']], [str(stock)])
        self.assertFalse(manifest['bindings'])
        self.assertTrue(all(s['sourceStoreId'].startswith('manager:') for s in manifest['sources']))
        self.assertEqual((stock / 'auth.json').read_bytes(), before)
        self.assertEqual({p.name for p in stock.iterdir()}, {'auth.json'})

    def test_older_catalog_publication_keeps_mixed_descriptor_and_adds_new_managed_source(self):
        user_home = self.root.parent / 'user'
        (user_home / '.codex').mkdir(parents=True)
        with patch('pathlib.Path.home', return_value=user_home), patch.dict('os.environ', {'XDG_CONFIG_HOME': str(user_home / '.config')}):
            generate(self.one, atomic=atomic_json, shared_catalog=True, legacy_discovery=True)
        mixed_path = self.root.parent / 'catalog-mixed-sources.json'
        before = mixed_path.read_bytes()
        added = self.root / str(uuid4())
        (added / 'codex').mkdir(parents=True)
        generate(added, atomic=atomic_json, shared_catalog=True)
        self.assertEqual(mixed_path.read_bytes(), before)
        inventory = json.loads((self.root.parent / 'catalog-sources.json').read_text())
        self.assertIn('manager:' + added.name, {s['sourceStoreId'] for s in inventory['sources']})

    def test_bad_registry_preserves_previous_mixed_and_managed_catalogs(self):
        user_home = self.root.parent / 'user'
        (user_home / '.codex').mkdir(parents=True)
        with patch('pathlib.Path.home', return_value=user_home), patch.dict('os.environ', {'XDG_CONFIG_HOME': str(user_home / '.config')}):
            generate(self.one, atomic=atomic_json, shared_catalog=True, legacy_discovery=True)
            paths = [self.root.parent / name for name in ('catalog-sources.json', 'catalog-mixed-sources.json')]
            before = [p.read_bytes() for p in paths]
            atomic_json(user_home / '.config/llm-usage/config.json', {'schema_version': 999})
            with self.assertRaises(ValueError):
                generate(self.one, atomic=atomic_json, shared_catalog=True, legacy_discovery=True)
            self.assertEqual([p.read_bytes() for p in paths], before)
