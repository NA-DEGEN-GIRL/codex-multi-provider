"""Staging contracts for local and Linux cross-built runtime packages; synthetic .exe files only."""
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from manager_core import runtime_build
import stage_manager_runtime as STAGE


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


MISSING = object()  # cross_package(field=MISSING) omits the field from windows-build.json


class StageRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / 'isolated-project'
        (self.root / 'patches').mkdir(parents=True)
        self.recorded = dict(base_commit='b' * 40, patch_sha256='c' * 64, result_tree='d' * 40)
        (self.root / 'patches/runtime-source.json').write_text(json.dumps(self.recorded), encoding='utf-8')
        self.releases = self.root / 'artifacts/manager-runtime/releases'
        self.git_head = None
        self.version_error = None
        self.calls = []

    @staticmethod
    def binaries(directory, label):
        directory.mkdir(parents=True, exist_ok=True)
        for name in STAGE.BINARIES:
            body = b'MZ ' + label + b' ' + name.encode()
            if name == 'codex':
                body += b' CODEX_CLAUDE_CODE_AGENT_V1'
            (directory / (name + '.exe')).write_bytes(body)

    def cross_package(self, **changes):
        package = Path(tempfile.mkdtemp(prefix='server-package-', dir=self.base))
        self.binaries(package, b'cross')
        files = {path.name: dict(sha256=sha256(path), size=path.stat().st_size)
                 for path in sorted(package.glob('*.exe'))}
        build = dict(schema=1, platform='windows', target='x86_64-pc-windows-msvc',
                     build_kind='linux-cross-xwin', profile='release',
                     base_commit=self.recorded['base_commit'], result_tree=self.recorded['result_tree'],
                     build_source_sha256=self.recorded['patch_sha256'],
                     toolchain=dict(rustc='rustc 1.90.0', cargo='cargo 1.90.0'),
                     xwin=dict(splat='xwin-fixture', libraries_sha256='e' * 64), files=files)
        build.update(changes)
        build = {key: value for key, value in build.items() if value is not MISSING}
        (package / 'windows-build.json').write_text(json.dumps(build), encoding='utf-8')
        return package

    def tool(self, command, **kwargs):
        self.calls.append(list(command))
        if Path(command[0]).name == 'codex.exe' and list(command[1:]) == ['--version']:
            if self.version_error is not None:
                raise self.version_error
            return 'codex-cli 0.153.4\n'
        if command[0] == 'git' and self.git_head is not None:
            return self.git_head + '\n'
        self.fail('Unexpected tool invocation: ' + repr(command))

    def release_directories(self):
        return sorted(self.releases.iterdir()) if self.releases.is_dir() else []

    def stage(self, **arguments):
        with patch.object(STAGE.subprocess, 'check_output', side_effect=self.tool), \
                redirect_stdout(io.StringIO()) as output:
            candidate = STAGE.stage(self.root, **arguments)
        self.assertEqual(json.loads(output.getvalue()),
                         dict(candidate_manifest=str(candidate), active_runtime_changed=False))
        return candidate

    def assertRejected(self, error, message='', **arguments):
        before = self.release_directories()
        with self.assertRaisesRegex(error, message):
            self.stage(**arguments)
        self.assertEqual(self.release_directories(), before)
        self.assertEqual(self.calls, [])

    def test_cross_package_stages_a_verified_candidate_without_git(self):
        package = self.cross_package()
        candidate = self.stage(source=package)
        release = candidate.parent
        self.assertEqual(release.parent, self.releases)
        self.assertRegex(release.name, r'^\d{8}-\d{6}-[0-9a-f]{6}$')
        self.assertEqual(candidate.name, 'candidate.json')
        manifest = json.loads(candidate.read_text(encoding='utf-8'))
        expected = {name + '.exe': sha256(package / (name + '.exe')) for name in STAGE.BINARIES}
        self.assertEqual(manifest['files'], expected)
        self.assertEqual({name: sha256(release / name) for name in expected}, expected)
        self.assertEqual(manifest['runtime'], str(release / 'codex.exe'))
        self.assertEqual(manifest['sha256'], expected['codex.exe'])
        self.assertEqual(manifest['version'], 'codex-cli 0.153.4')
        self.assertEqual(manifest['build_profile'], 'release')
        self.assertEqual(manifest['validation'], 'candidate')
        self.assertEqual(manifest['build_origin'], 'linux-cross-xwin')
        self.assertEqual(manifest['source_tree'], self.recorded['result_tree'])
        self.assertEqual(manifest['build_source_sha256'], self.recorded['patch_sha256'])
        self.assertEqual(manifest['source_base'], self.recorded['base_commit'])
        self.assertEqual(manifest['build_toolchain'], dict(rustc='rustc 1.90.0', cargo='cargo 1.90.0'))
        self.assertEqual(manifest['build_xwin'], dict(splat='xwin-fixture', libraries_sha256='e' * 64))
        self.assertTrue(manifest['capabilities']['claude_code_agent'])
        self.assertFalse(manifest['capabilities']['managed_execution_presets'])
        # Only the staged copy is executed; the base commit comes from the package, not git.
        self.assertEqual(self.calls, [[str(release / 'codex.exe'), '--version']])
        loaded = runtime_build.load_release(self.root, candidate)
        self.assertEqual(loaded['runtime'], str((release / 'codex.exe').resolve()))
        self.assertEqual(loaded['files'], expected)

    def test_changed_or_missing_cross_companion_is_rejected(self):
        for change in ('tampered', 'missing'):
            with self.subTest(change=change):
                package = self.cross_package()
                path = package / 'codex-command-runner.exe'
                if change == 'tampered':
                    path.write_bytes(path.read_bytes() + b' patched after packaging')
                else:
                    path.unlink()
                self.assertRejected(RuntimeError, source=package)

    def test_companion_changed_after_verification_removes_the_partial_release(self):
        package = self.cross_package()
        copy = shutil.copyfile

        def replaced_before_copy(source, target, *args, **kwargs):
            if Path(source).name == 'codex-windows-sandbox-setup.exe':
                Path(source).write_bytes(b'MZ replaced after verification')
            return copy(source, target, *args, **kwargs)

        with patch.object(STAGE.shutil, 'copyfile', side_effect=replaced_before_copy):
            self.assertRejected(RuntimeError, source=package)

    def test_package_from_another_runtime_patch_is_rejected(self):
        for field, value in (('result_tree', '0' * 40), ('build_source_sha256', '0' * 64),
                             ('base_commit', '0' * 40), ('base_commit', MISSING)):
            with self.subTest(field=field, value=value):
                self.assertRejected(RuntimeError, 'built from another runtime patch',
                                    source=self.cross_package(**{field: value}))

    def test_unsupported_cross_package_kind_is_rejected(self):
        for field, value in (('target', 'aarch64-pc-windows-msvc'), ('target', None),
                             ('schema', 2), ('profile', 'debug'),
                             ('build_kind', MISSING), ('build_kind', 'windows-msvc'),
                             ('files', MISSING), ('files', [])):
            with self.subTest(field=field, value=value):
                self.assertRejected(RuntimeError, '^Unsupported cross-build package',
                                    source=self.cross_package(**{field: value}))

    def test_version_failure_removes_the_partial_release(self):
        for error in (subprocess.TimeoutExpired(['codex.exe', '--version'], 15),
                      subprocess.CalledProcessError(1, ['codex.exe', '--version'])):
            with self.subTest(error=type(error).__name__):
                package = self.cross_package()
                self.calls.clear()
                self.version_error = error
                with self.assertRaises(type(error)):
                    self.stage(source=package)
                # The staged copy ran inside a release directory that is gone again.
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(Path(self.calls[0][0]).parent.parent, self.releases)
                self.assertEqual(self.release_directories(), [])

    def test_cross_package_requires_the_release_profile(self):
        self.assertRejected(ValueError, profile='debug', source=self.cross_package())

    def test_default_source_reads_the_local_target_runtime_profile(self):
        self.git_head = 'f' * 40
        for profile in ('release', 'debug'):
            self.binaries(self.root / 'work/target-runtime' / profile, profile.encode())
        for profile in ('release', 'debug'):
            with self.subTest(profile=profile):
                self.calls.clear()
                candidate = self.stage(profile=profile)
                manifest = json.loads(candidate.read_text(encoding='utf-8'))
                source = self.root / 'work/target-runtime' / profile
                self.assertEqual(manifest['files'],
                                 {name + '.exe': sha256(source / (name + '.exe')) for name in STAGE.BINARIES})
                self.assertEqual(manifest['build_profile'], profile)
                self.assertEqual(manifest['source_base'], 'f' * 40)
                for key in ('build_origin', 'source_tree', 'build_source_sha256'):
                    self.assertNotIn(key, manifest)
                self.assertEqual(self.calls, [[str(candidate.parent / 'codex.exe'), '--version'],
                                              ['git', '-C', str(self.root / 'runtime'), 'rev-parse', 'HEAD']])
                runtime_build.load_release(self.root, candidate)


if __name__ == '__main__':
    unittest.main()
