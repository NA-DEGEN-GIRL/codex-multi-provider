import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.app_preferences import prepare, merge_desktop


class AppPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source, self.target = (Path(self.temp.name) / name for name in ('source', 'target'))
        self.source.mkdir()
        (self.source / 'config.toml').write_text('[desktop]\nappearanceTheme="dark"\npermissionMode="full-access"\n')
        self.state = '.codex-global-state.json'
        (self.source / self.state).write_text(json.dumps({'electron-persisted-atom-state': {
            'last_completed_onboarding': 12345, 'electron:onboarding-welcome-pending': True,
            'composer-prompt-drafts-v2': {'private': 'private draft'}, 'permission-selection-by-host-id:local': 'full-access'},
            'queued-follow-ups': ['private prompt']}))

    def test_only_basic_settings_and_setup_completion_are_applied(self):
        before = (self.source / self.state).read_bytes()
        prepare(self.target, self.source, account_id='target-account')
        config = tomllib.loads((self.target / 'config.toml').read_text())
        self.assertEqual(config['desktop'], {'appearanceTheme': 'dark'})
        state = json.loads((self.target / self.state).read_text())
        self.assertEqual(state, {'local-projects': {}, 'thread-project-assignments': {},
            'codex-managed-remote-connections': [], 'remote-connection-auto-connect-by-host-id': {},
            'project-order': [], 'electron-persisted-atom-state': {
            'last_completed_onboarding': 12345, 'electron:onboarding-projectless-completed': True,
            'electron:onboarding-welcome-pending': False,
            'electron:onboarding-conversational-completed-by-account-id': {'target-account': True}}})
        self.assertEqual((self.source / self.state).read_bytes(), before)

    def test_refresh_applies_common_theme_again_and_preserves_other_atoms(self):
        prepare(self.target, self.source)
        config = self.target / 'config.toml'
        (self.source / 'config.toml').write_text('[desktop]\nappearanceTheme="light"\n')
        prepare(self.target, self.source)
        self.assertEqual(tomllib.loads(config.read_text())['desktop']['appearanceTheme'], 'light')
        config.write_text(config.read_text().replace('"light"', '"system"'))
        (self.target / self.state).write_text(json.dumps({'other': 4, 'electron-persisted-atom-state': {'private': 5}}))
        result = prepare(self.target, self.source)
        self.assertEqual(result['desktop'], 'updated')
        self.assertEqual(tomllib.loads(config.read_text())['desktop']['appearanceTheme'], 'light')
        self.assertEqual(json.loads((self.target / self.state).read_text())['other'], 4)

    def test_existing_native_desktop_settings_do_not_skip_common_theme(self):
        self.target.mkdir()
        config = self.target / 'config.toml'
        config.write_text('model="fixture"\n[desktop]\nfollowUpQueueMode="steer"\npermissionMode="limited"\n[windows]\nsandbox="unelevated"\n')
        result = prepare(self.target, self.source)
        self.assertEqual(result['desktop'], 'updated')
        self.assertEqual(tomllib.loads(config.read_text()), {'model': 'fixture',
            'desktop': {'appearanceTheme': 'dark', 'followUpQueueMode': 'steer', 'permissionMode': 'limited'},
            'windows': {'sandbox': 'unelevated'}})

    def test_native_nested_theme_tables_are_replaced_without_touching_permissions(self):
        text = '[desktop]\nappearanceTheme="light"\n[desktop.appearanceDarkChromeTheme]\nopaqueWindows=false\n[desktop.private]\nkeep="profile-only"\n[windows]\nsandbox="unelevated"\n'
        values = {'appearanceTheme': 'dark', 'appearanceDarkChromeTheme': {'opaqueWindows': True, 'surface': '#1e1e1e'}}
        updated = merge_desktop(text, values)
        data = tomllib.loads(updated)
        self.assertEqual(data['desktop'], {**values, 'private': {'keep': 'profile-only'}})
        self.assertEqual(data['windows'], {'sandbox': 'unelevated'})
        self.assertEqual(merge_desktop(updated, values), updated)

    def test_table_names_in_multiline_strings_and_comments_are_not_sections(self):
        text = 'instructions="""\n[desktop]\nnot a real table\n"""\n# [desktop]\n["desktop"]\nappearanceTheme="light"\n[other]\nvalue="keep"\n'
        updated = merge_desktop(text, {'appearanceTheme': 'dark'})
        expected = tomllib.loads(text)
        expected['desktop']['appearanceTheme'] = 'dark'
        self.assertEqual(tomllib.loads(updated), expected)
        self.assertIn('instructions="""\n[desktop]\nnot a real table\n"""', updated)

    def test_dotted_and_inline_desktop_values_preserve_unshared_fields(self):
        for text in ('desktop.appearanceTheme="light"\ndesktop.private=true\n',
                     'desktop={appearanceTheme="light",private=true}\n'):
            with self.subTest(text=text):
                updated = merge_desktop(text, {'appearanceTheme': 'dark'})
                self.assertEqual(tomllib.loads(updated)['desktop'], {'appearanceTheme': 'dark', 'private': True})

    def test_original_home_is_never_a_destination(self):
        with self.assertRaises(ValueError):
            prepare(self.source, self.source)


if __name__ == '__main__':
    unittest.main()
