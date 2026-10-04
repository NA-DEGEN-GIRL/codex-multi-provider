"""Legacy and SSH-capable Claude lifecycle boundaries; synthetic profile data."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.app_preferences import prepare
from manager_core.remote_maintenance import RemoteMaintenance
from manager_core.ssh_auto_prepare import ensure_binding
from manager_core.ssh_inventory import SshInventory
from manager_core.ssh_shim import ShimError, native_bodies, native_command
from manager_core.store import Store, atomic_json
from manager_core.update_hooks import UpdateHooks
from manager_core.updates import UpdateError


class ClaudeLocalOnlyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root)
        profile = self.store.add_profile('Claude', claude_settings={})
        self.store.mutate(lambda data: self.store.profile(profile['id'], data).update(generation=str(uuid4())))
        self.profile = self.store.profile(profile['id'])
        self.inventory = SshInventory(self.root, identity=lambda _: {})
        self.remote = Mock()
        self.maintenance = RemoteMaintenance(self.root, self.store, self.remote)

    def test_local_preferences_suppress_connections_without_mutating_donor_or_peer(self):
        source, peer, target = (self.root / name for name in ('source', 'gpt-peer', 'claude-ui'))
        source.mkdir()
        donor = {
            'local-projects': {'local': dict(id='local', name='Local', rootPaths=[str(source)])},
            'codex-managed-remote-connections': [dict(hostId='remote-ssh-discovered:dev',
                displayName='Dev', source='discovered', alias='dev')],
            'remote-connection-auto-connect-by-host-id': {'remote-ssh-discovered:dev': True},
        }
        path = source / '.codex-global-state.json'
        atomic_json(path, donor)
        prepare(peer, source)
        prepare(target, source)
        peer_before, donor_before = (peer / path.name).read_bytes(), path.read_bytes()
        # A stale queued SSH repair must not restore remote connections for this backend.
        recovery = target / '.manager-ssh-connection-recovery.json'
        recovery.write_text('stale unsupported repair', encoding='utf-8')
        for _ in range(2):
            result = prepare(target, source, allow_remote_connections=False)
            current = json.loads((target / path.name).read_text(encoding='utf-8'))
            self.assertEqual([], current['codex-managed-remote-connections'])
            self.assertEqual({}, current['remote-connection-auto-connect-by-host-id'])
            self.assertIn('local', current['local-projects'])
            self.assertEqual(0, result['workspace']['ssh_connections'])
            metadata = json.loads((target / '.manager-app-preferences.json').read_text(encoding='utf-8'))
            # The live desktop healer only restores aliases present in this map.
            self.assertEqual({}, metadata['workspace']['codex-managed-remote-connections'])
        self.assertEqual(donor_before, path.read_bytes())
        self.assertEqual(peer_before, (peer / path.name).read_bytes())
        self.assertEqual('stale unsupported repair', recovery.read_text(encoding='utf-8'))
        self.assertTrue(json.loads(peer_before)['remote-connection-auto-connect-by-host-id']['remote-ssh-discovered:dev'])

    def test_coverage_is_local_without_an_ssh_manifest_and_preserves_peer_inventory(self):
        peer = self.store.add_profile('API peer', external_model_id=str(uuid4()))
        peer['generation'] = str(uuid4())
        self.inventory.prepare(peer['id'], peer['generation'])
        with self.inventory.execution(peer['id'], peer['generation'], dict(operation='native-proxy', alias='dev')):
            pass
        before = self.store.path.read_bytes()
        coverage = self.inventory.coverage(self.profile)
        self.assertEqual(['local'], coverage['hosts'])
        self.assertTrue(coverage['complete'])
        self.assertTrue(coverage['maintenance_complete'])
        self.assertEqual(self.profile['generation'], coverage['generation'])
        self.assertEqual([], self.maintenance.snapshot(self.profile, coverage))
        self.assertFalse(self.maintenance.pending_on_open(self.profile))
        self.assertEqual(before, self.store.path.read_bytes())
        self.assertEqual(['local', 'dev'], self.inventory.coverage(peer)['hosts'])
        self.remote.assert_not_called()

    def test_unexpected_remote_evidence_is_not_falsely_certified_or_erased(self):
        self.store.mutate(lambda data: data.setdefault('ssh_inventory', {}).update({self.profile['id']:
            dict(generation=self.profile['generation'], hosts=['legacy'], operations={}, unclassified=False)}))
        before = self.store.path.read_bytes()
        coverage = self.inventory.coverage(self.profile)
        self.assertFalse(coverage['complete'])
        self.assertFalse(coverage['maintenance_complete'])
        self.assertEqual(['local', 'legacy'], coverage['hosts'])
        with self.assertRaises(UpdateError):
            self.maintenance.snapshot(self.profile, coverage)
        self.assertEqual(before, self.store.path.read_bytes())

    def test_automatic_prepare_and_direct_enrollment_fail_before_state_or_network_changes(self):
        profile = self.profile
        path = self.store.directory / 'profiles' / profile['id'] / 'ssh-bindings.json'
        manifest = dict(profile_id=profile['id'], generation=profile['generation'], inventory_root=str(self.root),
            auto_prepare_aliases=['dev'], native_compatible=True, native_cli='codex', selected_model_ids=[])
        atomic_json(path, manifest)
        before, manifest_before = self.store.path.read_bytes(), path.read_bytes()
        args = ['-T', 'dev', native_command(native_bodies()['native-probe'], b'01234567')]
        with self.assertRaises(ShimError) as raised:
            ensure_binding(args, manifest, path, remote=self.remote)
        self.assertEqual('claude_remote_runtime_required', raised.exception.code)
        with self.assertRaises(UpdateError) as raised:
            with self.inventory.execution(profile['id'], profile['generation'], dict(operation='native-start', alias='dev')):
                self.fail('Claude must not enroll SSH work')
        self.assertEqual('claude_remote_runtime_required', raised.exception.code)
        self.assertEqual(before, self.store.path.read_bytes())
        self.assertEqual(manifest_before, path.read_bytes())
        self.remote.prepare.assert_not_called()

    def test_explicit_remote_lifecycle_rejects_before_gates_or_network(self):
        instances = Mock()
        hooks = UpdateHooks(self.root, self.store, instances, host_inventory=self.inventory.coverage,
                            remote_maintenance=self.maintenance)
        before = self.store.path.read_bytes()
        with self.assertRaises(UpdateError) as raised:
            hooks.begin_remote_reconcile(self.profile['id'], graceful_drain=True)
        self.assertEqual('claude_remote_runtime_required', raised.exception.code)
        with self.assertRaises(UpdateError):
            self.maintenance.prepare_and_start(self.profile, [{'state': 'closed'}], Mock())
        self.assertFalse(self.maintenance.verify_settings(self.profile, {}))
        self.assertEqual(before, self.store.path.read_bytes())
        self.remote.prepare.assert_not_called()
        instances.show.assert_not_called()

    def test_supported_claude_prepare_uses_registered_account_and_tracked_generation(self):
        from control_center import ControlCenter
        from manager_core.model_settings import render_options
        center = ControlCenter.__new__(ControlCenter)
        center.root, center.store = self.root, self.store
        center.ssh_inventory, center.remote = self.inventory, self.remote
        self.remote.prepare.return_value = {'prepared': True}
        with patch('manager_core.remote.supports_remote_claude', return_value=True):
            # Installing a bundle cannot certify an old desktop's missing SSH adapter.
            with self.assertRaises(UpdateError) as error:
                center._prepare_remote(self.profile, 'dev', [])
            self.assertEqual('claude_remote_runtime_required', error.exception.code)
            self.remote.prepare.assert_not_called()
            self.inventory.prepare(self.profile['id'], self.profile['generation'])
            self.assertEqual({'prepared': True}, center._prepare_remote(self.profile, 'dev', []))
        self.remote.prepare.assert_called_once_with('dev', self.profile['id'], self.profile['home'], [],
                                                    **render_options(self.profile))

    def test_reconciliation_checks_supported_claude_bindings_without_installing(self):
        from control_center import ControlCenter
        binding = dict(alias='dev', prepared=True)
        self.store.mutate(lambda data: self.store.profile(self.profile['id'], data).update(remote_bindings=[binding]))
        center = ControlCenter.__new__(ControlCenter)
        center.store, center.remote_maintenance = self.store, Mock()
        center._reconcile_remote_hosts()
        center.remote_maintenance.verify_settings.assert_called_once_with(self.store.profile(self.profile['id']), binding)
        self.remote.prepare.assert_not_called()

    def test_supported_claude_open_preserves_remote_maintenance(self):
        from control_center import ControlCenter
        center = ControlCenter.__new__(ControlCenter)
        center.root, center.store = self.root, self.store
        center.instances, center.restarts, center.remote_maintenance = Mock(), Mock(), Mock()
        center.instances.observe.return_value = {'status': 'exited'}
        center.remote_maintenance.pending_on_open.return_value = True
        with patch('manager_core.remote.supports_remote_claude', return_value=True):
            self.assertEqual(center.restarts.open_local.return_value,
                             center._open_profile_locally(self.profile['id']))
        center.restarts.open_local.assert_called_once_with(self.profile['id'])
        center.instances.show.assert_not_called()
        center.restarts.reset_mock()
        with patch('manager_core.remote.supports_remote_claude', return_value=False):
            center._open_profile_locally(self.profile['id'])
        center.instances.show.assert_called_once_with(self.profile['id'], reopen_existing=False)
        center.restarts.open_local.assert_not_called()

    def test_local_idle_checks_are_still_required_for_claude_restart(self):
        profile = self.profile
        health = dict(generation=profile['generation'], connected=True, initialized=True,
            streamComplete=True, accountReady=True, pendingMutationCount=0, pendingApprovalCount=0,
            activeProcessCount=0, activeToolCount=0, activeTurnCount=1, activeChildCount=0)
        admin = Mock()
        def request(method, params):
            if method == 'manager/maintenance/status':
                return deepcopy(health)
            if method == 'thread/loaded/list':
                return dict(data=[], nextCursor=None)
            self.fail('Unexpected native request: ' + method)
        admin.request.side_effect = request
        hooks = UpdateHooks(self.root, self.store, Mock(), host_inventory=self.inventory.coverage,
            remote_maintenance=self.maintenance, admin_factory=lambda _: admin)
        hooks._live = Mock(return_value={**profile, 'profile_id': profile['id'], 'process_id': 123})
        hooks._observer_ready = Mock(return_value=True)
        busy = hooks.snapshot_instances([profile['id']])[0]
        self.assertFalse(busy['idle_verified'])
        self.assertEqual('runtime_not_idle', busy['update_blocker'])
        health['activeTurnCount'] = 0
        idle = hooks.snapshot_instances([profile['id']])[0]
        self.assertTrue(idle['idle_verified'])
        self.assertEqual([], idle['remote_states'])
        hooks._observer_ready.return_value = False
        unready = hooks.snapshot_instances([profile['id']])[0]
        self.assertFalse(unready['idle_verified'])
        self.assertEqual('runtime_identity_not_ready', unready['update_blocker'])


if __name__ == '__main__':
    unittest.main()
