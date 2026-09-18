import json
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.catalog_refresh import CatalogRefresh
from manager_core.shared_catalog import environment
from manager_core.store import Store


class SharedCatalogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = Store(self.root)
        self.one = self.store.add_profile('01')
        self.two = self.store.add_profile('02')
        self.capabilities = {'shared_record_catalog': True, 'paginated_record_catalog': True}
        self.refresh = CatalogRefresh(self.root)

    def record(self, profile):
        directory = Path(profile['home']) / 'sessions'
        directory.mkdir(parents=True, exist_ok=True)
        identity = str(uuid4())
        path = directory / (identity + '.jsonl')
        content = json.dumps({'type': 'session_meta', 'payload': {'id': identity, 'history_mode': 'paginated'}}) + '\n'
        path.write_text(content, encoding='utf-8')
        return path, path.read_bytes()

    def test_both_workers_use_same_catalog_and_new_records_are_added_without_copying(self):
        first_path, first_bytes = self.record(self.one)
        first = environment(self.store, self.one, self.capabilities, self.refresh)
        second = environment(self.store, self.two, self.capabilities, self.refresh)
        self.assertEqual(first, second)
        manifest = Path(first['CODEX_MANAGER_SHARED_CATALOG'])
        self.assertEqual(len(json.loads(manifest.read_text(encoding='utf-8'))['entries']), 1)
        second_path, second_bytes = self.record(self.two)
        environment(self.store, self.one, self.capabilities, self.refresh)
        entries = json.loads(manifest.read_text(encoding='utf-8'))['entries']
        self.assertEqual({entry['threadId'] for entry in entries}, {first_path.stem, second_path.stem})
        self.assertEqual(first_path.read_bytes(), first_bytes)
        self.assertEqual(second_path.read_bytes(), second_bytes)
        self.assertFalse((Path(self.two['home']) / 'sessions' / first_path.name).exists())
        self.assertTrue(all(entry['projectionThreadId'] != entry['threadId'] for entry in entries))

    def test_packaged_login_viewer_and_old_runtime_keep_their_existing_mode(self):
        def unwanted(*args, **kwargs): raise AssertionError('No record scan should run for this mode.')
        self.refresh.ensure = unwanted
        for profile, capabilities in [({**self.one, 'runtime_channel': 'packaged'}, self.capabilities),
                ({**self.one, 'view_only': True}, self.capabilities), (self.one, {})]:
            self.assertEqual(environment(self.store, profile, capabilities, self.refresh), {})

    def test_unrelated_or_replaced_catalog_path_is_rejected(self):
        outside = self.root / 'different-catalog.json'
        outside.write_text('{}', encoding='utf-8')
        self.refresh.ensure = lambda *args, **kwargs: {'path': str(outside)}
        with self.assertRaises(RuntimeError): environment(self.store, self.one, self.capabilities, self.refresh)


if __name__ == '__main__': unittest.main()
