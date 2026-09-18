from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.accounts import Accounts
from manager_core.profile_lifecycle import ProfileLifecycle, account_alias
from manager_core.store import Store


class ProfileLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.profile = self.store.add_profile('04', str(uuid4()))
        self.instances = Mock()
        self.instances.observe.return_value = {'status': 'not_started'}
        self.lifecycle = ProfileLifecycle(self.store, self.instances)

    def tearDown(self):
        self.temp.cleanup()

    def test_remove_restore_preserves_identity_files_and_import_does_not_restore(self):
        home = Path(self.profile['home'])
        home.mkdir(parents=True)
        record = home / 'fixture-record.jsonl'
        record.write_bytes(b'persisted conversation fixture')
        auth = home / 'auth.json'
        auth.write_bytes(b'private fixture')
        sources = self.store.read()['sources']
        self.lifecycle.remove(self.profile['id'])
        Accounts._sync_accounts(self.store, [{'id': self.profile['usage_account_id'],
            'alias': '04', 'home': str(home), 'usage': {}}])
        self.assertTrue(self.store.profile(self.profile['id'])['removed_at'])
        self.assertEqual(self.store.read()['sources'], sources)
        self.lifecycle.restore(self.profile['id'])
        self.assertNotIn('removed_at', self.store.profile(self.profile['id']))
        self.assertEqual(auth.read_bytes(), b'private fixture')
        self.assertEqual(record.read_bytes(), b'persisted conversation fixture')

    def test_active_instance_and_existing_links_block_removal_without_changes(self):
        before = self.store.read()
        self.instances.observe.return_value = {'status': 'running'}
        with self.assertRaises(RuntimeError):
            self.lifecycle.remove(self.profile['id'])
        self.assertEqual(before, self.store.read())
        self.instances.observe.return_value = {'status': 'not_started'}
        self.store.shortcut_add('work', self.profile['id'], str(uuid4()), 'local', 'manager:' + self.profile['id'])
        before = self.store.read()
        with self.assertRaises(ValueError):
            self.lifecycle.remove(self.profile['id'])
        self.assertEqual(before, self.store.read())

    def test_removed_profile_cannot_receive_task_links_and_duplicate_alias_does_not_rebind(self):
        self.lifecycle.remove(self.profile['id'])
        with self.assertRaises(ValueError):
            self.store.shortcut_add('work', self.profile['id'], str(uuid4()), 'local', 'manager:' + self.profile['id'])
        other = self.store.add_profile('04')
        with self.assertRaises(ValueError):
            self.lifecycle.restore(self.profile['id'])
        self.lifecycle.restore(self.profile['id'], '04-old')
        self.assertEqual(self.store.profile(other['id'])['alias'], '04')

    def test_account_alias_is_compatible_with_ssh_and_unique(self):
        for value in ('04', 'x' * 41, '\n'):
            with self.assertRaises(ValueError):
                account_alias(value, self.store.read()['profiles'])
        self.assertEqual(account_alias(' 새로운 계정 ', self.store.read()['profiles']), '새로운 계정')

    def test_undo_waits_for_profile_restore_without_losing_deleted_link(self):
        link = self.store.shortcut_add('work', self.profile['id'], str(uuid4()), 'local', 'manager:' + self.profile['id'])
        self.store.shortcut_delete(link['id'])
        self.lifecycle.remove(self.profile['id'])
        before = self.store.read()
        with self.assertRaises(ValueError):
            self.store.shortcut_undo()
        self.assertEqual(self.store.read(), before)
        self.lifecycle.restore(self.profile['id'])
        self.assertEqual(self.store.shortcut_undo()['id'], link['id'])


if __name__ == '__main__':
    unittest.main()
