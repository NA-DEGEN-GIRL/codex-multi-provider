import json
from pathlib import Path
import shutil
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import prepare_profiles
import desktop_launch


class FlashPolicyTests(unittest.TestCase):
    def test_regenerated_cli_profiles_force_flash_and_preserve_gpt_effort(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config').mkdir()
            shutil.copyfile(prepare_profiles.ROOT / 'config/deepseek-catalog.json', root / 'config/deepseek-catalog.json')
            with patch.object(prepare_profiles, 'ROOT', root):
                prepare_profiles.prepare()
                for mode in ('upstream', 'runtime'):
                    home = root / 'profiles' / mode
                    role_path = home / 'agents/deepseek.toml'
                    role = tomllib.loads(role_path.read_text(encoding='utf-8'))
                    self.assertEqual(role['model_reasoning_effort'], 'max')
                    catalog = json.loads((home / 'deepseek-models.json').read_text(encoding='utf-8'))['models'][0]
                    self.assertEqual([p['effort'] for p in catalog['supported_reasoning_levels']], ['max'])
                    self.assertEqual(tomllib.loads((home / 'config.toml').read_text())['model_reasoning_effort'], 'low')
                    role_path.write_text(role_path.read_text(encoding='utf-8').replace('"max"', '"low"'), encoding='utf-8')
                prepare_profiles.prepare()
                self.assertEqual(tomllib.loads((root / 'profiles/runtime/agents/deepseek.toml').read_text())['model_reasoning_effort'], 'max')

    def test_existing_desktop_policy_updates_only_flash_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'agents').mkdir()
            parent = 'model = "gpt-6-astra"\nmodel_reasoning_effort = "medium"\n# User preferences\n'
            (home / 'config.toml').write_text(parent)
            (home / 'auth.json').write_text('fictional auth sentinel')
            (home / 'agents/deepseek.toml').write_text('model = "deepseek-flash"\nmodel_reasoning_effort = "low"\ndeveloper_instructions = "Preserve this instruction"\n')
            (home / 'deepseek-models.json').write_text(json.dumps({'models': [{'slug': 'deepseek-flash', 'default_reasoning_level': 'low', 'supported_reasoning_levels': [{'effort': 'low'}]}]}))
            desktop_launch.ensure_profile({'model': 'deepseek-flash'}, destination=home)
            role = tomllib.loads((home / 'agents/deepseek.toml').read_text())
            self.assertEqual(role['model_reasoning_effort'], 'max')
            self.assertEqual(role['developer_instructions'], 'Preserve this instruction')
            self.assertEqual((home / 'config.toml').read_text(), parent)
            self.assertEqual((home / 'auth.json').read_text(), 'fictional auth sentinel')
            self.assertEqual(json.loads((home / 'deepseek-models.json').read_text())['models'][0]['default_reasoning_level'], 'max')
            before = (home / 'agents/deepseek.toml').stat().st_mtime_ns
            desktop_launch.ensure_profile({'model': 'deepseek-flash'}, destination=home)
            self.assertEqual((home / 'agents/deepseek.toml').stat().st_mtime_ns, before)


if __name__ == '__main__':
    unittest.main()
