import sys
import hashlib
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from remote_helpers import launch


class StaleInstanceTests(unittest.TestCase):
    runtime = "/home/dev/.local/share/codex-control-center/runtime/new-bundle"
    current = runtime + "/codex"
    old = "/home/dev/.local/share/codex-control-center/runtime/old-bundle/codex"

    def test_current_bundle_is_never_stale(self):
        self.assertEqual(launch.stale_instances([(11, self.current)], self.runtime), [])

    def test_other_bundle_and_unknown_executables_are_stale(self):
        instances = [(11, self.old), (12, ""), (13, self.current)]
        self.assertEqual(launch.stale_instances(instances, self.runtime),
                         [(11, self.old), (12, "")])

    def test_runtime_prefix_does_not_match_sibling_bundle(self):
        sibling = self.runtime + "-copy/codex"
        self.assertEqual(launch.stale_instances([(14, sibling)], self.runtime), [(14, sibling)])

    def test_managed_instance_scan_ignores_unrelated_processes(self):
        # The scan runs on Linux; on this platform it must degrade to no matches.
        if sys.platform.startswith("linux"):
            self.assertIsInstance(launch.managed_instances("0000000000000000"), list)
        else:
            self.assertEqual(launch.managed_instances("0000000000000000"), [])


class ObsoleteRoleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        (self.home / 'agents').mkdir()
        self.role = self.home / 'agents/cc_gpt_astra.toml'
        self.content = b'developer_instructions = "managed role"\n'
        self.role.write_bytes(self.content)
        self.previous = {'agents/cc_gpt_astra.toml': hashlib.sha256(self.content).hexdigest()}

    def test_unchanged_obsolete_role_moves_outside_native_discovery(self):
        plan = launch.obsolete_role_plan(self.home, self.previous, {})
        self.assertEqual(len(plan), 1)
        source, archive = plan[0]
        self.assertEqual(source, self.role)
        self.assertEqual(archive.parent, self.home / 'manager-retired-agents')
        self.assertTrue(self.role.exists())  # Validation precedes config writes.
        archive.parent.mkdir()
        source.replace(archive)
        self.assertEqual(archive.read_bytes(), self.content)
        self.assertFalse(self.role.exists())

    def test_selected_and_unowned_roles_are_preserved(self):
        self.assertEqual(launch.obsolete_role_plan(self.home, self.previous, self.previous), [])
        custom = self.home / 'agents/custom.toml'
        custom.write_bytes(self.content)
        previous = {'agents/custom.toml': hashlib.sha256(self.content).hexdigest()}
        self.assertEqual(launch.obsolete_role_plan(self.home, previous, {}), [])
        self.assertTrue(custom.exists())

    def test_edited_obsolete_role_blocks_before_any_mutation(self):
        self.role.write_bytes(self.content + b'# personal edit\n')
        with self.assertRaisesRegex(ValueError, 'was edited'):
            launch.obsolete_role_plan(self.home, self.previous, {})
        self.assertTrue(self.role.exists())
        self.assertFalse((self.home / 'manager-retired-agents').exists())

    def historical_definition(self, profile, content, *, revision='a' * 64, profile_id=None):
        definition = profile / 'definitions' / revision
        (definition / 'agents').mkdir(parents=True)
        (definition / 'agents/cc_gpt_astra.toml').write_bytes(content)
        descriptor = dict(profile_id=profile_id or profile.name, revision=revision, definition=str(definition))
        (definition.parent / (revision + '.json')).write_text(json.dumps(descriptor), encoding='utf-8')
        return definition

    def test_historical_manager_bytes_recover_lost_ownership_but_personal_edits_do_not(self):
        profile = self.home / 'profile'
        self.historical_definition(profile, self.content)
        self.assertEqual(launch.recover_role_ownership(profile, self.home, {}), self.previous)
        self.role.write_bytes(self.content + b'# personal edit\n')
        self.assertEqual(launch.recover_role_ownership(profile, self.home, {}), {})

    def test_foreign_descriptor_cannot_claim_historical_role_ownership(self):
        profile = self.home / 'profile'
        self.historical_definition(profile, self.content, profile_id='someone-else')
        self.assertEqual(launch.recover_role_ownership(profile, self.home, {}), {})

    def test_launch_repairs_orphaned_selected_role_and_keeps_exact_backup(self):
        profile = self.home / 'profiles' / 'fixture-profile'
        home = profile / 'codex'
        old = b'model = "gpt-fixture"\n'
        new = b'name = "fixture"\ndescription = "Fixture role"\nmodel = "gpt-fixture"\ndeveloper_instructions = "Fixture"\n'
        self.historical_definition(profile, old)
        revision = 'b' * 64
        definition = self.historical_definition(profile, new, revision=revision)
        runtime = self.home / 'runtime' / 'fixture'
        runtime.mkdir(parents=True)
        descriptor_path = profile / 'definitions' / (revision + '.json')
        descriptor = json.loads(descriptor_path.read_text())
        descriptor['runtime'] = str(runtime)
        descriptor_path.write_text(json.dumps(descriptor))
        (home / 'agents').mkdir(parents=True)
        target = home / 'agents/cc_gpt_astra.toml'
        target.write_bytes(old)
        (profile / 'generated-files.json').write_text('{}')
        (profile / 'credentials').mkdir()
        (profile / 'credentials' / (revision + '.json')).write_text('{}')
        common = types.SimpleNamespace(reconcile_skills=MagicMock())
        fcntl = types.SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=MagicMock())
        with patch.dict(sys.modules, {'common': common, 'fcntl': fcntl}), \
             patch.object(launch.os, 'execve', side_effect=RuntimeError('fixture runtime reached')):
            with self.assertRaisesRegex(RuntimeError, 'fixture runtime reached'):
                launch.run(profile, revision, ['--version'])
        self.assertEqual(target.read_bytes(), new)
        backups = list((home / 'manager-retired-agents').glob('*.toml'))
        self.assertEqual([path.read_bytes() for path in backups], [old])
        self.assertEqual(json.loads((profile / 'generated-files.json').read_text()),
                         {'agents/cc_gpt_astra.toml': hashlib.sha256(new).hexdigest()})


if __name__ == '__main__':
    unittest.main()
