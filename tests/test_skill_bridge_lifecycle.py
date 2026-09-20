import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.skill_bridge import SkillBridge, Tunnel, projection


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.row = dict(id='fixture', name='3d-assets', enabled=True)
        self.store = SimpleNamespace(directory=self.root / 'data', read=lambda: {'profiles': []})
        self.personal = SimpleNamespace(source=self.root / 'source', list=lambda: {'skills': [dict(self.row)]})
        self.remote = Mock()
        self.bridge = SkillBridge(self.root, self.store, self.personal, self.remote)
        self.inv = patch('manager_core.skill_bridge.inventory', return_value=[self.row])
        self.inv.start(); self.addCleanup(self.inv.stop)
        self.reg = patch('manager_core.skill_bridge.registrations', return_value=[])
        self.reg.start(); self.addCleanup(self.reg.stop)

    def test_disabled_start_does_not_open_ports_or_touch_ssh(self):
        self.bridge.reconcile()
        self.assertIsNone(self.bridge.server)
        self.remote.assert_not_called()
        self.assertFalse(self.bridge.path.exists())

    def test_toggle_persists_separate_preference_without_network_and_survives_recreation(self):
        result = self.bridge.set('fixture', True)
        self.assertTrue(result['skills'][0]['bridge']['enabled'])
        self.assertTrue(result['skills'][0]['enabled'])
        self.assertIsNone(self.bridge.server)
        other = SkillBridge(self.root, self.store, self.personal, self.remote)
        self.assertTrue(other.decorate(self.personal.list())['skills'][0]['bridge']['enabled'])
        self.assertEqual(self.remote.mock_calls, [])

    def test_common_disable_revokes_execution_even_without_tunnels(self):
        self.bridge.configure(['3d-assets'])
        self.bridge.server = Mock()
        self.bridge.reconcile()
        self.bridge.server.update_registrations.assert_called_once_with([])
        self.bridge.server.update_tokens.assert_called_once_with({})

    def test_unknown_skill_and_nonboolean_toggle_rejected(self):
        with self.assertRaises(ValueError): self.bridge.set('missing', True)
        with self.assertRaises(ValueError): self.bridge.set('fixture', 'yes')
        self.assertFalse(self.bridge.path.exists())

    def test_remote_failure_does_not_block_manager_or_disable_local_skill(self):
        self.bridge.status['host-fixture'] = dict(status='error', installed=[], message='SSH unavailable')
        self.bridge.configure(['3d-assets'])
        row = self.bridge.decorate(self.personal.list())['skills'][0]
        self.assertTrue(row['enabled'])
        self.assertEqual(row['bridge']['message'], 'SSH unavailable')

    def test_refresh_revokes_queued_commands_immediately(self):
        self.bridge.server = Mock()
        self.bridge.refresh()
        self.bridge.server.update_registrations.assert_called_once_with([])
        self.assertTrue(self.bridge.wake.is_set())

    def test_projection_preserves_docs_layout_and_excludes_credentials_and_models(self):
        root = self.root / 'runtime'; skill = root / '.agents/skills/3d-assets'
        for path, data in {
            skill / 'SKILL.md': b'---\nname: 3d-assets\n---\n',
            skill / 'references/runtime.md': b'../../../../docs/BLENDER.md',
            skill / 'scripts/assetctl.py': b'print("fixture")',
            root / 'docs/BLENDER.md': b'Blender fixture',
            root / '.secrets/key.txt': b'private-fixture',
            root / '.runtime/model.bin': b'weights',
        }.items():
            path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
        files = projection([dict(name='3d-assets', root=str(root), skill_dir=str(skill))])
        self.assertEqual(len(files), 4)
        self.assertEqual(base64.b64decode(files['3d-assets/docs/BLENDER.md']), b'Blender fixture')
        self.assertFalse(any('.secrets' in p or '.runtime' in p for p in files))

    def wait_for(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail('Timed out waiting for the isolated fixture.')
            time.sleep(.01)

    def stalled_tunnel(self):
        # This local fixture deliberately never consumes stdin. No SSH client,
        # account, host, or paid service is involved.
        real_popen = subprocess.Popen
        script = ('import sys,time; '
                  'print("Allocated port 43210 for remote forward", file=sys.stderr, flush=True); '
                  'time.sleep(60)')
        processes = []

        def start_fixture(argv, **kwargs):
            process = real_popen([sys.executable, '-u', '-c', script], **kwargs)
            processes.append(process)
            return process

        def cleanup():
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=3)

        self.addCleanup(cleanup)
        remote = SimpleNamespace(_alias=lambda alias: alias, ssh='unused-fixture-ssh',
                                 ssh_config='unused-fixture-config')
        with patch('manager_core.skill_bridge.subprocess.Popen', side_effect=start_fixture):
            tunnel = Tunnel(remote, 'isolated', '/usr/bin/python3', 54321, '# fixture', self.bridge.stopping)
        self.addCleanup(tunnel.close)
        return tunnel

    def test_shutdown_unblocks_stalled_sender_and_readers_before_stream_cleanup(self):
        tunnel = self.stalled_tunnel()
        errors = []

        def update():
            try:
                tunnel.update({'fixture': 'x' * (2 * 1024 * 1024)})
            except ValueError as error:
                errors.append(str(error))

        self.bridge.tunnels['isolated'] = tunnel
        self.bridge.worker = threading.Thread(target=update, daemon=True)
        self.bridge.worker.start()
        self.wait_for(lambda: tunnel._sender is not None and tunnel._sender.is_alive())
        started = time.monotonic()
        self.bridge.shutdown()
        self.assertLess(time.monotonic() - started, 4)
        self.assertIsNotNone(tunnel.process.poll())
        self.assertFalse(self.bridge.worker.is_alive())
        self.assertTrue(errors)
        self.assertFalse(any(thread.is_alive() for thread in tunnel._io_threads))
        self.assertTrue(all(stream.closed for stream in
                            (tunnel.process.stdin, tunnel.process.stdout, tunnel.process.stderr)))

    def test_send_timeout_ends_owned_transport_without_abandoned_io_threads(self):
        tunnel = self.stalled_tunnel()
        started = time.monotonic()
        with patch('manager_core.skill_bridge._TUNNEL_IO_TIMEOUT', .05):
            with self.assertRaises(ValueError):
                tunnel.update({'fixture': 'x' * (2 * 1024 * 1024)})
        self.assertLess(time.monotonic() - started, 4)
        self.assertIsNotNone(tunnel.process.poll())
        self.assertTrue(tunnel.closed.is_set())
        self.assertFalse(any(thread.is_alive() for thread in tunnel._io_threads))

    def test_removed_host_status_mutation_holds_catalog_lock_but_close_does_not(self):
        self.bridge.configure(['3d-assets'])
        self.bridge.server = Mock()
        self.bridge.server.start.return_value = {'port': 43210}
        lifecycle = self.bridge
        checked = []

        class GuardedStatus(dict):
            def pop(inner, key, *default):
                if key == 'removed-host':
                    self.assertTrue(lifecycle.lock._is_owned(), 'Catalog iteration and removal must share the lock.')
                    checked.append(key)
                return super().pop(key, *default)

        def close():
            self.assertFalse(lifecycle.lock._is_owned(), 'Transport cleanup must not block the catalog lock.')

        self.bridge.status = GuardedStatus({'removed-host': dict(status='error', message='obsolete error')})
        self.bridge.tunnels['removed-host'] = SimpleNamespace(close=close)
        self.bridge.retry_at['removed-host'] = time.monotonic() + 90
        helper = self.root / 'fixture-helper.py'
        helper.write_text('# fixture', encoding='utf-8')
        with patch('manager_core.skill_bridge.script_path', return_value=helper):
            self.bridge.reconcile()
        self.assertEqual(checked, ['removed-host'])
        self.assertEqual(self.bridge.decorate(self.personal.list())['bridge_hosts'], [])
        self.assertNotIn('removed-host', self.bridge.retry_at)
        self.assertEqual(self.bridge.tunnels, {})

    def test_successful_worker_reconciliation_clears_previous_workspace_error(self):
        self.bridge.configure(['3d-assets'])
        attempts = []

        def reconcile():
            attempts.append(1)
            if len(attempts) == 1:
                raise ValueError('Temporary fixture failure')
            with self.bridge.lock:
                self.bridge.status['recovered-host'] = dict(status='ready', installed=['3d-assets'])

        self.addCleanup(self.bridge.shutdown)
        with patch.object(self.bridge, 'reconcile', side_effect=reconcile):
            self.bridge.start()
            self.wait_for(lambda: 'workspace' in self.bridge.status)
            self.assertEqual(self.bridge.decorate(self.personal.list())['skills'][0]['bridge']['message'],
                             'Temporary fixture failure')
            self.bridge.wake.set()
            self.wait_for(lambda: len(attempts) >= 2 and 'workspace' not in self.bridge.status)
            row = self.bridge.decorate(self.personal.list())['skills'][0]['bridge']
            self.assertEqual(row['status'], 'ready')
            self.assertNotIn('Temporary fixture failure', row['message'])
            self.bridge.shutdown()


if __name__ == '__main__':
    unittest.main()
