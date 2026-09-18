import json
import os
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.personal_skills import PersonalSkills, inventory, replace_rules
from manager_core.store import Store
from manager_core.common import _share_skills


class PersonalSkillsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'user/.codex'
        self.source.mkdir(parents=True)
        self.store = Store(self.root / 'manager')
        self.profiles = [self.store.add_profile(alias) for alias in ('01', '02')]
        self.homes = [self.source] + [Path(p['home']) for p in self.profiles]
        for name in ('codex-handoff', '3d-assets', 'game-audio', 'old-skill'):
            folder = self.source / 'skills' / name
            folder.mkdir(parents=True)
            (folder / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: Fixture skill\n---\n', encoding='utf-8')
        for home in self.homes:
            home.mkdir(parents=True, exist_ok=True)
            (home / 'config.toml').write_text('model="fixture"\n[mcp_servers.private]\nenv={TOKEN="synthetic-do-not-change"}\n', encoding='utf-8')
        for home in self.homes[1:]:
            _share_skills(home, self.source, dict(warnings=[]))
        self.manager = PersonalSkills(self.store, self.source)

    def id(self, name):
        return next(r['id'] for r in inventory(self.source) if r['name'] == name)

    def rows(self):
        return {s['name']: s for s in self.manager.list()['skills']}

    def change(self, home, name, enabled):
        path = home / 'config.toml'
        rows = inventory(self.source)
        rules = [r for r in tomllib.loads(path.read_text()) .get('skills', {}).get('config', []) if Path(r.get('path', '')).parent.name != name]
        row = next(r for r in rows if r['name'] == name)
        rules.append(dict(path=row['path'], enabled=enabled))
        path.write_text(replace_rules(path.read_text(), rules), encoding='utf-8')

    def assert_enabled(self, name, enabled):
        row = next(r for r in inventory(self.source) if r['name'] == name)
        for home in self.homes:
            observation = self.manager._observation(home, [row])
            self.assertEqual(observation['values'][row['id']], enabled, str(home))

    def test_initial_disabled_settings_promoted_once_then_registry_is_authority(self):
        self.change(self.source, 'old-skill', False)
        data = self.manager.list()
        self.assertEqual(data['sync']['applied'], 3)
        self.assertEqual(data['sync']['errors'], [])
        self.assert_enabled('old-skill', False)
        self.assertEqual(sum(row['enabled'] for row in data['skills']), 3)
        self.assertTrue(self.manager.registry.is_file())

    def test_manager_toggle_applies_all_and_preserves_other_settings(self):
        self.manager.set(self.id('old-skill'), False)
        self.assert_enabled('old-skill', False)
        for home in self.homes:
            data = tomllib.loads((home / 'config.toml').read_text())
            self.assertEqual(data['model'], 'fixture')
            self.assertEqual(data['mcp_servers']['private']['env']['TOKEN'], 'synthetic-do-not-change')
        self.manager.set(self.id('old-skill'), True)
        self.assert_enabled('old-skill', True)

    def test_edit_in_each_profile_and_original_propagates_both_directions(self):
        self.manager.list()
        for home in self.homes:
            self.change(home, 'old-skill', False)
            self.manager.reconcile()
            self.assert_enabled('old-skill', False)
            self.change(home, 'old-skill', True)
            self.manager.reconcile()
            self.assert_enabled('old-skill', True)

    def test_new_profile_and_restarted_service_do_not_reenable_disabled_skill(self):
        self.manager.set(self.id('old-skill'), False)
        added = self.store.add_profile('03')
        self.homes.append(Path(added['home']))
        self.manager = PersonalSkills(self.store, self.source)
        self.manager.reconcile()
        self.assert_enabled('old-skill', False)

    def test_no_rewrite_on_idle_polls(self):
        self.manager.list()
        before = [(home / 'config.toml').stat().st_mtime_ns for home in self.homes]
        registry_time = self.manager.registry.stat().st_mtime_ns
        self.manager.list(); self.manager.reconcile(force=True)
        self.assertEqual(before, [(home / 'config.toml').stat().st_mtime_ns for home in self.homes])
        self.assertEqual(registry_time, self.manager.registry.stat().st_mtime_ns)

    def test_physical_deletion_from_shared_profile_is_not_resurrected(self):
        self.manager.list()
        path = self.homes[1] / 'skills/old-skill'
        (path / 'SKILL.md').unlink(); path.rmdir()
        self.assertNotIn('old-skill', self.rows())
        self.assertFalse((self.source / 'skills/old-skill').exists())

    def test_delete_and_restore_keep_files_and_previous_enablement(self):
        self.manager.set(self.id('old-skill'), False)
        data = self.manager.delete(self.id('old-skill'))
        self.assertNotIn('old-skill', {row['name'] for row in data['skills']})
        for home in self.homes:
            self.assertFalse((home / 'skills/old-skill').exists())
        self.manager.restore(data['deleted'][0]['id'])
        self.assert_enabled('old-skill', False)
        self.assertTrue((self.source / 'skills/old-skill/SKILL.md').is_file())

    def test_deleting_junction_only_moves_installation_not_repository(self):
        target = self.root / 'repository/skills'
        target.mkdir(parents=True)
        (target / 'SKILL.md').write_text('---\nname: linked\ndescription: fixture\n---\n')
        folder = self.source / 'skills/linked'
        if os.name == 'nt':
            import subprocess
            quote = lambda p: "'" + str(p).replace("'", "''") + "'"
            subprocess.run(['powershell.exe', '-NoProfile', '-Command', 'New-Item -ItemType Junction -Path ' + quote(folder) + ' -Target ' + quote(target)], check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            folder.symlink_to(target, target_is_directory=True)
        data = self.manager.delete(self.id('linked'))
        self.assertFalse(os.path.lexists(folder))
        self.assertTrue((target / 'SKILL.md').is_file())
        self.manager.restore(data['deleted'][0]['id'])
        self.assertEqual(folder.resolve(), target.resolve())
        self.assert_enabled('linked', True)

    def test_system_plugins_and_project_skills_excluded_and_protected(self):
        for path in (self.source / 'skills/.system/fixed', self.source / 'plugins/extra', self.root / '.agents/skills/project'):
            path.mkdir(parents=True); (path / 'SKILL.md').write_text('---\nname: hidden\n---\n')
        self.assertNotIn('hidden', self.rows())
        with self.assertRaises(ValueError): self.manager.delete('../.system')
        with self.assertRaises(ValueError): self.manager.restore('../elsewhere')
        with self.assertRaises(ValueError): self.manager._install_path(self.source / 'skills', '../escape')
        with self.assertRaises(ValueError): self.manager._install_path(self.source / 'skills', '.system')

    def test_malformed_profile_is_reported_and_other_profiles_still_sync(self):
        self.manager.list()
        broken = self.homes[1] / 'config.toml'
        broken.write_text('invalid = [')
        data = self.manager.set(self.id('old-skill'), False)
        self.assertEqual(broken.read_text(), 'invalid = [')
        self.assertEqual(len(data['sync']['errors']), 1)
        self.assertEqual(data['sync']['applied'], 2)

    def test_toml_multiline_fake_headers_and_other_skill_settings_preserved(self):
        text = 'model="x"\nextra="""\n[[skills.config]]\nhello\n"""\n[skills.bundled]\nenabled=false\n[[skills.config]]\npath="/repo/SKILL.md"\nenabled=false\n[windows]\nsandbox="elevated"\n'
        rules = [dict(path='/personal/SKILL.md', enabled=False)]
        updated = tomllib.loads(replace_rules(text, rules))
        expected = tomllib.loads(text); expected['skills']['config'] = rules
        self.assertEqual(updated, expected)
        self.assertEqual(replace_rules(replace_rules(text, rules), rules), replace_rules(text, rules))


if __name__ == '__main__': unittest.main()
