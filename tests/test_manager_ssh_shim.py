"""Native fixtures are extracted from the installed JS, not the shim implementation."""
from __future__ import annotations

import json
from contextlib import nullcontext
from io import StringIO
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.ssh_shim import (ADAPTER_VERSION, MarkerGate, ShimError, decode_native, main,
                                   native_bodies, native_command, parse_invocation,
                                   prepare_environment, quote_always, route_arguments,
                                   validate_binding)
from manager_core.updates import UpdateError


PROFILE = '00000000-0000-4000-9000-000000000007'
OTHER = '00000000-0000-4000-9000-000000000008'
FIXTURE = json.loads((Path(__file__).parent / 'fixtures/native_ssh_26_903_9818.json').read_text())
OPERATIONS = {
    'codex_path_probe': 'native-probe', 'codex_version_probe': 'native-version',
    'app_server_bootstrap': 'native-start', 'remote_codex_kill': 'native-stop',
}


def binding(profile=PROFILE):
    return {'alias': 'remote-dev', 'profile_id': profile, 'prepared': True,
            'revision': 'a' * 64, 'remote_python': '/usr/bin/python3',
            'remote_launcher': '/home/test/.local/share/codex-control-center/profiles/' + profile + '/launch.py'}


def manifest(profile=PROFILE):
    return {'schema': 1, 'adapter': ADAPTER_VERSION, 'profile_id': profile,
            'native_cli': 'codex', 'bindings': [binding(profile)]}


def unwrap(command):
    return shlex.split(command)[4]


class RaisingAdmission:
    """SshInventory.execution stand-in that fails when its context is entered."""

    def __init__(self, error):
        self.error = error

    def __enter__(self):
        raise self.error

    def __exit__(self, *exception):
        return False


