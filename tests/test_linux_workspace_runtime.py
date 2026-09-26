"""Pinned runtime installation against temporary synthetic archives; no network."""
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import install_linux_workspace_runtime as runtime


class LinuxWorkspaceRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='codex-runtime-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.target = self.root / runtime.ROOT_NAME

    def metadata(self):
        return dict(bundleVersion=runtime.BUNDLE_VERSION, targetPlatform='linux', targetArch='x64')

    def archive(self, extra=()):
        archive = self.root / 'fixture.tar'
        with tarfile.open(archive, 'w') as output:
            files = {'runtime.json': json.dumps(self.metadata()).encode(),
                     'dependencies/node/bin/node': b'#!/bin/sh\nexit 0\n',
                     'dependencies/python/bin/python3': b'#!/bin/sh\nexit 0\n'}
            for name, data in files.items():
                member = tarfile.TarInfo(runtime.ROOT_NAME + '/' + name)
                member.size = len(data)
                member.mode = 0o755 if '/bin/' in name else 0o644
                output.addfile(member, io.BytesIO(data))
            for name in ('dependencies/node/node_modules', 'dependencies/python/lib',
                         'dependencies/bin/override', 'dependencies/bin/fallback', 'plugins/openai-primary-runtime'):
                member = tarfile.TarInfo(runtime.ROOT_NAME + '/' + name)
                member.type, member.mode = tarfile.DIRTYPE, 0o755
                output.addfile(member)
            for member in extra:
                output.addfile(member)
        return archive

    def fixture_pins(self, archive):
        return mock.patch.multiple(runtime, ARCHIVE_BYTES=archive.stat().st_size,
                                   ARCHIVE_SHA256=hashlib.sha256(archive.read_bytes()).hexdigest())

    def test_rejects_members_outside_runtime_root(self):
        for name in ('/etc/passwd', '../outside', runtime.ROOT_NAME + '/../outside',
                     'unrelated/file', runtime.ROOT_NAME + '/C:\\outside'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                runtime.safe_member(tarfile.TarInfo(name), self.root)

    def test_rejects_special_files_and_escaping_links(self):
        member = tarfile.TarInfo(runtime.ROOT_NAME + '/bad')
        member.type = tarfile.FIFOTYPE
        with self.assertRaises(tarfile.FilterError):
            runtime.safe_member(member, self.root)
        for target in ('../../outside', '/tmp/unrecognized', '../outside'):
            member.type, member.linkname = tarfile.SYMTYPE, target
            with self.subTest(target=target), self.assertRaises((ValueError, tarfile.FilterError)):
                runtime.safe_member(member, self.root)

    def test_repairs_only_known_python_build_links(self):
        member = tarfile.TarInfo(runtime.ROOT_NAME + '/dependencies/python/bin/python')
        member.type = tarfile.SYMTYPE
        member.linkname = '/tmp/codex-primary-runtime-Ab12/python-download/python/bin/python3'
        fixed = runtime.safe_member(member, self.root)
        self.assertEqual(fixed.linkname, 'python3')
        self.assertNotEqual(member.linkname, fixed.linkname)
        member.type = tarfile.LNKTYPE
        self.assertEqual(runtime.safe_member(member, self.root).linkname.replace('\\', '/'),
                         runtime.ROOT_NAME + '/dependencies/python/bin/python3')
        member.linkname = '/tmp/codex-primary-runtime-Ab12/python-download/python/../../outside'
        with self.assertRaises(ValueError):
            runtime.safe_member(member, self.root)

    def test_hash_checked_before_any_extraction(self):
        archive = self.archive()
        stage = self.root / 'stage'
        stage.mkdir()
        with mock.patch.object(tarfile, 'open') as opened, self.assertRaises(ValueError):
            runtime.extract_verified(archive, stage)
        opened.assert_not_called()
        self.assertEqual(list(stage.iterdir()), [])

    @unittest.skipUnless(sys.platform == 'linux', 'POSIX symbolic links')
    def test_link_chain_cannot_escape_runtime_into_staging(self):
        self.target.mkdir()
        (self.target / 'alias').symlink_to('.', target_is_directory=True)
        member = tarfile.TarInfo(runtime.ROOT_NAME + '/alias/link')
        member.type, member.linkname = tarfile.SYMTYPE, '../outside'
        with self.assertRaises(ValueError):
            runtime.safe_member(member, self.root)

    def test_preserves_unknown_existing_target_without_download(self):
        self.target.mkdir()
        sentinel = self.target / 'keep.txt'
        sentinel.write_text('user data')
        with mock.patch.object(runtime, 'require_platform'), mock.patch.object(runtime, 'download') as download:
            with self.assertRaises(OSError):
                runtime.install(self.target)
        download.assert_not_called()
        self.assertEqual(sentinel.read_text(), 'user data')

    @unittest.skipUnless(sys.platform == 'linux', 'Native Linux atomic publication and modes')
    def test_offline_install_preserves_modes_and_ready_target(self):
        archive = self.archive()
        with self.fixture_pins(archive), mock.patch.object(runtime, 'download') as download:
            result = runtime.install(self.target, archive)
            self.assertEqual(result['status'], 'installed')
            self.assertTrue(os.access(self.target / 'dependencies/node/bin/node', os.X_OK))
            self.assertEqual((self.target / 'dependencies/node/bin/node').stat().st_mode & 0o777, 0o755)
            stamps = {path: path.stat().st_mtime_ns for path in self.target.rglob('*')}
            self.assertEqual(runtime.install(self.target)['status'], 'unchanged')
            self.assertEqual(stamps, {path: path.stat().st_mtime_ns for path in self.target.rglob('*')})
        download.assert_not_called()
        self.assertTrue(archive.is_file())
        self.assertFalse(list(self.root.glob('.workspace-runtime-*')))

    @unittest.skipUnless(sys.platform == 'linux', 'Native Linux no-replace publication')
    def test_atomic_publication_does_not_replace_an_empty_directory(self):
        source = self.root / 'source'
        source.mkdir()
        (source / 'sentinel').write_text('staged')
        self.target.mkdir()
        with self.assertRaises(FileExistsError):
            runtime.publish_noreplace(source, self.target)
        self.assertTrue((source / 'sentinel').is_file())
        self.assertEqual(list(self.target.iterdir()), [])

    def test_bad_archive_cleans_staging_without_touching_offline_archive(self):
        archive = self.archive()
        before = archive.read_bytes()
        with mock.patch.object(runtime, 'require_platform'), self.assertRaises(ValueError):
            runtime.install(self.target, archive)
        self.assertFalse(self.target.exists())
        self.assertFalse(list(self.root.glob('.workspace-runtime-*')))
        self.assertEqual(archive.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
