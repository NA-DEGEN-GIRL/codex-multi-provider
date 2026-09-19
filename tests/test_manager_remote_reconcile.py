"""Background host preparation must not publish over newer foreground choices."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from control_center import ControlCenter
from manager_core.store import Store


class RemoteReconcileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.center = ControlCenter.__new__(ControlCenter)
        self.center.store = Store(temporary.name)
        self.profile = self.center.store.add_profile('fixture')
        self.pid = self.profile['id']
        self.binding = dict(alias='dev', profile_id=self.pid, revision='a' * 64, prepared=True)
        self.center.store.mutate(lambda d: self.center.store.profile(self.pid, d).update(
            generation='old-generation', remote_bindings=[deepcopy(self.binding)]))
        self.center.remote = Mock()
        self.center.store.remote_source = Mock()
        self.result = dict(self.binding, revision='b' * 64)
        self.center.remote.prepare.return_value = self.result

    def test_unchanged_profile_publishes_prepared_revision(self):
        self.center._reconcile_remote_hosts()
        self.assertEqual(self.center.store.profile(self.pid)['remote_bindings'], [self.result])
        self.center.store.remote_source.assert_called_once()

    def test_new_generation_policy_or_foreground_binding_survives_late_preparation(self):
        changes = [dict(generation='next-generation'),
                   dict(policy={**self.profile['policy'], 'selection_mode': 'external_only'}),
                   dict(remote_bindings=[dict(self.binding, revision='c' * 64)])]
        for change in changes:
            with self.subTest(change=change):
                self.center.store.mutate(lambda d: self.center.store.profile(self.pid, d).update(
                    generation='old-generation', policy=deepcopy(self.profile['policy']), remote_bindings=[deepcopy(self.binding)]))
                def prepare(*args, **kwargs):
                    self.center.store.mutate(lambda d: self.center.store.profile(self.pid, d).update(change))
                    return self.result
                self.center.remote.prepare.side_effect = prepare
                self.center._reconcile_remote_hosts()
                current = self.center.store.profile(self.pid)
                self.assertEqual(current['remote_bindings'], change.get('remote_bindings', [self.binding]))
        self.center.store.remote_source.assert_not_called()


if __name__ == '__main__':
    unittest.main()
