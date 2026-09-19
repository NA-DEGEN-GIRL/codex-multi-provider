"""No live SSH, credentials, or account files; exercise real archive helpers."""
from copy import deepcopy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import socket
import threading
import tarfile
import tempfile
import time
import tomllib
import unittest
import uuid
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from manager_core.remote import RemoteError, RemoteManager, _parse_aliases, INSPECT_COMMAND


def helper(name):
    spec = importlib.util.spec_from_file_location("remote_test_" + name, ROOT / "scripts/remote_helpers" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


INSTALL = helper("install")
LAUNCH = helper("launch")
WS = helper("ws_client")
PROFILE = "00000000-0000-4000-9000-000000000006"


def probe(**overrides):
    values = {"os": "Linux", "arch": "x86_64", "home": "/home/test user", "uid": "1000",
              "machine": "test-host", "cli": "/usr/local/bin/codex", "cli_version": "codex-cli 0.153.4",
              "stock_login": "true", "python": "/usr/bin/python3", "python_version": "Python 3.11.9", "host_identity": "f" * 64}
    values.update(overrides)
    return "".join(f"CODEX_MANAGER_INSPECT_V1\t{k}\t{v}\n" for k, v in values.items()).encode()


def make_artifact(root, *, arch="x86_64", wrong_format=False):
    directory = root / "artifacts/remote" / ("linux-" + arch)
    directory.mkdir(parents=True)
    files = []
    header = bytearray(64)
    header[:6] = b"\x7fELF\x02\x01"
    header[18:20] = struct.pack("<H", 62 if arch == "x86_64" else 183)
    if wrong_format:
        header[:2] = b"MZ"
    for name in ("codex", "codex-code-mode-host", "bwrap"):
        data = bytes(header) + name.encode()
        (directory / name).write_bytes(data)
        files.append({"path": name, "sha256": hashlib.sha256(data).hexdigest()})
    (directory / "manifest.json").write_text(json.dumps({"schema": 1, "platform": "linux", "architecture": arch,
                "version": "0.153.4-test", "external_bridge_present": True,
                "bwrap_sha256": files[-1]["sha256"], "files": files}))
    return directory


class Registry:
    def render_for_host(self, config_home, enabled, model_ids, **kwargs):
        assert config_home.startswith("/home/test user/")
        return {"files": {"config.toml": 'model = "gpt-test"\n', "agents/test.toml": 'model = "test"\n'}, "revision": "test"}

    def environment(self, model_ids):
        return {"CODEX_EXTERNAL_TEST_API_KEY": "synthetic-secret-for-test"} if model_ids else {}


class SettingsRegistry(Registry):
    def render_for_host(self, config_home, enabled, model_ids, **kwargs):
        rendered = super().render_for_host(config_home, enabled, model_ids, **kwargs)
        settings = dict(enabled=enabled, models=model_ids, primary_model_id=kwargs.get('primary_model_id'),
                        primary_settings=kwargs.get('primary_settings'),
                        selection_mode=kwargs.get('selection_mode', 'automatic'))
        rendered['files']['config.toml'] += '# fixture settings: ' + json.dumps(settings, sort_keys=True) + '\n'
        return rendered


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / "ssh/config"
        self.config.parent.mkdir()
        self.config.write_text("Host production staging\n  HostName example.invalid\n  IdentityFile NEVER_READ_THIS_KEY\n")
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def manager(self, callback=None):
        def runner(args, **kwargs):
            self.calls.append((args, kwargs))
            return callback(args, kwargs) if callback else subprocess.CompletedProcess(args, 0, probe(), b"")
        return RemoteManager(self.root, ssh_config=self.config, runner=runner, ssh_executable="ssh.exe", registry=Registry())

    def prepared_settings_fixture(self):
        artifact = make_artifact(self.root)
        helpers = self.root / 'scripts/remote_helpers'
        helpers.mkdir(parents=True)
        for name in ('install', 'launch', 'native_controller', 'common', 'managed_sources', 'ws_client'):
            (helpers / (name + '.py')).write_bytes((ROOT / 'scripts/remote_helpers' / (name + '.py')).read_bytes())
        remote = self.root / 'simulated-settings-remote'
        def runner(args, options):
            if args[-1] == INSPECT_COMMAND:
                return subprocess.CompletedProcess(args, 0, probe(), b'')
            if args[-1].endswith('--preflight'):
                result = INSTALL.preflight(json.loads(options['input']), remote)
            elif 'stdin' in options:
                result = INSTALL.install(io.BytesIO(options['stdin'].read()), remote)
            else:
                result = LAUNCH.configure(remote / 'profiles' / PROFILE, json.loads(options['input']), expected_host='f' * 64)
            return subprocess.CompletedProcess(args, 0, json.dumps(result).encode(), b'')
        manager = self.manager(runner)
        manager._registry = SettingsRegistry()
        binding = manager.prepare('staging', PROFILE, self.root, ['model1'])
        self.assertIs(binding['prepared'], True)
        profile = dict(id=PROFILE, auth_mode='chatgpt', policy=dict(enabled=True, model_ids=['model1']))
        self.calls.clear()
        return manager, profile, binding, artifact, helpers

    def test_prepared_remote_definition_ignores_manager_only_build_changes_without_ssh(self):
        manager, profile, binding, _, _ = self.prepared_settings_fixture()
        self.assertTrue(manager.binding_matches_settings(profile, binding))
        (self.root / 'scripts/manager_core').mkdir()
        (self.root / 'scripts/manager_core/release_code.py').write_text('# different manager revision\n')
        (self.root / 'manager-shell.exe').write_bytes(b'new UI build')
        with patch('manager_core.release_code.runtime_revision', side_effect=AssertionError('UI revision consulted')):
            self.assertTrue(manager.binding_matches_settings(profile, binding))
        self.assertEqual(self.calls, [])

    def test_prepared_remote_definition_detects_helpers_models_bundle_and_host_changes(self):
        manager, profile, binding, artifact, helpers = self.prepared_settings_fixture()
        for path in helpers.glob('*.py'):
            original = path.read_bytes()
            with self.subTest(helper=path.name):
                path.write_bytes(original + b'\n# changed remote helper\n')
                self.assertFalse(manager.binding_matches_settings(profile, binding))
                path.write_bytes(original)
                self.assertTrue(manager.binding_matches_settings(profile, binding))
        variants = []
        changed = deepcopy(profile)
        changed['policy']['model_ids'] = ['model2']
        variants.append(changed)
        changed = deepcopy(profile)
        changed['policy']['enabled'] = False
        variants.append(changed)
        changed = deepcopy(profile)
        changed['policy']['selection_mode'] = 'external_only'
        variants.append(changed)
        changed = deepcopy(profile)
        changed.update(auth_mode='external', external_model_id='model2', external_settings={'reasoning_effort': 'high'})
        variants.append(changed)
        for changed in variants:
            with self.subTest(profile=changed):
                self.assertFalse(manager.binding_matches_settings(changed, binding))
        for field, value in [('host_identity', 'e' * 64), ('host_identity', None),
                             ('runtime_bundle', 'missing-bundle'), ('prepared', False), ('revision', 'b' * 64)]:
            with self.subTest(field=field, value=value):
                self.assertFalse(manager.binding_matches_settings(profile, {**binding, field: value}))
        path = artifact / 'manifest.json'
        manifest = json.loads(path.read_text())
        manifest['version'] = '0.153.4-new'
        path.write_text(json.dumps(manifest))
        self.assertFalse(manager.binding_matches_settings(profile, binding))
        self.assertEqual(self.calls, [])

    def test_discovery_only_exposes_exact_aliases_and_safe_include(self):
        (self.config.parent / "more.conf").write_text("Host gpu\n  HostName private-host\nMatch exec 'never execute'\nHost !negated wild* [range]\n")
        self.config.write_text(self.config.read_text() + "Include more.conf\nHost = equals\n")
        self.assertEqual(_parse_aliases(self.config), ["equals", "gpu", "production", "staging"])
        result = self.manager().list_hosts()
        self.assertNotIn("private-host", json.dumps(result))
        self.assertEqual(self.calls, [])

    def test_alias_injection_rejected_before_process_spawn(self):
        for alias in ("-oProxyCommand=evil", "production;evil", "production\nHost evil", "unknown", "user@host"):
            with self.subTest(alias=alias), self.assertRaises(RemoteError):
                self.manager().inspect(alias)
        self.assertEqual(self.calls, [])

    def test_inspection_read_only_and_known_host_auth_required(self):
        result = self.manager().inspect("production")
        args, options = self.calls[0]
        self.assertIn("BatchMode=yes", args)
        self.assertIn("StrictHostKeyChecking=yes", args)
        self.assertEqual(args[-2:], ["production", INSPECT_COMMAND])
        self.assertEqual(args[1:3], ["-F", str(self.config)])
        self.assertNotIn("shell", options)
        self.assertFalse(result["remote_writes"])
        self.assertTrue(result["stock_cli_authenticated"])
        self.assertFalse(result["native_gui_verified"])
        self.assertIn("remote_artifact_missing", result["blockers"])

    def test_remote_failure_redacts_stdout_and_stderr(self):
        result = self.manager(lambda a, k: subprocess.CompletedProcess(a, 255, b"secret-token", b"secret-key")).inspect("production")
        self.assertEqual(result["status"], "connection_failed")
        self.assertNotIn("secret", json.dumps(result))

    def test_os_or_missing_cli_cannot_prepare(self):
        result = self.manager(lambda a, k: subprocess.CompletedProcess(a, 0, probe(os="MINGW64_NT", cli=""), b"")).prepare("production", PROFILE, self.root, [])
        self.assertFalse(result["prepared"])
        self.assertEqual(len(self.calls), 1)
        self.assertIn("platform_not_supported", result["blockers"])

    def test_windows_executable_cannot_be_deployed_as_linux(self):
        make_artifact(self.root, wrong_format=True)
        result = self.manager().inspect("production")
        self.assertIn("artifact_architecture_mismatch", result["blockers"])
        self.assertFalse(result["preparation_supported"])

    def test_modified_artifact_rejected(self):
        directory = make_artifact(self.root)
        (directory / "codex").write_bytes(b"changed")
        result = self.manager().inspect("production")
        self.assertIn("artifact_hash_mismatch", result["blockers"])

    def test_linux_bundle_requires_pinned_sandbox_companion(self):
        directory = make_artifact(self.root)
        manifest_path = directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        for field in ("missing_file", "wrong_digest"):
            bad = dict(manifest)
            if field == "missing_file":
                bad["files"] = [item for item in bad["files"] if item["path"] != "bwrap"]
            else:
                bad["bwrap_sha256"] = "0" * 64
            manifest_path.write_text(json.dumps(bad))
            with self.subTest(field=field), self.assertRaises(RemoteError):
                self.manager()._artifact("linux", "x86_64")

    def test_python_version_is_not_assumed_compatible(self):
        result = self.manager(lambda a, k: subprocess.CompletedProcess(a, 0, probe(python_version="Python 3.9.1"), b"")).inspect("production")
        self.assertIn("python311_required", result["blockers"])
        self.assertFalse(result["preparation_supported"])

    def test_native_daemon_and_public_listeners_rejected(self):
        for argv in (["app-server", "daemon", "stop"], ["app-server", "proxy"],
                     ["app-server", "--listen", "ws://0.0.0.0:4000"],
                     ["app-server", "--remote-control"], ["app-server", "-c", "evil=true"]):
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                LAUNCH.validate_command(argv)
        for argv in (["app-server"], ["app-server", "--stdio"], ["app-server", "--listen", "stdio://"],
                     ["login", "status"]):
            LAUNCH.validate_command(argv)

    def test_cached_directory_must_match_exact_file_set(self):
        source, target = self.root / "source", self.root / "cached"
        source.mkdir()
        target.mkdir()
        (source / "config.toml").write_text("same")
        (target / "config.toml").write_text("same")
        self.assertTrue(INSTALL._same_files(source, target))
        (target / "injected.toml").write_text("bad")
        self.assertFalse(INSTALL._same_files(source, target))

    def test_dispatcher_imports_do_not_pollute_immutable_helpers(self):
        profile = self.root / 'profiles' / PROFILE
        helpers = profile / 'helpers' / ('b' * 64)
        helpers.mkdir(parents=True)
        definitions = profile / 'definitions'
        definitions.mkdir()
        revision = 'a' * 64
        (definitions / (revision + '.json')).write_text(json.dumps({'helpers': str(helpers)}))
        (helpers / 'cache_probe.py').write_text('VALUE = 7\n')
        (helpers / 'launch.py').write_text('import sys,cache_probe\nprint(sys.dont_write_bytecode, cache_probe.VALUE)\n')
        launcher = profile / 'launch.py'
        launcher.write_text(INSTALL.DISPATCHER)
        for _ in range(2):
            result = subprocess.run([sys.executable, str(launcher), revision], capture_output=True, text=True, timeout=20)
            self.assertEqual((result.returncode, result.stdout.strip()), (0, 'True 7'))
        self.assertFalse((helpers / '__pycache__').exists())

    def test_payload_install_and_credentials_are_separate_and_status_honest(self):
        make_artifact(self.root)
        helpers = self.root / "scripts/remote_helpers"
        helpers.mkdir(parents=True)
        for name in ("install.py", "launch.py", "native_controller.py", "common.py", "managed_sources.py", "ws_client.py"):
            (helpers / name).write_bytes((ROOT / "scripts/remote_helpers" / name).read_bytes())
        remote = self.root / "simulated-remote"
        archive_bytes = []

        def runner(args, options):
            if args[-1] == INSPECT_COMMAND:
                return subprocess.CompletedProcess(args, 0, probe(), b"")
            if args[-1].endswith('--preflight'):
                result=INSTALL.preflight(json.loads(options['input']),remote)
                return subprocess.CompletedProcess(args,0,json.dumps(result).encode(),b'')
            if "stdin" in options:
                raw = options["stdin"].read()
                archive_bytes.append(raw)
                result = INSTALL.install(io.BytesIO(raw), remote)
            else:
                result = LAUNCH.configure(remote / "profiles" / PROFILE, json.loads(options["input"]), expected_host="f" * 64)
            return subprocess.CompletedProcess(args, 0, json.dumps(result).encode(), b"")

        result = self.manager(runner).prepare("staging", PROFILE, self.root / "unused-private-home", ["model1"])
        self.assertEqual(result["status"], "prepared_not_connected")
        self.assertTrue(result["prepared"])
        self.assertTrue(result["authentication_required"])
        self.assertFalse(result["native_gui_verified"])
        self.assertNotIn(b"synthetic-secret", archive_bytes[0])
        self.assertNotIn("synthetic-secret", json.dumps(result))
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.calls[2][0][-2], "staging")
        self.assertFalse((remote / "profiles" / PROFILE / "codex/config.toml").exists())
        secret_files = list((remote / "profiles" / PROFILE / "credentials").glob("*.json"))
        self.assertEqual(len(secret_files), 1)
        self.assertIn("synthetic-secret", secret_files[0].read_text())
        definition = json.loads((remote / "profiles" / PROFILE / "definitions" / (result["revision"] + ".json")).read_text())
        self.assertEqual((Path(definition["helpers"]) / "ws_client.py").read_bytes(),
                         (helpers / "ws_client.py").read_bytes())

    def test_install_failure_does_not_send_credentials(self):
        make_artifact(self.root)
        helpers = self.root / "scripts/remote_helpers"
        helpers.mkdir(parents=True)
        for name in ("install.py", "launch.py", "native_controller.py", "common.py", "managed_sources.py", "ws_client.py"):
            (helpers / name).write_bytes((ROOT / "scripts/remote_helpers" / name).read_bytes())
        def callback(args, opts):
            return subprocess.CompletedProcess(args, 0, probe(), b"") if args[-1] == INSPECT_COMMAND else subprocess.CompletedProcess(args, 1, b"", b"private")
        with self.assertRaises(RemoteError):
            self.manager(callback).prepare("production", PROFILE, self.root, ["model1"])
        self.assertEqual(len(self.calls), 2)

    def test_new_profile_reuses_verified_host_runtime_without_uploading_binaries(self):
        from manager_core.store import Store
        artifact=make_artifact(self.root)
        helpers=self.root/'scripts/remote_helpers';helpers.mkdir(parents=True)
        for name in ('install.py','launch.py','native_controller.py','common.py','managed_sources.py','ws_client.py'):
            (helpers/name).write_bytes((ROOT/'scripts/remote_helpers'/name).read_bytes())
        remote=self.root/'simulated-remote';archives=[];profile_ids=[]
        def runner(args,options):
            if args[-1]==INSPECT_COMMAND:return subprocess.CompletedProcess(args,0,probe(),b'')
            if args[-1].endswith('--preflight'):
                result=INSTALL.preflight(json.loads(options['input']),remote)
            elif 'stdin' in options:
                raw=options['stdin'].read();archives.append(raw)
                result=INSTALL.install(io.BytesIO(raw),remote)
                with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
                    profile_ids.append(json.load(tar.extractfile('manifest.json'))['profile_id'])
            else:result=LAUNCH.configure(remote/'profiles'/profile_ids[-1],json.loads(options['input']),expected_host='f'*64)
            return subprocess.CompletedProcess(args,0,json.dumps(result).encode(),b'')
        manager=self.manager(runner)
        first=manager.prepare('staging',PROFILE,self.root,[])
        store=Store(self.root);profile=store.add_profile('existing')
        store.mutate(lambda data:store.profile(profile['id'],data).update(remote_bindings=[first]))
        # A newer local build must not force another ~475 MB upload merely to
        # add an account to a server already running a compatible pinned build.
        old=self.root/'artifacts/remote/previous';artifact.rename(old)
        make_artifact(self.root)
        manifest_path=artifact/'manifest.json';manifest=json.loads(manifest_path.read_text())
        (artifact/'codex').write_bytes((artifact/'codex').read_bytes()+b'new release')
        manifest['version']='0.153.4-new';manifest['files'][0]['sha256']=hashlib.sha256((artifact/'codex').read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest))
        pid=str(uuid.uuid4())
        with patch.object(INSTALL.shutil,'disk_usage',return_value=type('Usage',(),{'free':1})()):
            second=manager.prepare('staging',pid,self.root,[],reuse_host_runtime=True)
        self.assertTrue(second['runtime_reused'])
        self.assertEqual(second['runtime_bundle'],first['runtime_bundle'])
        with tarfile.open(fileobj=io.BytesIO(archives[-1])) as tar:
            self.assertFalse(any(name.startswith('runtime/') for name in tar.getnames()))
        descriptor=json.loads((remote/'profiles'/pid/'definitions'/(second['revision']+'.json')).read_text())
        self.assertEqual(Path(descriptor['runtime']).name,first['runtime_bundle'])
        self.assertTrue((remote/'profiles'/PROFILE/'launch.py').exists())
        # Explicit update preparation still requires the selected current build.
        with patch.object(INSTALL.shutil,'disk_usage',return_value=type('Usage',(),{'free':1})()):
            with self.assertRaisesRegex(RemoteError,'디스크 공간'):
                manager.prepare('staging',pid,self.root,[])
        # Never reuse corrupted cache bytes or silently send credentials on failure.
        (Path(descriptor['runtime'])/'codex').write_bytes(b'corrupt')
        with patch.object(INSTALL.shutil,'disk_usage',return_value=type('Usage',(),{'free':1})()):
            with self.assertRaisesRegex(RemoteError,'디스크 공간'):
                manager.prepare('staging',str(uuid.uuid4()),self.root,[],reuse_host_runtime=True)

    def test_disk_full_is_a_static_actionable_error_without_remote_output(self):
        import errno
        value=INSTALL.failure(OSError(errno.ENOSPC,'PRIVATE CONTENT'))
        self.assertEqual(value['code'],'remote_disk_full')
        self.assertNotIn('PRIVATE',json.dumps(value))
        with self.assertRaises(RemoteError) as raised:
            RemoteManager._json_result(subprocess.CompletedProcess([],1,json.dumps(value).encode(),b'PRIVATE'),'install_failed')
        self.assertEqual(raised.exception.code,'remote_disk_full')
        self.assertNotIn('PRIVATE',str(raised.exception))

    def test_mixed_catalog_helper_is_pinned_and_descriptor_requires_supported_runtime(self):
        artifact = make_artifact(self.root)
        manifest_path = artifact / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest.update(managed_sources_present=True, source_catalog_present=True, mixed_source_catalog_present=True)
        manifest_path.write_text(json.dumps(manifest))
        helpers = self.root / 'scripts/remote_helpers'
        helpers.mkdir(parents=True)
        for name in ('install.py', 'launch.py', 'native_controller.py', 'common.py', 'managed_sources.py', 'ws_client.py', 'catalog_legacy.py'):
            (helpers / name).write_bytes((ROOT / 'scripts/remote_helpers' / name).read_bytes())
        remote_home = self.root / 'simulated-remote'
        def runner(args, options):
            if args[-1] == INSPECT_COMMAND:
                return subprocess.CompletedProcess(args, 0, probe(), b'')
            if args[-1].endswith('--preflight'):
                result=INSTALL.preflight(json.loads(options['input']),remote_home)
                return subprocess.CompletedProcess(args,0,json.dumps(result).encode(),b'')
            if 'stdin' in options:
                result = INSTALL.install(io.BytesIO(options['stdin'].read()), remote_home)
            else:
                result = LAUNCH.configure(remote_home / 'profiles' / PROFILE, json.loads(options['input']), expected_host='f' * 64)
            return subprocess.CompletedProcess(args, 0, json.dumps(result).encode(), b'')
        manager = self.manager(runner)
        first = manager.prepare('staging', PROFILE, self.root / 'unused-home', [])
        definition = remote_home / 'profiles' / PROFILE / 'definitions' / (first['revision'] + '.json')
        descriptor = json.loads(definition.read_text())
        self.assertTrue(descriptor['mixed_source_catalog'])
        pinned = Path(descriptor['helpers']) / 'catalog_legacy.py'
        first_helper = pinned.read_bytes()
        self.assertEqual(first_helper, (helpers / 'catalog_legacy.py').read_bytes())
        with (helpers / 'catalog_legacy.py').open('ab') as file:
            file.write(b'\n# Fixture helper revision\n')
        second = manager.prepare('staging', PROFILE, self.root / 'unused-home', [])
        self.assertNotEqual(first['revision'], second['revision'])
        self.assertEqual(pinned.read_bytes(), first_helper)
        manifest['source_catalog_present'] = False
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaises(RemoteError):
            manager._artifact('linux', 'x86_64')

    def test_configure_rejects_injected_environment(self):
        profile = self.root / "profile"
        (profile / "definitions").mkdir(parents=True)
        revision = "a" * 64
        (profile / "definitions" / (revision + ".json")).write_text(json.dumps({"revision": revision}))
        with self.assertRaises(ValueError):
            LAUNCH.configure(profile, {"revision": revision, "environment": {"PATH": "evil"}})
        self.assertFalse((profile / "credentials").exists())

    def test_archive_traversal_and_partial_failure_leave_no_active_runtime(self):
        for extra_name in ("../escape", "/absolute", "runtime/../escape"):
            with self.subTest(extra_name=extra_name):
                output = io.BytesIO()
                files = {"runtime/codex": b"a", "runtime/codex-code-mode-host": b"b", "runtime/bwrap": b"e",
                         "definition/config.toml": b"c", "helpers/launch.py": b"d", "helpers/common.py": b"f",
                         "helpers/native_controller.py": b"g"}
                manifest = {"schema": 1, "bundle_id": "test", "profile_id": PROFILE, "revision": "a" * 64,
                            "files": [{"path": k, "mode": 0o700, "sha256": hashlib.sha256(v).hexdigest()} for k, v in files.items()]}
                with tarfile.open(fileobj=output, mode="w") as tar:
                    RemoteManager._add(tar, "manifest.json", json.dumps(manifest).encode(), 0o600)
                    RemoteManager._add(tar, extra_name, b"bad", 0o600)
                output.seek(0)
                target = self.root / uuid.uuid4().hex
                with self.assertRaises(ValueError):
                    INSTALL.install(output, target)
                self.assertFalse((target / "runtime").exists())
                self.assertEqual(list(target.glob(".incoming-*")), [])


