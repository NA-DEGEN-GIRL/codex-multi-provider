"""Shared plugin mirror tests using only temporary synthetic homes."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core import plugin_sync
from manager_core.plugin_sync import MARKETPLACE, PluginSync, edit_config, inventory, signature
from manager_core.store import Store


SKILL = '---\nname: demo\ndescription: Synthetic mirror skill.\n---\n\nBody.\n'


class PluginSyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='codex-plugin-sync-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.store = Store(self.root)
        self.original = self.root / 'homes/original'
        self.homes = {'02': self.root / 'homes/02', '03': self.root / 'homes/03'}
        for alias in self.homes:
            profile = self.store.add_profile(alias)
            self.homes[alias].mkdir(parents=True, exist_ok=True)
            (self.homes[alias] / 'auth.json').write_text(f'auth-{alias}', encoding='utf-8')
        self.original.mkdir(parents=True, exist_ok=True)
        (self.original / 'auth.json').write_text('auth-original', encoding='utf-8')

        def relink(data):
            for profile in data['profiles']:
                profile['home'] = str(self.homes[profile['alias']])
        self.store.mutate(relink)
        self.sync = PluginSync(self.store, source=self.original)

    def install_native(self, home, *, plugin='github', marketplace='openai-curated-remote',
                       version='1.0.0', marker=True, link=False, modified=None):
        base = home / 'plugins/cache' / marketplace / plugin
        root = base / version
        (root / '.codex-plugin').mkdir(parents=True, exist_ok=True)
        (root / '.codex-plugin/plugin.json').write_text(json.dumps({
            'name': plugin, 'version': version, 'description': 'Synthetic plugin',
            'skills': './skills/', 'apps': './.app.json', 'hooks': './hooks/hooks.json'}) + '\n',
            encoding='utf-8')
        (root / '.app.json').write_text('{"apps":["synthetic"]}', encoding='utf-8')
        (root / 'hooks').mkdir(exist_ok=True)
        (root / 'hooks/hooks.json').write_text('{"hooks":[]}', encoding='utf-8')
        (root / 'skills/demo').mkdir(parents=True, exist_ok=True)
        (root / 'skills/demo/SKILL.md').write_text(SKILL, encoding='utf-8')
        if marker:
            (base / '.codex-remote-plugin-install.json').write_text(
                json.dumps({'schema_version': 1, 'remote_plugin_id': 'plugin_synthetic'}),
                encoding='utf-8')
        if link:
            try:
                os.symlink(home / 'auth.json', root / 'outside.txt')
            except (OSError, NotImplementedError):
                self.skipTest('File symlinks are not available for this account.')
        if modified is not None:
            stamp = time.time() - modified
            for path in (root, base):
                os.utime(path, (stamp, stamp))
        data = self.home(home)
        data.setdefault('marketplaces', {})[marketplace] = dict(
            source_type='local', source=str(self.root / 'marketplace-source'))
        data.setdefault('plugins', {})[f'{plugin}@{marketplace}'] = dict(enabled=True)
        self.write_config(home, data)
        return base

    def home(self, home):
        path = home / 'config.toml'
        return tomllib.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}

    def write_config(self, home, data):
        home.mkdir(parents=True, exist_ok=True)
        lines = []
        for name, value in data.get('marketplaces', {}).items():
            lines.append(f'[marketplaces.{name}]')
            lines += [f'{key} = {json.dumps(item)}' for key, item in value.items()]
            lines.append('')
        for name, value in data.get('plugins', {}).items():
            lines += [f'[plugins."{name}"]',
                      f'enabled = {str(bool(value.get("enabled", True))).lower()}', '']
        (home / 'config.toml').write_text('\n'.join(lines), encoding='utf-8')

    def mirror(self, home, plugin='github'):
        return home / 'plugins/cache' / MARKETPLACE / plugin

    def signal(self):
        return json.loads((self.store.directory / 'record-signals/plugins.json').read_text(encoding='utf-8'))

    def test_native_install_is_mirrored_faithfully_without_account_state(self):
        private = self.homes['02'] / 'plugins/data/github-openai-curated-remote/session.json'
        private.parent.mkdir(parents=True)
        private.write_text('profile-private', encoding='utf-8')
        self.install_native(self.homes['02'])
        result = self.sync.reconcile(force=True)
        self.assertEqual([], result['errors'])
        for home in (self.original, self.homes['03']):
            bundle = self.mirror(home) / '1.0.0'
            self.assertTrue((bundle / '.codex-plugin/plugin.json').is_file())
            self.assertTrue((bundle / '.app.json').is_file())
            self.assertTrue((bundle / 'hooks/hooks.json').is_file())
            self.assertTrue((bundle / 'skills/demo/SKILL.md').is_file())
            self.assertFalse((self.mirror(home) / '.codex-remote-plugin-install.json').exists())
            self.assertFalse((home / 'plugins/data/github-openai-curated-remote/session.json').exists())
            config = self.home(home)
            self.assertEqual(config['marketplaces'][MARKETPLACE]['source'],
                             str(self.store.directory / 'shared-plugins' / MARKETPLACE))
            self.assertTrue(config['plugins'][f'github@{MARKETPLACE}']['enabled'])
        self.assertFalse(self.mirror(self.homes['02']).exists())
        self.assertNotIn(MARKETPLACE, self.home(self.homes['02']).get('marketplaces', {}))
        for home, expected in ((self.original, 'auth-original'), (self.homes['02'], 'auth-02'),
                               (self.homes['03'], 'auth-03')):
            self.assertEqual(expected, (home / 'auth.json').read_text(encoding='utf-8'))
        registry = self.sync.status()
        self.assertEqual('openai-curated-remote', registry['shared']['github']['marketplace'])
        self.assertEqual(1, self.signal()['version'])
        self.assertTrue(self.signal()['revision'])
        self.assertEqual(0, self.sync.reconcile(force=True)['applied'])

    def test_same_version_republish_updates_once_and_signals_once(self):
        self.install_native(self.homes['02'])
        self.sync.reconcile(force=True)
        first = self.signal()['revision']
        root = self.homes['02'] / 'plugins/cache/openai-curated-remote/github/1.0.0'
        (root / 'skills/demo/extra.txt').write_text('republished\n', encoding='utf-8')
        os.utime(root, None)
        result = self.sync.reconcile(force=True)
        self.assertGreaterEqual(result['applied'], 1)
        second = self.signal()['revision']
        self.assertNotEqual(first, second)
        for home in (self.original, self.homes['03']):
            self.assertTrue((self.mirror(home) / '1.0.0/skills/demo/extra.txt').is_file())
        self.assertEqual(0, self.sync.reconcile(force=True)['applied'])
        self.assertEqual(second, self.signal()['revision'])

    def test_peer_disable_is_preserved_and_native_peer_drops_the_mirror(self):
        self.install_native(self.homes['02'])
        self.sync.reconcile(force=True)
        data = self.home(self.homes['03'])
        data['plugins'][f'github@{MARKETPLACE}'] = dict(enabled=False)
        self.write_config(self.homes['03'], data)
        self.sync.reconcile(force=True)
        self.assertFalse(self.home(self.homes['03'])['plugins'][f'github@{MARKETPLACE}']['enabled'])
        self.install_native(self.homes['03'])
        self.sync.reconcile(force=True)
        self.assertFalse(self.mirror(self.homes['03']).exists())
        self.assertNotIn(f'github@{MARKETPLACE}', self.home(self.homes['03']).get('plugins', {}))
        self.assertTrue((self.mirror(self.original) / '1.0.0').is_dir())

    def test_owner_uninstall_removes_mirrors_and_blocks_stale_resurrect(self):
        self.install_native(self.homes['02'])
        self.sync.reconcile(force=True)
        first = self.signal()['revision']
        shutil.rmtree(self.homes['02'] / 'plugins/cache/openai-curated-remote/github')
        result = self.sync.reconcile(force=True)
        self.assertEqual(1, result['removed'])
        self.assertNotEqual(first, self.signal()['revision'])
        for home in (self.original, self.homes['03']):
            self.assertFalse(self.mirror(home).exists())
            self.assertNotIn(f'github@{MARKETPLACE}', self.home(home).get('plugins', {}))
            self.assertNotIn(MARKETPLACE, self.home(home).get('marketplaces', {}))
        self.assertIn('github', self.sync.status()['removed'])
        self.install_native(self.homes['03'], modified=3600)
        self.sync.reconcile(force=True)
        self.assertNotIn('github', self.sync.status()['shared'])
        self.assertFalse(self.mirror(self.original).exists())
        self.install_native(self.homes['02'])
        self.sync.reconcile(force=True)
        self.assertIn('github', self.sync.status()['shared'])
        self.assertTrue((self.mirror(self.original) / '1.0.0').is_dir())
        self.assertNotIn('github', self.sync.status()['removed'])

    def test_peer_removal_is_an_optout_and_is_not_reinstalled(self):
        self.install_native(self.homes['02'])
        self.sync.reconcile(force=True)
        shutil.rmtree(self.mirror(self.homes['03']))
        data = self.home(self.homes['03'])
        data['plugins'].pop(f'github@{MARKETPLACE}')
        self.write_config(self.homes['03'], data)
        self.sync.reconcile(force=True)
        self.assertFalse(self.mirror(self.homes['03']).exists())
        self.assertNotIn(f'github@{MARKETPLACE}', self.home(self.homes['03']).get('plugins', {}))
        self.assertTrue(self.sync.status()['optout'])
        self.assertTrue((self.mirror(self.original) / '1.0.0').is_dir())

    def test_first_install_then_immediate_removal_is_an_optout(self):
        self.install_native(self.homes['02'])
        self.sync.reconcile(force=True)
        # A following idle tick must not lose the post-apply observation.
        self.assertEqual(0, self.sync.reconcile(force=True)['applied'])
        shutil.rmtree(self.mirror(self.homes['03']))
        data = self.home(self.homes['03'])
        data['plugins'].pop(f'github@{MARKETPLACE}')
        self.write_config(self.homes['03'], data)
        self.sync.reconcile()
        self.assertFalse(self.mirror(self.homes['03']).exists())
        self.assertNotIn(f'github@{MARKETPLACE}', self.home(self.homes['03']).get('plugins', {}))
        self.assertTrue(self.sync.status()['optout'])
        self.assertTrue((self.mirror(self.original) / '1.0.0').is_dir())

    def test_version_update_replaces_the_mirror_bundle(self):
        self.install_native(self.homes['02'])
        self.sync.reconcile(force=True)
        self.install_native(self.homes['02'], version='1.1.0', marker=False)
        self.sync.reconcile(force=True)
        for home in (self.original, self.homes['03']):
            self.assertTrue((self.mirror(home) / '1.1.0/.codex-plugin/plugin.json').is_file())
            self.assertFalse((self.mirror(home) / '1.0.0').exists())
        self.assertEqual('1.1.0', self.sync.status()['shared']['github']['version'])

    def test_symlinked_bundle_content_is_never_copied(self):
        self.install_native(self.homes['02'], link=True)
        self.sync.reconcile(force=True)
        self.assertFalse((self.mirror(self.original) / '1.0.0/outside.txt').exists())

    def test_linked_mirror_root_and_config_are_rejected(self):
        self.install_native(self.homes['02'])
        outside = self.root / 'outside'
        outside.mkdir()
        cache = self.homes['03'] / 'plugins/cache'
        cache.mkdir(parents=True)
        try:
            os.symlink(outside, cache / MARKETPLACE, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('Directory symlinks are not available for this account.')
        result = self.sync.reconcile(force=True)
        self.assertTrue(result['errors'])
        self.assertEqual([], list(outside.iterdir()))
        self.assertNotIn(MARKETPLACE, self.home(self.homes['03']).get('marketplaces', {}))
        self.assertTrue((self.mirror(self.original) / '1.0.0').is_dir())

    def test_linked_config_is_rejected_without_touching_its_target(self):
        self.install_native(self.homes['02'])
        target = self.root / 'outside-config.toml'
        target.write_text('model = "keep"\n', encoding='utf-8')
        config = self.homes['03'] / 'config.toml'
        self.homes['03'].mkdir(parents=True, exist_ok=True)
        if config.exists():
            config.unlink()
        try:
            os.symlink(target, config)
        except (OSError, NotImplementedError):
            self.skipTest('File symlinks are not available for this account.')
        result = self.sync.reconcile(force=True)
        self.assertTrue(result['errors'])
        self.assertEqual('model = "keep"\n', target.read_text(encoding='utf-8'))

    def test_damaged_registry_is_preserved_and_reported(self):
        self.install_native(self.homes['02'])
        registry = self.store.directory / 'plugin-sync.json'
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text('{not json', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.sync.reconcile(force=True)
        self.assertEqual('{not json', registry.read_text(encoding='utf-8'))
        registry.write_text(json.dumps({
            'version': 1, 'shared': {'../evil': {'marketplace': 'openai-curated-remote',
                                                 'version': '1.0.0', 'origin': 'x',
                                                 'installed_at_ns': 1}},
            'removed': {}, 'observations': {}, 'optout': {}}), encoding='utf-8')
        with self.assertRaises(ValueError):
            self.sync.reconcile(force=True)
        self.assertIn('../evil', registry.read_text(encoding='utf-8'))
        self.assertFalse((self.store.directory / 'shared-plugins').exists())

    def test_inventory_is_bounded_and_prefers_native_installations(self):
        base = self.homes['02'] / 'plugins/cache/openai-curated-remote/github'
        (base / '.codex-plugin').mkdir(parents=True)
        (base / '.codex-plugin/plugin.json').write_text(
            json.dumps({'name': 'github', 'version': '9.9.9'}), encoding='utf-8')
        (base / '9.9.9').mkdir()
        for index in range(plugin_sync._MAX_VERSIONS + 3):
            (base / f'0.0.{index}').mkdir()
        self.mirror(self.original, 'github').mkdir(parents=True)
        (self.mirror(self.original, 'github') / '1.0.0').mkdir()
        found = inventory(self.homes['02'])
        self.assertTrue(found['github']['native'])
        self.assertLessEqual(len(found['github']['versions']), plugin_sync._MAX_VERSIONS)
        self.assertIsInstance(signature(self.homes['02']), tuple)

    def test_bundled_marketplaces_are_never_promoted_or_mirrored(self):
        for marketplace, name, version in (('openai-primary-runtime', 'documents', '26.909.61513'),
                                           ('openai-bundled', 'browser', '26.911.61220')):
            bundle = self.homes['02'] / 'plugins/cache' / marketplace / name / version
            (bundle / '.codex-plugin').mkdir(parents=True)
            (bundle / '.codex-plugin/plugin.json').write_text(
                json.dumps({'name': name, 'version': version}), encoding='utf-8')
        data = self.home(self.homes['02'])
        data.setdefault('marketplaces', {})['openai-primary-runtime'] = dict(
            source_type='local', source=str(self.root / 'bundled-runtime'))
        data['marketplaces']['openai-bundled'] = dict(
            source_type='local', source=str(self.root / 'bundled'))
        data.setdefault('plugins', {})['documents@openai-primary-runtime'] = dict(enabled=True)
        data['plugins']['browser@openai-bundled'] = dict(enabled=True)
        self.write_config(self.homes['02'], data)
        result = self.sync.reconcile(force=True)
        self.assertEqual([], result['errors'])
        self.assertEqual({}, self.sync.status()['shared'])
        self.assertFalse((self.store.directory / 'shared-plugins').exists())
        for home in (self.original, self.homes['03']):
            self.assertFalse(self.mirror(home, 'documents').exists())
            self.assertFalse(self.mirror(home, 'browser').exists())
            self.assertNotIn(MARKETPLACE, self.home(home).get('marketplaces', {}))
        signal = self.store.directory / 'record-signals/plugins.json'
        first = json.loads(signal.read_text(encoding='utf-8'))['revision'] if signal.exists() else None
        self.sync.reconcile(force=True)
        second = json.loads(signal.read_text(encoding='utf-8'))['revision'] if signal.exists() else None
        self.assertEqual(first, second)
        self.assertFalse(inventory(self.homes['02'])['documents']['promotable'])

    def test_legacy_bundled_record_is_dropped_without_tombstone(self):
        record = dict(name='documents', marketplace='openai-primary-runtime',
                      version='26.909.61513', origin='unused', installed_at_ns=1, enabled=True)
        published = self.store.directory / 'shared-plugins' / MARKETPLACE / 'plugins/documents'
        (published / '.codex-plugin').mkdir(parents=True)
        (published / '.codex-plugin/plugin.json').write_text(
            json.dumps({'name': 'documents', 'version': '26.909.61513'}), encoding='utf-8')
        for home in (self.original, self.homes['03']):
            bundle = self.mirror(home, 'documents') / '26.909.61513'
            (bundle / '.codex-plugin').mkdir(parents=True)
            (bundle / '.codex-plugin/plugin.json').write_text('{}', encoding='utf-8')
            data = self.home(home)
            data.setdefault('marketplaces', {})[MARKETPLACE] = dict(
                source_type='local',
                source=str(self.store.directory / 'shared-plugins' / MARKETPLACE))
            data.setdefault('plugins', {})[f'documents@{MARKETPLACE}'] = dict(enabled=True)
            self.write_config(home, data)
        registry = self.store.directory / 'plugin-sync.json'
        atomic = dict(version=1, shared={'documents': record}, removed={},
                      observations={}, optout={})
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(json.dumps(atomic), encoding='utf-8')
        self.sync.reconcile(force=True)
        status = self.sync.status()
        self.assertEqual({}, status['shared'])
        self.assertNotIn('documents', status['removed'])
        self.assertFalse(published.exists())
        for home in (self.original, self.homes['03']):
            self.assertFalse(self.mirror(home, 'documents').exists())
            self.assertNotIn(f'documents@{MARKETPLACE}', self.home(home).get('plugins', {}))

    def test_vanished_source_entry_is_retried_once(self):
        self.install_native(self.homes['02'])
        real = plugin_sync.shutil.copyfile
        calls = dict(count=0)
        victim = 'SKILL.md'

        def flaky(source, destination):
            if str(source).endswith(victim) and calls['count'] == 0:
                calls['count'] += 1
                raise FileNotFoundError(str(source))
            return real(source, destination)

        with mock.patch.object(plugin_sync.shutil, 'copyfile', flaky):
            result = self.sync.reconcile(force=True)
        self.assertEqual([], result['errors'])
        self.assertEqual(1, calls['count'])
        self.assertTrue((self.mirror(self.original) / '1.0.0/skills/demo/SKILL.md').is_file())

    def test_store_state_is_not_locked_while_bundles_are_copied(self):
        self.install_native(self.homes['02'])
        real = plugin_sync.shutil.copyfile
        state = dict(calls=0)
        finished = threading.Event()

        def probe(source, destination):
            if state['calls'] == 0:
                state['calls'] += 1

                def mutate():
                    self.store.mutate(lambda data: data.__setitem__('probe_marker', 7))
                    finished.set()

                worker = threading.Thread(target=mutate, daemon=True)
                worker.start()
                worker.join(timeout=10)
                self.assertFalse(worker.is_alive(), 'state lock was held during the copy')
            return real(source, destination)

        with mock.patch.object(plugin_sync.shutil, 'copyfile', probe):
            result = self.sync.reconcile(force=True)
        self.assertEqual([], result['errors'])
        self.assertTrue(finished.is_set())
        self.assertEqual(7, self.store.read().get('probe_marker'))

    def test_busy_plugin_lock_returns_busy_without_writing(self):
        self.install_native(self.homes['02'])
        from manager_core.updates import _lock_file, _unlock_file
        handle = _lock_file(self.sync.lock_path)
        try:
            started = time.monotonic()
            result = self.sync.reconcile(force=True)
            elapsed = time.monotonic() - started
        finally:
            _unlock_file(handle)
        self.assertTrue(result.get('busy'))
        self.assertEqual(0, result['applied'])
        self.assertLess(elapsed, 5)
        self.assertFalse((self.store.directory / 'shared-plugins').exists())
        self.assertFalse(self.mirror(self.original).exists())
        self.assertFalse(self.sync.reconcile(force=True).get('busy'))
        self.assertTrue((self.mirror(self.original) / '1.0.0').is_dir())

    def test_ancestor_link_under_home_is_rejected(self):
        self.install_native(self.homes['02'])
        outside = self.root / 'outside-home'
        (outside / 'cache').mkdir(parents=True)
        home = self.homes['03']
        try:
            os.symlink(outside, home / 'plugins', target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('Directory symlinks are not available for this account.')
        result = self.sync.reconcile(force=True)
        self.assertTrue(result['errors'])
        self.assertEqual([], list((outside / 'cache').iterdir()))
        self.assertNotIn(MARKETPLACE, self.home(home).get('marketplaces', {}))
        self.assertTrue((self.mirror(self.original) / '1.0.0').is_dir())

    def test_shared_root_ancestor_link_is_rejected(self):
        self.install_native(self.homes['02'])
        outside = self.root / 'outside-shared'
        outside.mkdir()
        try:
            os.symlink(outside, self.store.directory / 'shared-plugins', target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('Directory symlinks are not available for this account.')
        result = self.sync.reconcile(force=True)
        self.assertTrue(result['errors'])
        self.assertEqual([], list(outside.iterdir()))
        self.assertFalse(self.mirror(self.original).exists())

    def test_canonical_home_participates_only_when_named_or_registered(self):
        unnamed = PluginSync(self.store)
        self.assertNotIn('original', [alias for alias, _ in unnamed.homes()])
        self.assertNotIn(str(self.original.resolve()),
                         [str(home.resolve()) for _, home in unnamed.homes()])
        named = PluginSync(self.store, source=self.original)
        self.assertIn(str(self.original.resolve()),
                      [str(home.resolve()) for _, home in named.homes()])


class ConfigEditTests(unittest.TestCase):
    def test_edit_preserves_unrelated_toml_and_is_idempotent(self):
        text = ('# keep me\r\n'
                'model = "synthetic"\r\n'
                '[mcp_servers.local]\r\ncommand = "tool"\r\n'
                '[plugins."browser@openai-bundled"]\r\nenabled = true\r\n'
                '[plugins]\r\n"legacy@local" = { enabled = false }\r\n')
        root = Path('C:/synthetic/shared')
        updated = edit_config(text, marketplace_root=root,
                              plugin_states={'github@' + MARKETPLACE: True})
        again = edit_config(updated, marketplace_root=root,
                            plugin_states={'github@' + MARKETPLACE: True})
        self.assertEqual(updated, again)
        parsed = tomllib.loads(updated)
        self.assertEqual('synthetic', parsed['model'])
        self.assertEqual('tool', parsed['mcp_servers']['local']['command'])
        self.assertTrue(parsed['plugins']['browser@openai-bundled']['enabled'])
        self.assertFalse(parsed['plugins']['legacy@local']['enabled'])
        self.assertTrue(parsed['plugins'][f'github@{MARKETPLACE}']['enabled'])
        self.assertEqual(str(root), parsed['marketplaces'][MARKETPLACE]['source'])
        self.assertIn('# keep me\r\n', updated)
        removed = edit_config(updated, marketplace_root=None,
                              plugin_states={'github@' + MARKETPLACE: None})
        self.assertNotEqual(updated, removed)
        parsed = tomllib.loads(removed)
        self.assertNotIn(f'github@{MARKETPLACE}', parsed.get('plugins', {}))
        self.assertNotIn(MARKETPLACE, parsed.get('marketplaces', {}))
        self.assertTrue(parsed['plugins']['browser@openai-bundled']['enabled'])
        self.assertEqual(removed, edit_config(removed, marketplace_root=None,
                                              plugin_states={'github@' + MARKETPLACE: None}))

    def test_edit_handles_root_level_inline_entries(self):
        text = ('[marketplaces]\ncodex-manager-shared = { source_type = "local", source = "old" }\n'
                'other = { source_type = "local", source = "keep" }\n'
                '[plugins]\n"github@codex-manager-shared" = { enabled = true }\n'
                '"browser@openai-bundled" = { enabled = true }\n')
        root = Path('C:/synthetic/new-shared')
        updated = edit_config(text, marketplace_root=root,
                              plugin_states={'github@' + MARKETPLACE: False})
        parsed = tomllib.loads(updated)
        self.assertEqual(str(root), parsed['marketplaces'][MARKETPLACE]['source'])
        self.assertEqual('keep', parsed['marketplaces']['other']['source'])
        self.assertFalse(parsed['plugins'][f'github@{MARKETPLACE}']['enabled'])
        self.assertTrue(parsed['plugins']['browser@openai-bundled']['enabled'])


if __name__ == '__main__':
    unittest.main()
