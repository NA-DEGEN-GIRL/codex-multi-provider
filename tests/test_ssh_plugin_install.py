"""Portable SSH plugin publication with synthetic bundles and private homes."""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.plugin_sync import edit_config
from remote_helpers.plugin_install import MARKETPLACE, decode_bundles, install
from sync_ssh_plugins import remote_source


def bundle(name='demo', version='1.0.0', text='skill instructions', extra=None):
    files = {'.codex-plugin/plugin.json': json.dumps(dict(name=name, version=version, skills='./skills')).encode(),
             'skills/demo/SKILL.md': text.encode(), '.app.json': b'{"apps":{"demo":{"id":"connector_demo"}}}'}
    files.update(extra or {})
    digest = hashlib.sha256()
    for key, data in sorted(files.items()):
        digest.update(key.encode() + b'\0' + hashlib.sha256(data).digest())
    return dict(name=name, version=version, enabled=True, digest=digest.hexdigest(),
                files={key: base64.b64encode(data).decode() for key, data in files.items()})


class SshPluginInstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='codex-ssh-plugin-test-')
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.profiles = []
        for _ in range(2):
            identity = str(uuid4())
            path = self.home / '.local/share/codex-control-center/profiles' / identity / 'codex'
            path.mkdir(parents=True)
            (path / 'config.toml').write_text('# personal\nmodel = "existing"\n', encoding='utf-8')
            (path / 'auth.json').write_text('private account state', encoding='utf-8')
            self.profiles.append(dict(id=identity, home=str(path)))

    def apply(self, bundles=None):
        return install(dict(profiles=self.profiles, bundles=bundles or [bundle()]), edit_config=edit_config, home=self.home)

    def test_publish_multiple_profiles_preserves_auth_and_is_idempotent(self):
        result = self.apply()
        self.assertEqual([entry['mirrored'] for entry in result['profiles']], [['demo'], ['demo']])
        for row in self.profiles:
            path = Path(row['home'])
            config = tomllib.loads((path / 'config.toml').read_text())
            self.assertEqual(config['model'], 'existing')
            self.assertEqual((path / 'auth.json').read_text(), 'private account state')
            self.assertTrue(config['plugins']['demo@' + MARKETPLACE]['enabled'])
            self.assertEqual((path / 'plugins/cache' / MARKETPLACE / 'demo/1.0.0/.app.json').read_bytes(),
                             b'{"apps":{"demo":{"id":"connector_demo"}}}')
        stamps = {p: p.stat().st_mtime_ns for p in self.home.rglob('*') if p.is_file()}
        repeated = self.apply()
        self.assertFalse(any(row['config_changed'] for row in repeated['profiles']))
        self.assertEqual(stamps, {p: p.stat().st_mtime_ns for p in self.home.rglob('*') if p.is_file()})

    def test_native_bundle_and_remote_disable_survive(self):
        native_home, disabled_home = (Path(row['home']) for row in self.profiles)
        native = native_home / 'plugins/cache/openai-curated-remote/demo/9.0.0'
        native.mkdir(parents=True)
        (native / 'native.txt').write_text('native')
        with (native_home / 'config.toml').open('a') as stream:
            stream.write('[plugins."demo@openai-curated-remote"]\nenabled = false\n')
        with (disabled_home / 'config.toml').open('a') as stream:
            stream.write('[plugins."demo@codex-manager-shared"]\nenabled = false\n')
        result = self.apply()
        self.assertEqual(result['profiles'][0]['native'], ['demo'])
        self.assertFalse((native_home / 'plugins/cache' / MARKETPLACE / 'demo').exists())
        self.assertEqual(result['profiles'][1]['disabled'], ['demo'])
        self.assertFalse(tomllib.loads((disabled_home / 'config.toml').read_text())['plugins']['demo@' + MARKETPLACE]['enabled'])
        self.assertEqual((native / 'native.txt').read_text(), 'native')

    def test_same_version_republication_and_version_update(self):
        self.apply()
        self.apply([bundle(text='updated')])
        for row in self.profiles:
            base = Path(row['home']) / 'plugins/cache' / MARKETPLACE / 'demo'
            self.assertEqual((base / '1.0.0/skills/demo/SKILL.md').read_text(), 'updated')
        self.apply([bundle(version='2.0.0')])
        for row in self.profiles:
            base = Path(row['home']) / 'plugins/cache' / MARKETPLACE / 'demo'
            self.assertFalse((base / '1.0.0').exists())
            self.assertTrue((base / '2.0.0/.codex-plugin/plugin.json').is_file())

    def test_rejects_paths_binaries_private_files_and_windows_commands(self):
        for extra in ({'../escape': b'x'}, {'run.exe': b'windows'}, {'auth.json': b'secret'},
                      {'binary': b'MZopaque'}, {'.mcp.json': b'{"command":"C:\\\\Windows\\\\cmd.exe"}'}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                decode_bundles([bundle(extra=extra)])
        for identity in ('..', '.'):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                decode_bundles([bundle(name=identity)])

    def test_http_mcp_declaration_is_portable(self):
        decoded = decode_bundles([bundle(extra={'.mcp.json': b'{"url":"https://example.test/mcp"}'})])
        self.assertIn('.mcp.json', decoded['demo']['files'])

    def test_transported_helper_uses_the_existing_config_editor(self):
        source = 'from pathlib import Path;Path.home=classmethod(lambda cls:Path(' + repr(str(self.home)) + '));'
        result = subprocess.run([sys.executable, '-c', source + remote_source()],
                                input=json.dumps(dict(profiles=self.profiles, bundles=[bundle()])).encode(),
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(len(json.loads(result.stdout)['profiles']), 2)

    def test_installed_native_version_outranks_a_shared_bundle(self):
        target = Path(self.profiles[0]['home'])
        manifest = target / 'plugins/cache/openai-curated-remote/demo/9.0.0/.codex-plugin/plugin.json'
        manifest.parent.mkdir(parents=True)
        manifest.write_text('{"name":"demo","version":"9.0.0"}')
        result = self.apply()
        self.assertEqual(result['profiles'][0]['native'], ['demo'])
        self.assertFalse((target / 'plugins/cache' / MARKETPLACE / 'demo').exists())

    def test_native_disabled_declaration_without_cache_is_preserved(self):
        path = Path(self.profiles[0]['home']) / 'config.toml'
        with path.open('a') as stream:
            stream.write('[plugins."demo@openai-curated-remote"]\nenabled = false\n')
        result = self.apply()
        self.assertEqual(result['profiles'][0]['native'], ['demo'])
        self.assertNotIn('demo@' + MARKETPLACE, tomllib.loads(path.read_text())['plugins'])

    def test_rejects_digest_mismatch_before_writing(self):
        invalid = bundle()
        invalid['digest'] = '0' * 64
        with self.assertRaises(ValueError):
            self.apply([invalid])
        self.assertFalse((self.home / '.local/share/codex-control-center/shared-plugins').exists())

    def test_rejects_profile_home_escape_and_unowned_cache(self):
        original = copy.deepcopy(self.profiles)
        self.profiles[0]['home'] = str(self.home)
        with self.assertRaises(ValueError):
            self.apply()
        self.profiles = original
        unowned = Path(self.profiles[0]['home']) / 'plugins/cache' / MARKETPLACE / 'demo/1.0.0'
        unowned.mkdir(parents=True)
        (unowned / 'keep.txt').write_text('manual content')
        with self.assertRaises(ValueError):
            self.apply()
        self.assertEqual((unowned / 'keep.txt').read_text(), 'manual content')

    def test_rejects_linked_profile_cache(self):
        target = Path(self.profiles[0]['home']) / 'plugins'
        elsewhere = self.home / 'outside'
        elsewhere.mkdir()
        try:
            os.symlink(elsewhere, target, target_is_directory=True)
        except OSError:
            self.skipTest('Symlink creation unavailable.')
        with self.assertRaises(ValueError):
            self.apply()
        self.assertEqual(list(elsewhere.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
