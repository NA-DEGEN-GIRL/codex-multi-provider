import concurrent.futures
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from control_center import ControlCenter
from manager_core.store import Store


class ProfileOrderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.profiles = [self.store.add_profile(alias) for alias in ('04', '02', '03', '01')]
        self.ids = [p['id'] for p in self.profiles]

    def order(self, store=None):
        return [p['id'] for p in (store or self.store).read()['profiles']]

    def test_move_via_command_persists_after_reopening_without_account_adapter(self):
        center = ControlCenter.__new__(ControlCenter)
        center.store = self.store
        before = self.store.read()
        result = center.dispatch('profile.move', dict(profile_id=self.ids[3], target_profile_id=self.ids[0], position='before'))
        expected = [self.ids[3], *self.ids[:3]]
        self.assertEqual(result['profile_ids'], expected)
        reopened = Store(self.temp.name)
        self.assertEqual(self.order(reopened), expected)
        after = reopened.read()
        self.assertEqual({p['id']: p for p in before['profiles']}, {p['id']: p for p in after['profiles']})
        self.assertEqual(before['representative_profile_id'], after['representative_profile_id'])
        self.assertEqual(before['sources'], after['sources'])

    def test_downward_move_and_new_account_append(self):
        self.store.move_profile(self.ids[0], self.ids[2], 'after')
        added = self.store.add_profile('API', external_model_id=str(uuid4()))
        self.assertEqual(self.order(), [self.ids[1], self.ids[2], self.ids[0], self.ids[3], added['id']])

    def test_invalid_or_removed_target_is_atomic(self):
        for source, target, position in [(self.ids[0], str(uuid4()), 'before'), (self.ids[0], self.ids[1], 'invalid'), ('invalid', self.ids[0], 'after')]:
            before = self.store.read()
            with self.assertRaises(ValueError):
                self.store.move_profile(source, target, position)
            self.assertEqual(self.store.read(), before)
        self.store.mutate(lambda data: self.store.profile(self.ids[1], data).update(removed_at='fixture'))
        before = self.store.read()
        with self.assertRaises(ValueError):
            self.store.move_profile(self.ids[0], self.ids[1], 'after')
        with self.assertRaises(ValueError):
            self.store.move_profile(self.ids[1], self.ids[0], 'before')
        self.assertEqual(self.store.read(), before)

    def test_removed_profiles_are_retained_and_restore_keeps_order(self):
        self.store.mutate(lambda data: self.store.profile(self.ids[1], data).update(removed_at='fixture'))
        result = self.store.move_profile(self.ids[3], self.ids[0], 'before')
        self.assertNotIn(self.ids[1], result['profile_ids'])
        self.store.mutate(lambda data: self.store.profile(self.ids[1], data).pop('removed_at'))
        self.assertEqual(self.order(), [self.ids[3], *self.ids[:3]])

    def test_concurrent_add_and_account_updates_are_not_lost(self):
        def move():
            return Store(self.temp.name).move_profile(self.ids[3], self.ids[0], 'before')
        def update():
            store = Store(self.temp.name)
            store.mutate(lambda data: store.profile(self.ids[2], data).update(login_state='verified', process_id=123))
            return store.add_profile('new')['id']
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            moving, updating = pool.submit(move), pool.submit(update)
            moving.result(); added = updating.result()
        self.assertEqual(self.order(), [self.ids[3], *self.ids[:3], added])
        self.assertEqual(self.store.profile(self.ids[2])['login_state'], 'verified')
        self.assertEqual(self.store.profile(self.ids[2])['process_id'], 123)


if __name__ == '__main__':
    unittest.main()
