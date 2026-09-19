from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.instances import Instances


class PackageDiscoveryCacheTests(unittest.TestCase):
    def test_short_lived_cache_returns_copies_and_refreshes_after_expiry_or_removal(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            executable = root / 'app.exe'
            executable.write_bytes(b'fixture')
            instances = Instances(root, Mock(directory=root / 'state'), Mock())
            app = dict(executable=str(executable), Version='fixture')
            with patch('desktop_launch.find_app', return_value=app) as discover, \
                 patch('manager_core.instances.time.monotonic', return_value=1) as clock:
                first = instances.installed_app()
                first['Version'] = 'caller change'
                self.assertEqual(instances.installed_app(), app)
                self.assertEqual(discover.call_count, 1)
                clock.return_value = 6
                instances.installed_app()
                self.assertEqual(discover.call_count, 2)
                executable.unlink()
                discover.side_effect = RuntimeError('package removed')
                with self.assertRaisesRegex(RuntimeError, 'removed'):
                    instances.installed_app()
                self.assertEqual(discover.call_count, 3)


if __name__ == '__main__':
    unittest.main()
