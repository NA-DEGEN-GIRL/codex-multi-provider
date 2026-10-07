"""Staging contracts for local and Linux cross-built runtime packages; synthetic .exe files only."""
from contextlib import closing, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from activate_manager_runtime import release_problem, require_activatable
from manager_core import runtime_build
from manager_core.runtime_migrations import source_migrations
import stage_manager_runtime as STAGE


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


MISSING = object()  # cross_package(field=MISSING) omits the field from windows-build.json
THREADS_CRLF = b'CREATE TABLE threads (\r\n    id TEXT PRIMARY KEY\r\n);\r\n'
SETUP = 'codex-windows-sandbox-setup'


def local_pe_check(name):
    # What the fake verify_pe reports for a staged copy (the real one needs PE32+ files).
    return dict(stack_reserve=8 << 20, imports=['kernel32.dll'], manifest=name == SETUP)


def make_store(path, rows):
    """A store as sqlx leaves it: _sqlx_migrations rows of (version, description, checksum bytes)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute('CREATE TABLE _sqlx_migrations (version INTEGER PRIMARY KEY, description TEXT NOT NULL, '
                           'installed_on TEXT NOT NULL, success BOOLEAN NOT NULL, checksum BLOB NOT NULL, '
                           'execution_time INTEGER NOT NULL)')
        connection.executemany("INSERT INTO _sqlx_migrations VALUES (?, ?, '2026-10-01 00:00:00', 1, ?, 1000)", rows)
        connection.commit()
    return path


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
        self.pe_checked = []
        self.pe_failures = {}
        # What the package says its codex.exe embeds (a CRLF checkout's bytes).
        self.migrations = [dict(directory='migrations', version=1, description='threads',
                                sha384=hashlib.sha384(THREADS_CRLF).hexdigest())]

    def live_store(self, checksum, *, profile=None):
        """A record-home (or profile-home) store the live manager uses, migrated with `checksum`."""
        if profile is None:
            home = self.base / 'record-home'
            canonical = self.root / 'work/control-center/canonical-storage.json'
            canonical.parent.mkdir(parents=True, exist_ok=True)
            canonical.write_text(json.dumps(dict(version=1, home=str(home))), encoding='utf-8')
        else:
            home = self.root / 'work/control-center/profiles' / profile / 'codex'
        return make_store(home / 'state_5.sqlite', [(1, 'threads', checksum)])

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
                     xwin=dict(splat='xwin-fixture', libraries_sha256='e' * 64), files=files,
                     line_endings='crlf', migrations=self.migrations,
                     # The server's claims; staging repeats the checks on its own copies.
                     pe_checks={name: dict(stack_reserve=8 << 20, imports=['server-claim.dll'],
                                           manifest=name == SETUP) for name in STAGE.BINARIES})
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

    def verify_pe(self, path, name):
        self.pe_checked.append((Path(path), name))
        if name in self.pe_failures:
            raise RuntimeError(name + '.exe: ' + self.pe_failures[name])
        return local_pe_check(name)

    def release_directories(self):
        return sorted(self.releases.iterdir()) if self.releases.is_dir() else []

    def stage(self, **arguments):
        with patch.object(STAGE.subprocess, 'check_output', side_effect=self.tool), \
                patch.object(STAGE, 'verify_pe', side_effect=self.verify_pe), \
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
        # Activation re-checks the live stores against these.
        self.assertEqual(manifest['migrations'], self.migrations)
        # Only the staged copy is executed; the base commit comes from the package, not git.
        self.assertEqual(self.calls, [[str(release / 'codex.exe'), '--version']])
        loaded = runtime_build.load_release(self.root, candidate)
        self.assertEqual(loaded['runtime'], str((release / 'codex.exe').resolve()))
        self.assertEqual(loaded['files'], expected)
        self.assertEqual(loaded['migrations'], self.migrations)

    def test_staged_cross_build_records_pe_checks_and_passes_the_activation_gate(self):
        candidate = self.stage(source=self.cross_package())
        release = candidate.parent
        # Verified on the staged copies, before the staged codex.exe runs.
        self.assertEqual(self.pe_checked, [(release / (name + '.exe'), name) for name in STAGE.BINARIES])
        manifest = json.loads(candidate.read_text(encoding='utf-8'))
        self.assertEqual(manifest['pe_checks'], {name: local_pe_check(name) for name in STAGE.BINARIES})
        loaded = runtime_build.load_release(self.root, candidate)
        self.assertIsNone(release_problem(loaded))
        require_activatable(self.root, loaded)
        # The gate this feeds: without the record, activation refuses the build.
        self.assertIn('PE checks', release_problem({key: value for key, value in loaded.items()
                                                    if key != 'pe_checks'}))

    def test_failed_pe_check_of_a_staged_copy_removes_the_partial_release(self):
        self.pe_failures['codex-command-runner'] = 'stack reserve 1048576'
        self.assertRejected(RuntimeError, '^codex-command-runner.exe: stack reserve', source=self.cross_package())
        self.assertEqual(self.release_directories(), [])
        self.assertEqual(self.pe_checked[-1][1], 'codex-command-runner')

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
                             ('files', MISSING), ('files', []),
                             # A package whose server-side PE checks were not recorded.
                             ('pe_checks', MISSING), ('pe_checks', []),
                             ('pe_checks', {'codex': local_pe_check('codex')})):
            with self.subTest(field=field, value=value):
                self.assertRejected(RuntimeError, '^Unsupported cross-build package',
                                    source=self.cross_package(**{field: value}))

    def test_package_without_crlf_migration_digests_is_rejected(self):
        # Packages from before the CRLF checkout were built from LF sources and cannot be checked.
        for field, value in (('migrations', MISSING), ('migrations', None), ('migrations', {}),
                             ('line_endings', MISSING), ('line_endings', 'lf'), ('line_endings', 'CRLF')):
            with self.subTest(field=field, value=value):
                self.assertRejected(RuntimeError, '^Unsupported cross-build package',
                                    source=self.cross_package(**{field: value}))

    def test_package_whose_migrations_differ_from_the_live_stores_is_not_staged(self):
        lf_checksum = hashlib.sha384(THREADS_CRLF.replace(b'\r\n', b'\n')).digest()
        for profile in (None, 'alpha'):
            with self.subTest(home='profile' if profile else 'record home'):
                store = self.live_store(lf_checksum, profile=profile)
                before = store.read_bytes()
                # Refused before a release directory exists or the staged codex.exe runs.
                self.assertRejected(RuntimeError, '^This runtime would refuse existing records.*'
                                    + re.escape('%s: 1 threads' % store), source=self.cross_package())
                self.assertEqual(self.release_directories(), [])
                self.assertEqual(store.read_bytes(), before)
                store.unlink()

    def test_package_matching_the_live_stores_is_staged(self):
        store = self.live_store(bytes.fromhex(self.migrations[0]['sha384']))
        # A migration the package does not embed (a newer runtime applied it) is not a conflict.
        with closing(sqlite3.connect(store)) as connection:
            connection.execute("INSERT INTO _sqlx_migrations VALUES (2, 'newer', 'now', 1, ?, 1)", (b'\x02' * 48,))
            connection.commit()
        candidate = self.stage(source=self.cross_package())
        self.assertEqual(json.loads(candidate.read_text(encoding='utf-8'))['migrations'], self.migrations)

    def test_local_build_records_the_runtime_source_migrations(self):
        self.git_head = 'f' * 40
        self.binaries(self.root / 'work/target-runtime/release', b'local')
        codex_rs = self.root / 'runtime/codex-rs'
        (codex_rs / 'state/migrations').mkdir(parents=True)
        (codex_rs / 'state/migrations/0001_threads.sql').write_bytes(THREADS_CRLF)
        (codex_rs / 'state/logs_migrations').mkdir(parents=True)
        (codex_rs / 'state/logs_migrations/0001_logs.sql').write_bytes(b'CREATE TABLE logs (id INTEGER);\r\n')
        expected = [dict(directory='migrations', version=1, description='threads',
                         sha384=hashlib.sha384(THREADS_CRLF).hexdigest()),
                    dict(directory='logs_migrations', version=1, description='logs',
                         sha384=hashlib.sha384(b'CREATE TABLE logs (id INTEGER);\r\n').hexdigest())]
        self.assertEqual(source_migrations(codex_rs), expected)
        candidate = self.stage()
        self.assertEqual(json.loads(candidate.read_text(encoding='utf-8'))['migrations'], expected)
        # The same local sources against a store an LF build migrated: refused before anything is copied.
        store = self.live_store(hashlib.sha384(THREADS_CRLF.replace(b'\r\n', b'\n')).digest(), profile='alpha')
        releases = self.release_directories()
        self.calls.clear()
        with self.assertRaisesRegex(RuntimeError, re.escape('%s: 1 threads' % store)):
            self.stage()
        self.assertEqual(self.release_directories(), releases)
        self.assertEqual(self.calls, [])

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
                for key in ('build_origin', 'source_tree', 'build_source_sha256', 'pe_checks'):
                    self.assertNotIn(key, manifest)
                self.assertEqual(self.calls, [[str(candidate.parent / 'codex.exe'), '--version'],
                                              ['git', '-C', str(self.root / 'runtime'), 'rev-parse', 'HEAD']])
                runtime_build.load_release(self.root, candidate)
        # A local MSVC build is not PE-checked; only cross builds are.
        self.assertEqual(self.pe_checked, [])


if __name__ == '__main__':
    unittest.main()
