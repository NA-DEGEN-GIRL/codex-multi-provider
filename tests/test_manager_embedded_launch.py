"""Launch visibility policy only; never starts a process or touches a real window."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from control_center import ControlCenter
from manager_core.instances import embedded_startup


class EmbeddedLaunchTests(unittest.TestCase):
    def test_login_helper_without_desktop_supervisor_remains_visible(self):
        with tempfile.TemporaryDirectory() as root:
            center = ControlCenter(root)
            self.assertFalse(center.instances.embed_windows)
            self.assertEqual(embedded_startup(center.instances.embed_windows), {})

    @unittest.skipUnless(os.name == 'nt', 'Windows startup flags')
    def test_desktop_supervisor_starts_hidden_for_internal_attachment(self):
        with tempfile.TemporaryDirectory() as root:
            center = ControlCenter(root, supervisor_protocol=21)
            self.assertTrue(center.instances.embed_windows)
            startup = embedded_startup(center.instances.embed_windows)['startupinfo']
            self.assertTrue(startup.dwFlags & subprocess.STARTF_USESHOWWINDOW)
            self.assertEqual(startup.wShowWindow, subprocess.SW_HIDE)


if __name__ == '__main__':
    unittest.main()
