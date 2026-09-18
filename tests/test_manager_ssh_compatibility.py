import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.ssh_compatibility import fingerprint
from test_manager_desktop_bundle import archive


class NativeSshCompatibilityTests(unittest.TestCase):
    def test_archive_changes_invalidate_cached_implementation(self):
        with tempfile.TemporaryDirectory() as folder:
            home=Path(folder);(home/'resources').mkdir()
            path=home/'resources/app.asar'
            body=archive(path,b'CODEX_REMOTE_PAYLOAD; fixture')
            first=hashlib.sha256(body).hexdigest()
            self.assertEqual(fingerprint(home/'ChatGPT.exe'),first)
            body=archive(path,b'CODEX_REMOTE_PAYLOAD; changed implementation')
            self.assertEqual(fingerprint(home/'ChatGPT.exe'),hashlib.sha256(body).hexdigest())
            path.write_bytes(b'unsupported archive')
            self.assertEqual(fingerprint(home/'ChatGPT.exe'),'unavailable')


if __name__=='__main__': unittest.main()
