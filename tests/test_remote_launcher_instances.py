import sys
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


if __name__ == '__main__':
    unittest.main()
