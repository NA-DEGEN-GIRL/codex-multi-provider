"""Background host preparation must not publish over newer foreground choices."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from control_center import ControlCenter
from manager_core.store import Store
from manager_core.ssh_inventory import SshInventory
from manager_core.updates import UpdateError


class RemoteReconcileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.center = ControlCenter.__new__(ControlCenter)
        self.center.store = Store(temporary.name)
        self.center.ssh_inventory = SshInventory(temporary.name, identity=lambda _: {})
        self.profile = self.center.store.add_profile('fixture')
        self.pid = self.profile['id']
        self.binding = dict(alias='dev', profile_id=self.pid, revision='a' * 64, prepared=True)
        self.generation = str(uuid4())
        self.center.store.mutate(lambda d: self.center.store.profile(self.pid, d).update(
            generation=self.generation, remote_bindings=[deepcopy(self.binding)]))
        self.center.remote = Mock()
        self.center.store.remote_source = Mock()
        self.result = dict(self.binding, revision='b' * 64)
        self.center.remote.prepare.return_value = self.result

    def test_unchanged_profile_publishes_prepared_revision(self):
        self.center._reconcile_remote_hosts()
        self.assertEqual(self.center.store.profile(self.pid)['remote_bindings'], [self.result])
        self.center.store.remote_source.assert_called_once()

    def test_new_generation_policy_or_foreground_binding_survives_late_preparation(self):
        changes = [dict(generation=str(uuid4())),
                   dict(policy={**self.profile['policy'], 'selection_mode': 'external_only'}),
                   dict(remote_bindings=[dict(self.binding, revision='c' * 64)])]
        for change in changes:
            with self.subTest(change=change):
                self.center.store.mutate(lambda d: self.center.store.profile(self.pid, d).update(
                    generation=self.generation, policy=deepcopy(self.profile['policy']), remote_bindings=[deepcopy(self.binding)]))
                def prepare(*args, **kwargs):
                    self.center.store.mutate(lambda d: self.center.store.profile(self.pid, d).update(change))
                    return self.result
                self.center.remote.prepare.side_effect = prepare
                self.center._reconcile_remote_hosts()
                current = self.center.store.profile(self.pid)
                self.assertEqual(current['remote_bindings'], change.get('remote_bindings', [self.binding]))
        self.center.store.remote_source.assert_not_called()

    def test_preparation_is_enrolled_before_network_io_and_fenced_after_gate(self):
        def prepared(*args, **kwargs):
            inventory = self.center.store.read()['ssh_inventory'][self.pid]
            self.assertEqual([v['operation'] for v in inventory['operations'].values()], ['prepare'])
            self.center.store.mutate(lambda data: data.setdefault('ssh_maintenance', {}).update({
                self.pid: dict(state='held', transaction_id=str(uuid4()))}))
            return self.result
        self.center.remote.prepare.side_effect = prepared
        self.center._reconcile_remote_hosts()
        self.assertEqual(self.center.store.profile(self.pid)['remote_bindings'], [self.binding])
        self.assertEqual(self.center.store.read()['ssh_inventory'][self.pid]['operations'], {})
        self.center.remote.prepare.reset_mock()
        profile = self.center.store.profile(self.pid)
        with self.assertRaises(UpdateError):
            self.center._prepare_remote(profile, 'dev', [])
        self.center.remote.prepare.assert_not_called()


if __name__ == '__main__':
    unittest.main()
