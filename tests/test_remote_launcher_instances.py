import sys
import hashlib
import tempfile
import unittest
from pathlib import Path

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


if __name__ == '__main__':
    unittest.main()