class NativeFixtureTests(unittest.TestCase):
    def test_waiting_connection_routes_the_new_published_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            executable=Path(directory)/'ssh.exe'
            executable.write_bytes(b'fixture')
            old=manifest()
            old.update(generation=OTHER,inventory_root=directory,real_ssh=str(executable))
            new=json.loads(json.dumps(old))
            new['bindings'][0]['revision']='b'*64
            path=Path(directory)/'ssh-bindings.json'
            with patch('manager_core.ssh_shim._load_manifest',side_effect=[(old,path),(new,path)]), \
                 patch('manager_core.ssh_connection_wait.wait_for_settings') as wait, \
                 patch('manager_core.ssh_shim._audit'), \
                 patch('manager_core.ssh_inventory.SshInventory.execution',return_value=nullcontext()), \
                 patch('manager_core.ssh_shim._execute',return_value=0) as execute:
                self.assertEqual(main(['remote-dev',FIXTURE['wrapped']['app_server_bootstrap']]),0)
            wait.assert_called_once()
            self.assertEqual(execute.call_args.args[-1]['revision'],'b'*64)
            self.assertNotIn('a'*64,execute.call_args.args[1][-1])

    def test_updated_installed_app_routes_exact_captured_start_and_proxy(self):
        for version in ('26_908_4834', '26_911_7940'):
            fixture=json.loads((Path(__file__).parent/f'fixtures/native_ssh_{version}.json').read_text(encoding='utf-8-sig'))
            for name,command in fixture['commands'].items():
                rewritten,event=route_arguments(['-T','remote-dev',command],manifest())
                self.assertEqual(event['operation'],'native-'+name)
                self.assertEqual(rewritten[:-1],['-T','remote-dev'])
                marker=''.join('\\'+format(c,'03o') for c in fixture['marker'])
                self.assertIn(quote_always(marker),unwrap(rewritten[-1]))

    def test_fixture_provenance(self):
        self.assertEqual(FIXTURE['source_sha256'], '471f06dfcda15de10196f701504244c6f412d7ed401c155efc56a427a89a3195')

    def test_all_installed_management_operations_are_routed(self):
        cases = [(operation, FIXTURE['wrapped'][name]) for name, operation in OPERATIONS.items()]
        cases.append(('native-proxy', FIXTURE['proxyWrapped']))
        for operation, command in cases:
            with self.subTest(operation=operation):
                prefix = ['-T', '-v', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15',
                          '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=12', 'remote-dev']
                rewritten, event = route_arguments(prefix + [command], manifest())
                self.assertEqual(rewritten[:-1], prefix)
                self.assertEqual(event['operation'], operation)
                source = decode_native(command)
                self.assertTrue(unwrap(rewritten[-1]).startswith(source[1]))
                self.assertEqual(shlex.split(rewritten[-1])[2], shlex.split(command)[2])
                payload = unwrap(rewritten[-1])
                self.assertIn(binding()['remote_launcher'], payload)
                self.assertTrue(payload.endswith('a' * 64 + ' ' + operation))
                self.assertNotIn('pkill', payload)
                self.assertNotIn('/app-server-control', payload)
                self.assertNotIn('nohup codex', payload)

    def test_markers_cover_binary_not_text(self):
        for marker in (bytes(8), bytes([255] * 8), bytes.fromhex(FIXTURE['marker_hex'])):
            command = native_command(native_bodies()['native-proxy'], marker)
            routed, _ = route_arguments(['remote-dev', command], manifest())
            expected = ''.join('\\' + format(c, '03o') for c in marker)
            self.assertIn(quote_always(expected), unwrap(routed[-1]))

    def test_future_native_variant_and_stock_install_do_not_spawn(self):
        bodies = [native_bodies()['native-start'] + ' ; echo changed',
                  'curl -fsSL https://example.invalid/install | CODEX_RELEASE=latest sh',
                  'pkill -9 -f codex', native_bodies()['native-proxy'] + ' --future']
        for body in bodies:
            with self.subTest(body=body), self.assertRaisesRegex(ShimError, 'updated management adapter'):
                route_arguments(['remote-dev', native_command(body, bytes(8))], manifest())

    def test_wrapper_changes_and_marker_changes_block(self):
        command = FIXTURE['proxyWrapped']
        tokens = shlex.split(command)
        for outer, payload in ((tokens[2] + '; true', tokens[4]),
                               (tokens[2], tokens[4].replace('\\377', '\\777')),
                               (tokens[2], tokens[4].replace("printf '%b'", "printf '%s'"))):
            changed = 'sh -c ' + quote_always(outer) + ' sh ' + quote_always(payload)
            with self.assertRaises(ShimError):
                route_arguments(['remote-dev', changed], manifest())

    def test_extra_native_arguments_block(self):
        with self.assertRaises(ShimError):
            route_arguments(['remote-dev', FIXTURE['proxyWrapped'], 'extra'], manifest())

    def test_unprepared_host_or_cross_profile_binding_blocks(self):
        with self.assertRaises(ShimError):
            route_arguments(['another-host', FIXTURE['proxyWrapped']], manifest())
        changed = manifest()
        changed['bindings'] = [binding(OTHER)]
        with self.assertRaises(ShimError):
            route_arguments(['remote-dev', FIXTURE['proxyWrapped']], changed)

    def test_platform_probe_is_read_only_and_installer_stays_blocked(self):
        routed, event = route_arguments(['remote-dev', native_command('uname -s', bytes(8))], manifest())
        self.assertEqual(event['operation'], 'native-platform')
        self.assertTrue(unwrap(routed[-1]).endswith('exec /bin/uname -s'))


