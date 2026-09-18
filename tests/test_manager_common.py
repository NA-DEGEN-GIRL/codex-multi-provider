"""Common declaration ownership tests using only temporary synthetic profiles."""
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core import common


class CommonSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='codex-common-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source, self.home = self.root / 'source', self.root / 'managed'
        self.source.mkdir()
        self.home.mkdir()

    def donor(self, command='old-command', extra=''):
        (self.source / 'config.toml').write_text(
            'model = "do-not-copy-model"\n'
            '[model_providers.private]\nbase_url = "https://private.example"\n'
            '[mcp_servers."shared.tool"]\n'
            f'command = "{command}"\nargs = ["--tool", "두 단어"]\n'
            'env = { TEST_TOKEN = "synthetic-secret-value" }\n' + extra, encoding='utf-8')

    def config(self):
        return tomllib.loads((self.home / 'config.toml').read_text(encoding='utf-8'))

    def prepare(self):
        return common.prepare_common(self.home, self.source)

    def test_existing_profile_gains_shared_mcp_without_changing_settings_or_auth(self):
        self.donor()
        custom = ('# Keep this comment and profile policy\r\nmodel = "gpt-6-astra"\r\n'
                  'model_provider = "mine"\r\n[model_providers.mine]\r\n'
                  'base_url = "https://mine.example"\r\n[mcp_servers.local]\r\ncommand = "local-only"\r\n')
        (self.home / 'config.toml').write_bytes(custom.encode())
        for filename in ('auth.json', '.credentials.json', 'sessions.json'):
            (self.source / filename).write_text('private-source-sentinel', encoding='utf-8')
            (self.home / filename).write_text('private-profile-sentinel', encoding='utf-8')
        real_open = Path.open
        forbidden = {'auth.json', '.credentials.json', 'sessions.json'}
        def guarded_open(path, *args, **kwargs):
            if path.name in forbidden:
                raise AssertionError('Authentication/history must not be read')
            return real_open(path, *args, **kwargs)
        with patch.object(Path, 'open', guarded_open):
            status = self.prepare()
        self.assertEqual(status['mcp_counts']['added'], 1)
        self.assertTrue((self.home / 'config.toml').read_bytes().startswith(custom.encode()))
        self.assertEqual(self.config()['model_provider'], 'mine')
        self.assertNotIn('private', self.config()['model_providers'])
        self.assertEqual(self.config()['mcp_servers']['shared.tool']['env']['TEST_TOKEN'], 'synthetic-secret-value')
        public = json.dumps(status) + (self.home / common._STATE).read_text(encoding='utf-8')
        self.assertNotIn('synthetic-secret-value', public)
        self.assertNotIn('old-command', public)
        for filename in forbidden:
            self.assertEqual((self.home / filename).read_text(), 'private-profile-sentinel')

    def test_refresh_updates_managed_definition_and_preserves_manual_additions(self):
        self.donor()
        self.prepare()
        config = self.home / 'config.toml'
        with config.open('a', encoding='utf-8') as output:
            output.write('\n# User addition\n[mcp_servers.extra]\ncommand = "mine"\n')
        self.donor('new-command', '[mcp_servers.new]\ncommand = "new-server"\n')
        result = self.prepare()
        self.assertEqual(result['mcp_counts']['updated'], 1)
        self.assertEqual(result['mcp_counts']['added'], 1)
        self.assertEqual(self.config()['mcp_servers']['shared.tool']['command'], 'new-command')
        self.assertEqual(self.config()['mcp_servers']['extra']['command'], 'mine')
        self.assertIn('# User addition', config.read_text(encoding='utf-8'))
        before = {p.name: p.read_bytes() for p in self.home.iterdir() if p.is_file()}
        self.assertEqual(self.prepare()['mcp'], 'unchanged')
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.home.iterdir() if p.is_file()})

    def test_source_deletion_only_removes_unchanged_generated_blocks(self):
        self.donor(extra='[mcp_servers.other]\ncommand = "common-other"\n')
        self.prepare()
        config = self.home / 'config.toml'
        config.write_text(config.read_text(encoding='utf-8').replace('common-other', 'user-customized'), encoding='utf-8')
        (self.source / 'config.toml').write_text('model = "gpt-6-astra"\n', encoding='utf-8')
        result = self.prepare()
        self.assertEqual(result['mcp_counts']['removed'], 1)
        self.assertNotIn('shared.tool', self.config()['mcp_servers'])
        self.assertEqual(self.config()['mcp_servers']['other']['command'], 'user-customized')

    def test_manual_definition_edit_or_deletion_is_not_undone_on_future_refresh(self):
        self.donor()
        self.prepare()
        config = self.home / 'config.toml'
        config.write_text(config.read_text(encoding='utf-8').replace('old-command', 'manual-command'), encoding='utf-8')
        self.donor('new-command')
        self.prepare()
        self.assertEqual(self.config()['mcp_servers']['shared.tool']['command'], 'manual-command')
        config.write_text(common._BLOCK.sub('', config.read_text(encoding='utf-8')), encoding='utf-8')
        self.prepare()
        self.assertNotIn('shared.tool', self.config().get('mcp_servers', {}))

    def test_preexisting_unmarked_mcp_is_kept_even_when_source_uses_same_name(self):
        self.donor()
        original = '[mcp_servers."shared.tool"]\ncommand = "my-command"\n'
        (self.home / 'config.toml').write_text(original, encoding='utf-8')
        result = self.prepare()
        self.assertEqual((self.home / 'config.toml').read_text(), original)
        self.assertEqual(result['mcp_counts']['preserved'], 1)

    def test_invalid_or_missing_source_does_not_remove_generated_mcp_or_leak_parse_input(self):
        self.donor()
        self.prepare()
        before = (self.home / 'config.toml').read_bytes()
        (self.source / 'config.toml').write_text('synthetic-secret-value = "unterminated', encoding='utf-8')
        result = self.prepare()
        self.assertEqual((self.home / 'config.toml').read_bytes(), before)
        self.assertNotIn('synthetic-secret-value', json.dumps(result))
        (self.source / 'config.toml').unlink()
        self.prepare()
        self.assertEqual((self.home / 'config.toml').read_bytes(), before)

    def test_invalid_profile_error_is_sanitized_and_does_not_write(self):
        self.donor()
        invalid = 'synthetic-profile-secret = "unterminated'
        (self.home / 'config.toml').write_text(invalid, encoding='utf-8')
        with self.assertRaises(ValueError) as caught:
            self.prepare()
        self.assertNotIn('synthetic-profile-secret', str(caught.exception))
        self.assertEqual((self.home / 'config.toml').read_text(), invalid)
        self.assertFalse((self.home / common._STATE).exists())

    def test_existing_skills_directory_is_not_falsely_reported_as_shared(self):
        self.donor()
        (self.source / 'skills').mkdir()
        (self.home / 'skills').mkdir()
        (self.home / 'skills/local.md').write_text('personal', encoding='utf-8')
        result = self.prepare()
        self.assertEqual(result['skills'], 'profile_directory_preserved')
        self.assertEqual((self.home / 'skills/local.md').read_text(), 'personal')

    def test_shared_skills_follow_source_changes_and_instructions_preserve_edits(self):
        self.donor()
        (self.source / 'skills').mkdir()
        (self.source / 'skills/SKILL.md').write_text('first', encoding='utf-8')
        (self.source / 'AGENTS.md').write_text('common instructions', encoding='utf-8')
        result = self.prepare()
        self.assertEqual(result['skills'], 'shared_directory')
        (self.source / 'skills/SKILL.md').write_text('second', encoding='utf-8')
        self.assertEqual((self.home / 'skills/SKILL.md').read_text(), 'second')
        (self.source / 'AGENTS.md').write_text('updated instructions', encoding='utf-8')
        self.prepare()
        self.assertEqual((self.home / 'AGENTS.md').read_text(), 'updated instructions')
        (self.home / 'AGENTS.md').write_text('personal instructions', encoding='utf-8')
        (self.source / 'AGENTS.md').write_text('third instructions', encoding='utf-8')
        self.assertEqual(self.prepare()['instructions'], 'profile_file_preserved')
        self.assertEqual((self.home / 'AGENTS.md').read_text(), 'personal instructions')
        self.assertFalse((self.home / 'plugins').exists())

    def test_original_home_cannot_be_used_as_destination(self):
        self.donor()
        before = (self.source / 'config.toml').read_bytes()
        with self.assertRaises(ValueError):
            common.prepare_common(self.source, self.source)
        self.assertEqual((self.source / 'config.toml').read_bytes(), before)

    def test_inline_mcp_table_is_preserved_when_toml_cannot_be_extended(self):
        self.donor()
        original = 'mcp_servers = { local = { command = "my-command" } }\n'
        (self.home / 'config.toml').write_text(original, encoding='utf-8')
        result = self.prepare()
        self.assertEqual((self.home / 'config.toml').read_text(), original)
        self.assertEqual(result['mcp'], 'unchanged')
        self.assertTrue(result['warnings'])

    def test_provider_policy_render_keeps_common_block_eligible_for_refresh(self):
        from manager_core.providers import ProviderRegistry
        self.donor()
        self.prepare()
        registry = ProviderRegistry(self.root)
        generated = registry.render_for_host(str(self.home), False, [],
                                             existing_config=common._read(self.home / 'config.toml'))
        (self.home / 'config.toml').write_text(generated['files']['config.toml'], encoding='utf-8')
        self.donor('after-provider-render')
        self.assertEqual(self.prepare()['mcp_counts']['updated'], 1)
        config = self.config()
        self.assertEqual(config['mcp_servers']['shared.tool']['command'], 'after-provider-render')
        self.assertEqual(config['subagent_model_provider_allowlist'], [])


if __name__ == '__main__':
    unittest.main()
