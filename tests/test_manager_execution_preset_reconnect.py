"""Reconnect publishes offline choices synchronously, with no runtime lifecycle."""
from contextlib import nullcontext
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from manager_core.execution_preset_reconnect import publish_before_connect
from manager_core.execution_presets import ExecutionPresets
from manager_core.providers import ProviderRegistry
from manager_core.remote import RemoteError, RemoteManager
from manager_core.store import Store, atomic_json
from test_manager_remote import helper

REMOTE = helper('execution_presets')


class ReconnectPresetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root)
        self.profile = self.store.add_profile('Owner')
        self.profile_id = self.profile['id']
        self.generation = str(uuid4())
        self.revision = 'a' * 64
        self.binding = dict(profile_id=self.profile_id, alias='dev', revision=self.revision,
            remote_python='/usr/bin/python3', prepared=True, host_identity='b' * 64,
            remote_launcher='/home/test/.local/share/codex-control-center/profiles/' + self.profile_id + '/launch.py',
            execution_presets_version=1)
        self.store.mutate(lambda data: self.store.profile(self.profile_id, data).update(
            generation=self.generation, auth_mode='native', account_fingerprint='c' * 64, remote_bindings=[self.binding]))
        self.binding_path = self.store.directory / 'profiles' / self.profile_id / 'ssh-bindings.json'
        atomic_json(self.binding_path, dict(profile_id=self.profile_id, generation=self.generation, bindings=[self.binding]))
        self.environment = dict(CODEX_MANAGER_ROOT=str(self.root), CODEX_MANAGER_GENERATION=self.generation)
        self.event = dict(operation='native-proxy', profile_id=self.profile_id, alias='dev', revision=self.revision)

    def test_old_runtimes_and_non_native_connections_skip_publication(self):
        with patch('manager_core.execution_preset_reconnect.RemoteManager') as factory:
            for environment, event in (({}, self.event), (self.environment, {**self.event, 'operation': 'local-forward'})):
                self.assertEqual(publish_before_connect(environment, event)['state'], 'not_required')
            self.store.mutate(lambda data: self.store.profile(self.profile_id, data)['remote_bindings'][0].pop('execution_presets_version'))
            self.assertEqual(publish_before_connect(self.environment, self.event)['state'], 'not_required')
            factory.assert_not_called()

    def test_metadata_uses_validated_openssh_not_the_desktop_adapter_on_path(self):
        with patch('manager_core.execution_preset_reconnect.RemoteManager') as factory:
            factory.return_value.publish_execution_presets.return_value = {
                'execution_presets_version': 1, 'published': True}
            result = publish_before_connect(self.environment, self.event,
                ssh_executable=r'C:\Windows\System32\OpenSSH\ssh.exe')
            self.assertEqual(result['state'], 'published')
            factory.assert_called_once_with(str(self.root),
                ssh_executable=r'C:\Windows\System32\OpenSSH\ssh.exe')

    def test_changed_generation_or_definition_prevents_ssh_publication(self):
        with patch('manager_core.execution_preset_reconnect.RemoteManager') as factory:
            for event, environment in ((self.event, {**self.environment, 'CODEX_MANAGER_GENERATION': str(uuid4())}),
                                       ({**self.event, 'revision': 'c' * 64}, self.environment)):
                with self.subTest(event=event), self.assertRaises(RemoteError) as raised:
                    publish_before_connect(environment, event)
                self.assertEqual(raised.exception.code, 'ssh_preset_sync_required')
            factory.assert_not_called()

    def test_publication_failure_blocks_only_this_connection_without_retry(self):
        with patch('manager_core.execution_preset_reconnect.RemoteManager') as factory:
            factory.return_value.publish_execution_presets.side_effect = RemoteError('ssh_failed', 'private remote output')
            with self.assertRaises(RemoteError) as raised:
                publish_before_connect(self.environment, self.event)
            self.assertNotIn('private', str(raised.exception))
            factory.return_value.publish_execution_presets.assert_called_once()
            self.assertEqual(self.store.profile(self.profile_id)['generation'], self.generation)

    def test_generation_change_during_publication_does_not_continue_handshake(self):
        with patch('manager_core.execution_preset_reconnect.RemoteManager') as factory:
            def publish(*_):
                self.store.mutate(lambda data: self.store.profile(self.profile_id, data).update(generation=str(uuid4())))
                return dict(published=True, execution_presets_version=1)
            factory.return_value.publish_execution_presets.side_effect = publish
            with self.assertRaises(RemoteError):
                publish_before_connect(self.environment, self.event)
            factory.return_value.publish_execution_presets.assert_called_once()

    def test_unprepared_independent_role_does_not_block_ready_tasks_connecting(self):
        with patch('manager_core.execution_preset_reconnect.RemoteManager') as factory:
            factory.return_value.publish_execution_presets.return_value = dict(
                published=True, execution_presets_version=1, runtime_prepare_required=True,
                unprepared_presets=[dict(id=str(uuid4()), revision=1)])
            self.assertEqual(publish_before_connect(self.environment, self.event),
                             dict(state='published', runtime_prepare_required=True))

    def test_offline_default_and_cold_task_choices_reconnect_to_latest_without_clobbering_newer_write(self):
        registry = ProviderRegistry(self.root)
        presets = ExecutionPresets(self.store, registry)
        first = presets.save(self.profile_id, dict(name='First', roles=[]))
        second = presets.save(self.profile_id, dict(name='Second', roles=[]))
        presets.set_default(self.profile_id, first['id'], 1)
        remote_home = self.binding['remote_launcher'].removesuffix('/launch.py') + '/codex'
        base = registry.render_for_host(remote_home, False, [])['files']['config.toml']
        rendered = presets.render_for_host(self.profile_id, base, host_id='ssh:dev', config_home=remote_home,
            remote_python=self.binding['remote_python'], host_identity=self.binding['host_identity'])
        profile = self.root / 'simulated-remote/profiles' / self.profile_id
        definition = profile / 'definitions' / self.revision
        definition.mkdir(parents=True)
        atomic_json(definition / REMOTE.AUTHORITY, rendered['authority'])
        descriptor = dict(profile_id=self.profile_id, revision=self.revision,
                          definition=str(definition), host_identity=self.binding['host_identity'])
        requests = []
        def ssh(args, **options):
            request = json.loads(options['input'])
            requests.append(deepcopy(request))
            result = REMOTE.publish(profile, self.revision, request)
            return subprocess.CompletedProcess(args, 0, json.dumps(result).encode(), b'')
        config = self.root / 'ssh-config'
        config.write_text('Host dev\n HostName example.invalid\n')
        manager = RemoteManager(self.root, registry=registry, ssh_config=config, runner=ssh, ssh_executable='ssh.exe')
        atomic_json(manager._authority_path(self.profile_id, 'dev', self.revision), dict(rendered['authority'],
            revision=self.revision, host_identity=self.binding['host_identity']))
        controller = SimpleNamespace(_descriptor=lambda *args: descriptor)
        with patch.dict(sys.modules, native_controller=controller), patch.object(REMOTE, '_lock', lambda p: nullcontext()), patch(
                'manager_core.execution_preset_reconnect.RemoteManager', return_value=manager):
            publish_before_connect(self.environment, self.event)
            path = REMOTE.runtime_path(profile, self.revision)
            self.assertEqual(json.loads(path.read_text())['default_preset']['id'], first['id'])
            stale_request = deepcopy(requests[-1])
            # No live transport exists while these changes are saved.
            presets.set_default(self.profile_id, second['id'], 1)
            presets.bind(self.profile_id, 'ssh:dev', 'cold-task', second['id'], 1)
            presets.bind(self.profile_id, 'ssh:dev', 'explicit-clear', None)
            self.assertEqual(len(requests), 1)
            self.assertEqual(publish_before_connect(self.environment, self.event)['state'], 'published')
            applied = json.loads(path.read_text())
            self.assertEqual(applied['default_preset']['id'], second['id'])
            self.assertEqual(applied['task_bindings']['cold-task']['id'], second['id'])
            self.assertIsNone(applied['task_bindings']['explicit-clear'])
            before = path.read_bytes()
            self.assertEqual(publish_before_connect(self.environment, self.event)['state'], 'published')
            self.assertEqual(path.read_bytes(), before)
            with self.assertRaisesRegex(ValueError, 'stale'):
                REMOTE.publish(profile, self.revision, stale_request)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(self.store.profile(self.profile_id)['generation'], self.generation)


if __name__ == '__main__':
    unittest.main()