class ArgumentTests(unittest.TestCase):
    def test_alias_occurring_only_in_option_value_is_not_destination(self):
        args = ['-o', 'ProxyCommand=ssh remote-dev nc %h %p', '-i', 'remote-dev',
                '-p', '2222', '-J', 'remote-dev', 'actual-host', 'ls']
        self.assertEqual(parse_invocation(args).destination, 'actual-host')
        self.assertEqual(route_arguments(args, manifest())[0], args)

    def test_attached_and_separate_values_and_combined_flags(self):
        args = ['-Tv', '-oBatchMode=yes', '-p2222', '-iidentity key', '-Jjump', '-Fconfig',
                '-l', 'remoteuser', '--', 'remote-dev', FIXTURE['proxyWrapped']]
        routed, _ = route_arguments(args, manifest())
        self.assertEqual(routed[:-1], args[:-1])

    def test_ordinary_ssh_and_scp_transport_unchanged(self):
        cases = [[], ['-V'], ['-Q', 'cipher'], ['-G', 'remote-dev'],
                 ['remote-dev'], ['remote-dev', 'codex', '--version'],
                 ['remote-dev', 'git-upload-pack repo'], ['-W', 'host:22', 'remote-dev'],
                 ['remote-dev', 'ls ~/.codex/app-server-control'],
                 ['remote-dev', 'cat ~/.codex/app-server-control/app-server.log'],
                 ['-s', 'remote-dev', 'sftp'], ['remote-dev', 'scp -t /tmp/file'],
                 ['-future-option', 'remote-dev', 'echo hi']]
        for args in cases:
            with self.subTest(args=args):
                self.assertEqual(route_arguments(args, manifest()), (args, {'operation': 'passthrough'}))

    def test_unknown_option_with_native_command_blocks(self):
        with self.assertRaises(ShimError):
            route_arguments(['--future', 'remote-dev', FIXTURE['proxyWrapped']], manifest())


class MarkerGateTests(unittest.TestCase):
    def test_every_split_of_binary_marker_preserves_original_bytes(self):
        marker = bytes.fromhex(FIXTURE['marker_hex'])
        payload = b'HTTP/1.1 101 Switching Protocols\r\n\r\n\x81\x00'
        prefix = b'login banner\xff\x00\n' + marker
        source = prefix + payload
        for split in range(len(source) + 1):
            gate = MarkerGate(marker)
            before, protocol = [], []
            for part in (source[:split], source[split:]):
                a, b = gate.feed(part)
                before.append(a)
                protocol.append(b)
            with self.subTest(split=split):
                self.assertEqual(b''.join(before), prefix)
                self.assertEqual(b''.join(protocol), payload)
                self.assertTrue(gate.found)

    def test_one_byte_at_a_time_and_bounded_missing_marker(self):
        marker = bytes.fromhex(FIXTURE['marker_hex'])
        gate = MarkerGate(marker)
        before, protocol = b'', b''
        for value in b'banner' + marker + b'HTTP':
            a, b = gate.feed(bytes([value]))
            before += a
            protocol += b
        self.assertEqual(before, b'banner' + marker)
        self.assertEqual(protocol, b'HTTP')
        with self.assertRaises(ShimError):
            MarkerGate(marker, limit=3).feed(b'four')


