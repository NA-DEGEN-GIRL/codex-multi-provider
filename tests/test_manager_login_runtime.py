"""Official login CLI staging must never launch the protected MSIX path."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from manager_core.login_probe import verification_runtime


class LoginRuntimeTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.source=self.root/'WindowsApps/app/resources/codex.exe'
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(b'official executable fixture')
        self.app={'InstallLocation':str(self.root/'WindowsApps'),'Version':'fixture'}

    def test_copy_is_verified_and_reused_without_replacing_running_executable(self):
        staged=verification_runtime(self.root,self.app)
        self.assertFalse(staged.is_relative_to(self.source.parents[2]))
        self.assertEqual(staged.read_bytes(),self.source.read_bytes())
        with patch('manager_core.login_probe.shutil.copyfile',side_effect=AssertionError('unexpected rewrite')):
            self.assertEqual(verification_runtime(self.root,self.app),staged)

    def test_corrupted_cached_copy_is_rejected(self):
        staged=verification_runtime(self.root,self.app)
        staged.write_bytes(b'corrupted fixture')
        with self.assertRaisesRegex(RuntimeError,'무결성'):
            verification_runtime(self.root,self.app)
        self.assertEqual(self.source.read_bytes(),b'official executable fixture')

    def test_bad_copy_is_not_published(self):
        def broken(source,target):
            Path(target).write_bytes(b'incomplete')
        with patch('manager_core.login_probe.shutil.copyfile',side_effect=broken):
            with self.assertRaisesRegex(RuntimeError,'무결성'):
                verification_runtime(self.root,self.app)
        self.assertFalse(list((self.root/'artifacts/login-runtime').rglob('*.exe')))
        self.assertFalse(list((self.root/'artifacts/login-runtime').rglob('*.tmp')))


if __name__=='__main__':unittest.main()
