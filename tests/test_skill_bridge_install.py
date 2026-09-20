"""Isolated remote-installer fixtures; no real SSH host or user home is used."""
import base64
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from remote_helpers import skill_bridge_install as installer


def encoded(value):
    return base64.b64encode(value).decode('ascii')


@unittest.skipIf(os.name == 'nt', 'Remote installer targets POSIX; these fixtures are also run under isolated WSL /tmp.')
class SkillBridgeInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='codex-skill-bridge-fixture-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / 'isolated-home'
        self.home.mkdir()
        probe = self.root / 'symlink-probe'
        try:
            probe.symlink_to(self.home, target_is_directory=True)
            probe.unlink()
        except OSError:
            self.skipTest('Installer requires POSIX symlinks; run this fixture under WSL/Linux.')
        self.bridge = self.home / '.codex/workspace-skill-bridge'
        self.skills = self.home / '.agents/skills'
        self.client_bytes = b'# fixture client\nprint("fixture only")\n'
        self.source_bytes = {}
        for name in ('3d-assets', 'game-audio'):
            base = name + '/.agents/skills/' + name
            self.source_bytes[base + '/SKILL.md'] = ('# ' + name + '\n[Runtime](../../../docs/guide.md)\n').encode()
            self.source_bytes[base + '/references/use.md'] = '사용 설명 · ü\n'.encode('utf-8')
            self.source_bytes[base + '/scripts/fixture.py'] = b'# wrapper fixture\n'
            self.source_bytes[name + '/docs/guide.md'] = b'# Runtime guide\n'
        self.payload = dict(owner=str(uuid4()), files={k: encoded(v) for k, v in self.source_bytes.items()},
                            client=encoded(self.client_bytes), endpoint='http://127.0.0.1:43210/v1',
                            token='isolated-fixture-token-' + uuid4().hex,
                            enabled_skills=['3d-assets', 'game-audio'])

    def install(self, payload=None):
        return installer.install(deepcopy(self.payload if payload is None else payload), home=self.home)

    def disabled(self):
        return {**self.payload, 'files': {}, 'enabled_skills': []}

    def test_projection_preserves_bytes_relative_docs_and_private_descriptor(self):
        result = self.install()
        self.assertEqual(result['installed'], ['3d-assets', 'game-audio'])
        self.assertEqual(result['conflicts'], [])
        release = self.bridge / 'revisions' / result['revision']
        for name, content in self.source_bytes.items():
            self.assertEqual((release / name).read_bytes(), content)
        for name in self.payload['enabled_skills']:
            link = self.skills / name
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), release / name / '.agents/skills' / name)
            self.assertEqual((link / '../../../docs/guide.md').resolve().read_bytes(), b'# Runtime guide\n')
        self.assertEqual((self.bridge / 'client.py').read_bytes(), self.client_bytes)
        descriptor = self.bridge / 'connection.json'
        config = json.loads(descriptor.read_text())
        self.assertEqual(config['token'], self.payload['token'])
        self.assertEqual(config['enabled_skills'], ['3d-assets', 'game-audio'])
        self.assertEqual(config['endpoint'], self.payload['endpoint'])
        if os.name != 'nt':
            self.assertEqual(descriptor.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.bridge.stat().st_mode & 0o777, 0o700)

    def test_repeated_install_is_content_idempotent_and_reuses_revision(self):
        first = self.install()
        before = {p.name: p.read_bytes() for p in self.bridge.glob('*.json')}
        targets = {name: (self.skills / name).readlink() for name in self.payload['enabled_skills']}
        second = self.install()
        self.assertEqual(first, second)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.bridge.glob('*.json')})
        self.assertEqual(targets, {name: (self.skills / name).readlink() for name in targets})
        self.assertEqual(len(list((self.bridge / 'revisions').iterdir())), 1)

    def test_native_directory_and_conflicting_symlink_are_preserved(self):
        native = self.home / '.codex/skills/3d-assets'
        native.mkdir(parents=True)
        (native / 'SKILL.md').write_bytes(b'native skill bytes')
        other = self.root / 'native-audio'
        other.mkdir()
        (other / 'SKILL.md').write_bytes(b'other native skill')
        self.skills.mkdir(parents=True)
        (self.skills / 'game-audio').symlink_to(other, target_is_directory=True)
        result = self.install()
        self.assertEqual(result['installed'], [])
        self.assertEqual(sorted(result['conflicts']), ['3d-assets', 'game-audio'])
        self.assertEqual((native / 'SKILL.md').read_bytes(), b'native skill bytes')
        self.assertEqual((self.skills / 'game-audio').readlink(), other)
        self.assertFalse((self.skills / '3d-assets').exists())

    def test_dangling_native_skill_link_is_preserved_as_a_conflict(self):
        native = self.home / '.codex/skills/3d-assets'
        native.parent.mkdir(parents=True)
        missing = self.root / 'temporarily-unavailable-native-install'
        native.symlink_to(missing, target_is_directory=True)
        result = self.install()
        self.assertIn('3d-assets', result['conflicts'])
        self.assertNotIn('3d-assets', result['installed'])
        self.assertEqual(native.readlink(), missing)
        self.assertFalse((self.skills / '3d-assets').exists())

    def test_update_repoints_only_owned_projection_and_retains_previous_bytes(self):
        first = self.install()
        old = (self.skills / '3d-assets').resolve()
        replacement = self.skills / 'game-audio'
        replacement.unlink()
        replacement.mkdir()
        (replacement / 'SKILL.md').write_bytes(b'user replacement')
        changed = deepcopy(self.payload)
        key = '3d-assets/.agents/skills/3d-assets/SKILL.md'
        changed['files'][key] = encoded(b'# New skill documentation\n')
        result = self.install(changed)
        self.assertNotEqual(first['revision'], result['revision'])
        self.assertEqual(result['installed'], ['3d-assets'])
        self.assertEqual(result['conflicts'], ['game-audio'])
        self.assertEqual((self.skills / '3d-assets/SKILL.md').read_bytes(), b'# New skill documentation\n')
        self.assertEqual((old / 'SKILL.md').read_bytes(), self.source_bytes[key])
        self.assertEqual((replacement / 'SKILL.md').read_bytes(), b'user replacement')

    def test_disable_removes_only_links_still_owned_by_this_installation(self):
        self.install()
        replacement = self.root / 'user-audio'
        replacement.mkdir()
        (replacement / 'SKILL.md').write_bytes(b'user skill')
        changed = self.skills / 'game-audio'
        changed.unlink()
        changed.symlink_to(replacement, target_is_directory=True)
        unrelated = self.skills / 'unrelated'
        unrelated.mkdir()
        (unrelated / 'SKILL.md').write_bytes(b'unrelated')
        result = self.install(self.disabled())
        self.assertEqual(result['installed'], [])
        self.assertFalse((self.skills / '3d-assets').is_symlink())
        self.assertEqual(changed.readlink(), replacement)
        self.assertEqual((changed / 'SKILL.md').read_bytes(), b'user skill')
        self.assertEqual((unrelated / 'SKILL.md').read_bytes(), b'unrelated')
        self.assertEqual(json.loads((self.bridge / 'connection.json').read_text())['enabled_skills'], [])

    def test_another_owner_cannot_replace_existing_configuration(self):
        self.install()
        before = (self.bridge / 'connection.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'owns'):
            self.install({**self.payload, 'owner': str(uuid4())})
        self.assertEqual((self.bridge / 'connection.json').read_bytes(), before)

    def test_connection_symlink_is_rejected_without_modifying_target(self):
        self.install()
        outside = self.root / 'outside-descriptor.json'
        outside.write_bytes(b'outside content must remain')
        descriptor = self.bridge / 'connection.json'
        descriptor.unlink()
        descriptor.symlink_to(outside)
        with self.assertRaises(ValueError):
            self.install()
        self.assertTrue(descriptor.is_symlink())
        self.assertEqual(outside.read_bytes(), b'outside content must remain')

    def test_tampered_registry_symlink_cannot_authorize_deletion_of_user_link(self):
        self.install()
        native = self.root / 'user-installed-skill'
        native.mkdir()
        (native / 'SKILL.md').write_bytes(b'preserve user installation')
        user_link = self.skills / '3d-assets'
        user_link.unlink()
        user_link.symlink_to(native, target_is_directory=True)
        fake_registry = self.root / 'untrusted-links.json'
        fake_registry.write_text(json.dumps({'3d-assets': str(native)}))
        registry = self.bridge / 'links.json'
        registry.unlink()
        registry.symlink_to(fake_registry)
        with self.assertRaises(ValueError):
            self.install(self.disabled())
        self.assertTrue(user_link.is_symlink(), 'Reject the registry before using it to remove any link.')
        self.assertEqual(user_link.readlink(), native)
        self.assertEqual((native / 'SKILL.md').read_bytes(), b'preserve user installation')

    def test_tampered_projected_file_symlink_is_rejected_even_when_bytes_match(self):
        self.install()
        projected = (self.skills / '3d-assets').resolve() / 'SKILL.md'
        outside = self.root / 'outside-skill.md'
        content = projected.read_bytes()
        outside.write_bytes(content)
        projected.unlink()
        projected.symlink_to(outside)
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(outside.read_bytes(), content)

    def test_symlinked_install_or_skill_ancestor_is_rejected(self):
        outside = self.root / 'outside-home'
        outside.mkdir()
        (self.home / '.codex').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(list(outside.iterdir()), [])
        (self.home / '.codex').unlink()
        (self.home / '.agents').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(list(outside.iterdir()), [])

    def test_invalid_relative_paths_and_bytes_cannot_escape_projection(self):
        cases = ['../escape.md', '/absolute.md', '3d-assets/../escape.md',
                 'C:/escape.md', '3d-assets\\escape.md', '3d-assets/private.json']
        for name in cases:
            with self.subTest(name=name):
                payload = deepcopy(self.payload)
                payload['files'][name] = encoded(b'forbidden')
                with self.assertRaises(ValueError):
                    self.install(payload)
        with self.assertRaises(ValueError):
            self.install({**self.payload, 'files': {'invalid.md': '!not base64!'}})
        self.assertFalse((self.root / 'escape.md').exists())
        self.assertFalse((self.bridge / 'escape.md').exists())


if __name__ == '__main__':
    unittest.main()