class ManifestTests(unittest.TestCase):
    def test_remote_paths_cannot_escape_profile_or_inject_line(self):
        for path in ('/home/test/launch.py',
                     binding()['remote_launcher'].replace(PROFILE, OTHER),
                     binding()['remote_launcher'] + '\n',
                     binding()['remote_launcher'].replace('/profiles/', '/profiles/../profiles/')):
            item = binding()
            item['remote_launcher'] = path
            with self.subTest(path=path), self.assertRaises(ShimError):
                validate_binding(item, PROFILE)

    def test_quoted_path_is_passed_as_one_remote_argument(self):
        item = binding()
        item['remote_launcher'] = item['remote_launcher'].replace('/home/test/', "/home/a b'c/")
        data = manifest()
        data['bindings'] = [item]
        rewritten, _ = route_arguments(['remote-dev', FIXTURE['proxyWrapped']], data)
        payload = unwrap(rewritten[-1]).split('; exec ', 1)[1]
        self.assertEqual(shlex.split(payload), ['/usr/bin/python3', item['remote_launcher'], 'a' * 64, 'native-proxy'])

    def test_prepare_creates_only_scoped_manifest_and_new_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proxy = root / 'bin/ssh.exe'
            real = root / 'original/ssh.exe'
            proxy.parent.mkdir()
            real.parent.mkdir()
            proxy.touch()
            real.touch()
            original = {'PATH': 'existing/path', 'SOME_SECRET': 'not-in-manifest'}
            scoped = prepare_environment(root, PROFILE, [binding()], original, app_version='26.903.9818.0', ssh_proxy=proxy, real_ssh=real)
            self.assertEqual(original['PATH'], 'existing/path')
            self.assertTrue(scoped['PATH'].startswith(str(proxy.parent)))
            data = Path(scoped['CODEX_MANAGER_SSH_BINDINGS']).read_text()
            self.assertNotIn('not-in-manifest', data)
            saved = json.loads(data)
            self.assertEqual(saved['real_ssh'], str(real.resolve()))
            self.assertEqual(saved['bindings'][0]['alias'], 'remote-dev')

    def test_missing_real_ssh_or_self_resolution_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proxy = root / 'ssh.exe'
            proxy.touch()
            with self.assertRaises(ShimError):
                prepare_environment(root, PROFILE, [], {}, app_version='26.903.9818.0', ssh_proxy=proxy, real_ssh=proxy)

    def test_changed_model_policy_cannot_silently_reuse_old_remote_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            proxy=root/'ssh.exe';real=root/'original.exe'
            proxy.touch();real.touch()
            old={**binding(),'model_ids':[PROFILE]}
            scoped=prepare_environment(root,PROFILE,[old],{},app_version='26.908.4834.0',
                                       ssh_proxy=proxy,real_ssh=real,selected_model_ids=[])
            manifest=json.loads(Path(scoped['CODEX_MANAGER_SSH_BINDINGS']).read_text())
            self.assertEqual(manifest['bindings'],[])
            self.assertEqual(manifest['pending_policy_hosts'], ['remote-dev'])
            self.assertEqual(manifest['auto_prepare_aliases'], ['remote-dev'])
            command=native_command(native_bodies()['native-proxy'],bytes(range(8)))
            with self.assertRaises(ShimError) as raised:
                route_arguments(['remote-dev',command],manifest)
            self.assertEqual(raised.exception.code,'host_binding_required')
            # Preparing a requested saved alias never rewrites desktop visibility.
            self.assertFalse((root/'work/control-center/profiles'/PROFILE/'codex/.codex-global-state.json').exists())

    def test_stale_policy_host_with_saved_alias_is_repaired_instead_of_blocked(self):
        # A reboot or a policy change can leave every prepared host pending.
        # Hosts with a saved alias must reach the scoped auto-prepare path
        # instead of failing forever with the pending-policy error.
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            proxy=root/'ssh.exe';real=root/'original.exe'
            proxy.touch();real.touch()
            profile=root/'work/control-center/profiles'/PROFILE
            (profile/'codex').mkdir(parents=True)
            (profile/'codex/.codex-global-state.json').write_text(json.dumps({
                'codex-managed-remote-connections': [
                    {'alias': 'remote-dev', 'hostId': 'remote-ssh-discovered:remote-dev'}]}),encoding='utf-8')
            old={**binding(),'model_ids':[PROFILE]}
            scoped=prepare_environment(root,PROFILE,[old],{},app_version='26.908.4834.0',
                                       ssh_proxy=proxy,real_ssh=real,selected_model_ids=[])
            saved=json.loads(Path(scoped['CODEX_MANAGER_SSH_BINDINGS']).read_text())
            self.assertEqual(saved['bindings'],[])
            self.assertEqual(saved['pending_policy_hosts'],['remote-dev'])
            self.assertEqual(saved['auto_prepare_aliases'],['remote-dev'])
            command=native_command(native_bodies()['native-proxy'],bytes(range(8)))
            with self.assertRaises(ShimError) as raised:
                route_arguments(['remote-dev',command],saved)
            self.assertEqual(raised.exception.code,'host_binding_required')

    def test_opening_task_preserves_live_binding_until_new_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proxy, real = root / 'ssh.exe', root / 'original.exe'
            proxy.touch(); real.touch()
            environment = {'CODEX_MANAGER_GENERATION': PROFILE, 'PATH': 'live-path'}
            old = {**binding(), 'model_ids': []}
            first = prepare_environment(root, PROFILE, [old], environment, app_version='26.908.4834.0',
                                        ssh_proxy=proxy, real_ssh=real, selected_model_ids=[])
            path = Path(first['CODEX_MANAGER_SSH_BINDINGS'])
            original = path.read_bytes()
            changed = {**old, 'revision': 'b' * 64, 'model_ids': [OTHER]}
            reopened = prepare_environment(root, PROFILE, [changed], environment, app_version='26.908.4834.0',
                                            ssh_proxy=proxy, real_ssh=real, selected_model_ids=[OTHER])
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(reopened, first)
            prepare_environment(root, PROFILE, [changed], dict(environment, CODEX_MANAGER_GENERATION=OTHER),
                                app_version='26.908.4834.0', ssh_proxy=proxy, real_ssh=real, selected_model_ids=[OTHER])
            self.assertEqual(json.loads(path.read_bytes())['bindings'][0]['revision'], 'b' * 64)

    def test_new_package_with_verified_native_code_keeps_ssh_working(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proxy, real = root / 'ssh.exe', root / 'original.exe'
            proxy.touch(); real.touch()
            fixture=json.loads((Path(__file__).parent/'fixtures/native_ssh_26_908_4834.json').read_text(encoding='utf-8-sig'))
            environment=prepare_environment(root, PROFILE, [binding()], {}, app_version='26.908.9136.0',
                ssh_proxy=proxy, real_ssh=real, app_source_sha256=fixture['source_sha256'])
            saved=json.loads(Path(environment['CODEX_MANAGER_SSH_BINDINGS']).read_text())
            for name,command in fixture['commands'].items():
                rewritten,event=route_arguments(['-T','remote-dev',command],saved)
                self.assertEqual(event['operation'],'native-'+name)
                self.assertIn(binding()['remote_launcher'],unwrap(rewritten[-1]))

    def test_unknown_ssh_implementation_does_not_prevent_local_profile_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proxy, real = root / 'ssh.exe', root / 'original.exe'
            proxy.touch(); real.touch()
            for version,source in [('future-version', None),('26.908.4834.0','changed-source')]:
                environment=prepare_environment(root, PROFILE, [binding()], {}, app_version=version,
                    ssh_proxy=proxy, real_ssh=real, app_source_sha256=source)
                saved=json.loads(Path(environment['CODEX_MANAGER_SSH_BINDINGS']).read_text())
                self.assertFalse(saved['native_compatible'])
                for args in [['-G','remote-dev']]:
                    self.assertEqual(route_arguments(args,saved),(args,{'operation':'passthrough'}))
                rewritten,event=route_arguments(['remote-dev',FIXTURE['proxyWrapped']],saved)
                self.assertEqual(event['operation'],'native-proxy')
                self.assertIn(binding()['remote_launcher'],unwrap(rewritten[-1]))
                for args in [['remote-dev','completely-changed-native-command'], ['remote-dev'],
                             ['remote-dev','echo unsafe'], ['-s','remote-dev','unknown-subsystem']]:
                    with self.assertRaises(ShimError):route_arguments(args,saved)
                # Old manifests keep their old conservative behavior until launch.
                saved.pop('native_command_validation')
                with self.assertRaises(ShimError):route_arguments(['remote-dev',FIXTURE['proxyWrapped']],saved)

    def test_windows_path_spelling_does_not_leave_two_conflicting_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proxy = root / 'bin/ssh.exe'
            real = root / 'original/ssh.exe'
            proxy.parent.mkdir()
            real.parent.mkdir()
            proxy.touch()
            real.touch()
            original = {'Path': 'selected-old-path', 'PATH': 'shadowed-old-path'}
            scoped = prepare_environment(root, PROFILE, [], original, app_version='26.903.9818.0', ssh_proxy=proxy, real_ssh=real)
            self.assertEqual([key for key in scoped if key.casefold() == 'path'], ['PATH'])
            self.assertTrue(scoped['PATH'].endswith('selected-old-path'))
            self.assertEqual(original['Path'], 'selected-old-path')

    def test_main_does_not_spawn_when_native_command_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / PROFILE
            profile.mkdir()
            real = root / 'ssh.exe'
            real.touch()
            data = {**manifest(), 'real_ssh': str(real), 'original_path': ''}
            path = profile / 'ssh-bindings.json'
            path.write_text(json.dumps(data))
            command = native_command('pkill -9 -f codex', bytes(8))
            with patch.dict(os.environ, {'CODEX_MANAGER_SSH_BINDINGS': str(path)}), \
                    patch('manager_core.ssh_shim.subprocess.Popen') as spawn, \
                    patch('manager_core.ssh_shim.os.execvpe') as replace, \
                    patch('sys.stderr'):
                self.assertEqual(main(['--', 'remote-dev', command]), 125)
                spawn.assert_not_called()
                replace.assert_not_called()
            audit = path.with_name('ssh-routing.jsonl').read_text()
            self.assertIn('native_command_changed', audit)
            self.assertNotIn('pkill', audit)
            self.assertNotIn('CODEX_REMOTE_PAYLOAD', audit)


class ShimStartFailureTests(unittest.TestCase):
    """Startup failures after the manifest loads must land in the audit trail."""

    def blocked_start(self, error):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            executable=root/'ssh.exe'
            executable.write_bytes(b'fixture')
            path=root/'ssh-bindings.json'
            data={**manifest(),'generation':OTHER,'inventory_root':str(root),'real_ssh':str(executable)}
            stderr=StringIO()
            with patch('manager_core.ssh_shim._load_manifest',side_effect=[(data,path),(data,path)]), \
                    patch('manager_core.ssh_connection_wait.wait_for_settings'), \
                    patch('manager_core.ssh_inventory.SshInventory.execution',return_value=RaisingAdmission(error)), \
                    patch('manager_core.ssh_shim._execute') as execute, patch('sys.stderr',stderr):
                code=main(['remote-dev',FIXTURE['proxyWrapped']])
            return code,path.with_name('ssh-routing.jsonl').read_text(encoding='utf-8'),stderr.getvalue(),execute

    def blocked(self, audit):
        return [entry for entry in map(json.loads,audit.splitlines()) if entry.get('operation')=='blocked']

    def test_context_entry_update_error_is_reported_as_profile_restarting(self):
        # The maintenance gate raises through SshInventory.execution's context
        # entry. The shim must preserve its code instead of reporting a generic
        # shim_start_failed, and must attribute it to the enrollment stage.
        code,audit,_,execute=self.blocked_start(
            UpdateError('profile_restarting','This profile is applying settings. Reconnect after it reopens.'))
        self.assertEqual(code,125)
        execute.assert_not_called()
        blocked=self.blocked(audit)
        self.assertEqual(len(blocked),1)
        self.assertEqual(blocked[0]['code'],'profile_restarting')
        self.assertEqual(blocked[0]['stage'],'enroll_ssh')

    def test_unexpected_runtime_error_is_typed_without_repeating_private_text(self):
        private='ssh-hidden-detail-2f9d'
        code,audit,stderr,execute=self.blocked_start(RuntimeError('transport unavailable: '+private))
        self.assertEqual(code,125)
        execute.assert_not_called()
        self.assertNotIn(private,audit)
        self.assertNotIn(private,stderr)
        self.assertIn('could not start',stderr)
        blocked=self.blocked(audit)
        self.assertEqual(len(blocked),1)
        self.assertEqual(blocked[0]['code'],'shim_start_failed')
        self.assertEqual(blocked[0]['stage'],'enroll_ssh')
        self.assertEqual(blocked[0].get('error_type'),'RuntimeError')


if __name__ == '__main__':
    unittest.main()