@unittest.skipUnless(os.name == "posix", "Linux profile lock and exec semantics")
class LinuxLauncherTests(unittest.TestCase):
    def test_lock_is_owned_by_exec_runtime_and_configuration_is_scoped(self):
        import fcntl
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            profile = base / "profiles" / PROFILE
            revision = "a" * 64
            definition = profile / "definitions" / revision
            definition.mkdir(parents=True)
            (definition / "config.toml").write_text('model = "synthetic"\n')
            runtime = base / "runtime/bundle"
            runtime.mkdir(parents=True)
            executable = runtime / "codex"
            executable.write_text("#!/usr/bin/python3\nimport os,sys\nprint('RUNTIME:'+str(os.getpid()),flush=True)\nsys.stdin.readline()\n")
            executable.chmod(0o700)
            (profile / "definitions" / (revision + ".json")).write_text(json.dumps({"revision": revision,
                "profile_id": PROFILE, "runtime": str(runtime), "definition": str(definition), "host_identity": LAUNCH.host_identity()}))
            LAUNCH.configure(profile, {"revision": revision, "environment": {}})
            launcher = profile / "launch.py"
            launcher.write_bytes((ROOT / "scripts/remote_helpers/launch.py").read_bytes())
            (profile / "common.py").write_bytes((ROOT / "scripts/remote_helpers/common.py").read_bytes())
            process = subprocess.Popen([sys.executable, str(launcher), revision, "app-server", "--stdio"],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                       env=dict(os.environ, HOME=str(base / "synthetic-user")))
            try:
                # exec retains the same PID: there is no Python supervisor to die
                # and release the lock while a separately spawned runtime survives.
                self.assertEqual(process.stdout.readline().strip(), "RUNTIME:" + str(process.pid))
                self.assertEqual(tomllib.loads((profile / "codex/config.toml").read_text()), {"model": "synthetic"})
                with (profile / "instance.lock").open("a+b") as lock:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    process.communicate("finish\n", timeout=5)
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(process.returncode, 0)
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)


class WebSocketClientTests(unittest.TestCase):
    def test_masked_client_and_fragmented_server_with_ping(self):
        import base64
        client, server = socket.socketpair()
        captured = []
        failures = []

        def read_exact(stream, size):
            data = b""
            while len(data) < size:
                piece = stream.read(size - len(data))
                if not piece:
                    raise EOFError()
                data += piece
            return data

        def serve():
            try:
                with server, server.makefile("rwb", buffering=0) as stream:
                    request = b""
                    while not request.endswith(b"\r\n\r\n"):
                        request += read_exact(stream, 1)
                    key = next(line.split(b": ", 1)[1] for line in request.split(b"\r\n") if line.startswith(b"Sec-WebSocket-Key:"))
                    accept = base64.b64encode(hashlib.sha1(key + WS.GUID.encode()).digest())
                    stream.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
                    first, size = read_exact(stream, 2)
                    self.assertEqual(first, 0x81)
                    self.assertTrue(size & 0x80)
                    mask = read_exact(stream, 4)
                    payload = read_exact(stream, size & 127)
                    captured.append(json.loads(bytes(b ^ mask[i % 4] for i, b in enumerate(payload))))
                    stream.write(b'\x01\x07{"id":1')
                    stream.write(b'\x89\x01x')
                    suffix = b',"result":{}}'
                    stream.write(bytes((0x80, len(suffix))) + suffix)
                    # Capture and validate the masked pong as well.
                    first, size = read_exact(stream, 2)
                    self.assertEqual(first, 0x8a)
                    self.assertEqual(size, 0x81)
                    mask = read_exact(stream, 4)
                    self.assertEqual(read_exact(stream, 1)[0] ^ mask[0], ord('x'))
            except Exception as error:
                failures.append(error)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            with client.makefile("rwb", buffering=0) as stream:
                channel = WS.WebSocketPipe(stream, stream)
                channel.handshake(timeout=3)
                channel.send_json({"id": 1, "method": "test"})
                self.assertEqual(channel.receive_json(timeout=3), {"id": 1, "result": {}})
            thread.join(timeout=3)
            self.assertEqual(captured, [{"id": 1, "method": "test"}])
            self.assertEqual(failures, [])
        finally:
            client.close()
            server.close()

    def test_oversized_and_masked_server_frames_are_rejected(self):
        for frame in (b"\x81\x80", b"\x81\x7f" + struct.pack("!Q", 1000)):
            with self.subTest(frame=frame):
                channel = WS.WebSocketPipe(io.BytesIO(frame), io.BytesIO(), max_message=10)
                with self.assertRaises(ValueError):
                    channel.receive_json(timeout=1)


if __name__ == "__main__":
    unittest.main()
