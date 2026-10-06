"""Offline Windows cross-build contracts; PE checks and the build use synthetic PE32+ files."""
from contextlib import ExitStack
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import call, patch


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WINDOWS = load('package_windows_runtime_test', 'scripts/remote_helpers/package_windows_runtime.py')
STAGE = load('stage_manager_runtime_cross_test', 'scripts/stage_manager_runtime.py')

SETUP = 'codex-windows-sandbox-setup'
# The manifest embedded in the baseline (local MSVC build) sandbox setup exe.
MANIFEST = (b'<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">'
            b'<trustInfo xmlns="urn:schemas-microsoft-com:asm.v2"><security><requestedPrivileges>'
            b'<requestedExecutionLevel level="asInvoker" uiAccess="false"></requestedExecutionLevel>'
            b'</requestedPrivileges></security></trustInfo></assembly>')
# lld-link's default UAC manifest merged with the setup's asm.v2 trustInfo:
# Windows refuses to start the exe (error 14001) over the prefixed attributes.
LLD_MERGED_MANIFEST = b'''<?xml version="1.0" encoding="UTF-8"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <ms_asmv1:trustInfo xmlns="urn:schemas-microsoft-com:asm.v2" xmlns:ms_asmv1="urn:schemas-microsoft-com:asm.v1">
    <ms_asmv1:security>
      <ms_asmv1:requestedPrivileges>
        <ms_asmv1:requestedExecutionLevel ms_asmv1:level="asInvoker" ms_asmv1:uiAccess="false"/>
      </ms_asmv1:requestedPrivileges>
    </ms_asmv1:security>
  </ms_asmv1:trustInfo>
</assembly>
'''
# The same merge with /MANIFESTUAC:NO, which Windows accepts.
MANIFESTUAC_NO_MANIFEST = b'''<?xml version="1.0" encoding="UTF-8"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
<trustInfo xmlns="urn:schemas-microsoft-com:asm.v2"><security><requestedPrivileges><requestedExecutionLevel level="asInvoker" uiAccess="false"/></requestedPrivileges></security></trustInfo></assembly>
'''
BASELINE_IMPORTS = ('KERNEL32.dll', 'api-ms-win-core-synch-l1-2-0.dll')
RT_VERSION, RT_MANIFEST = 16, 24
VERSION_ONLY = {RT_VERSION: {1: {0x409: b'VS_VERSION_INFO fixture'}}}
SETUP_RESOURCES = {**VERSION_ONLY, RT_MANIFEST: {1: {0x409: MANIFEST}}}
BASE_COMMIT, PATCH_SHA256, RESULT_TREE = 'b' * 40, 'c' * 64, 'd' * 40


def import_section(rva, dlls):
    """Import descriptors (null-terminated) whose Name RVAs point at the DLL strings."""
    thunks = 20 * (len(dlls) + 1)
    data = bytearray(thunks + 8)  # descriptors, then one empty lookup table
    for index, dll in enumerate(dlls):
        struct.pack_into('<IIIII', data, 20 * index, rva + thunks, 0, 0, rva + len(data), rva + thunks)
        data += dll.encode('ascii') + b'\0'
    return bytes(data)


def resource_section(rva, tree):
    """Type -> name -> language directories; bytes leaves become data entries."""
    data = bytearray()

    def place(node):
        offset = len(data)
        if isinstance(node, bytes):
            data.extend(struct.pack('<IIII', rva + offset + 16, len(node), 0, 0) + node)
            data.extend(bytes(-len(data) % 4))
            return offset
        items = sorted(node.items())
        data.extend(struct.pack('<IIHHHH', 0, 0, 0, 0, 0, len(items)) + bytes(8 * len(items)))
        for index, (ident, child) in enumerate(items):
            flag = 0 if isinstance(child, bytes) else 0x80000000
            struct.pack_into('<II', data, offset + 16 + 8 * index, ident, place(child) | flag)
        return offset

    place(tree)
    return bytes(data)


def pe_image(*, stack_reserve=8 << 20, dll_characteristics=0x8160, imports=BASELINE_IMPORTS, resources=None,
             overlay=b''):
    """A minimal PE32+ console executable shaped like the release build's output."""
    bodies = [(b'.text', b'\xcc' * 16, 0x60000020), (b'.rdata', import_section(0x2000, imports), 0x40000040)]
    if resources is not None:
        bodies.append((b'.rsrc', resource_section(0x3000, resources), 0x40000040))
    pe, headers = 0x80, 0x400
    optional = bytearray(112 + 8 * 16)
    struct.pack_into('<H', optional, 0, 0x20B)
    struct.pack_into('<IIQII', optional, 16, 0x1000, 0x1000, 0x140000000, 0x1000, 0x200)
    struct.pack_into('<HHHHHH', optional, 40, 6, 0, 0, 0, 6, 0)
    struct.pack_into('<II', optional, 56, 0x1000 * (len(bodies) + 1), headers)
    struct.pack_into('<HHQQQQ', optional, 68, 3, dll_characteristics, stack_reserve, 0x1000, 0x100000, 0x1000)
    struct.pack_into('<I', optional, 108, 16)
    image = bytearray(headers)
    image[:2] = b'MZ'
    struct.pack_into('<I', image, 0x3C, pe)
    image[pe:pe + 4] = b'PE\0\0'
    struct.pack_into('<HHIIIHH', image, pe + 4, 0x8664, len(bodies), 0, 0, 0, len(optional), 0x22)
    table = pe + 24 + len(optional)
    for index, (name, body, characteristics) in enumerate(bodies):
        rva = 0x1000 * (index + 1)
        assert len(body) < 0x1000
        if name in (b'.rdata', b'.rsrc'):
            struct.pack_into('<II', optional, 112 + 8 * (1 if name == b'.rdata' else 2), rva, len(body))
        raw_size = -(-len(body) // 0x200) * 0x200
        struct.pack_into('<8sIIIIIIHHI', image, table + 40 * index, name, len(body), rva, raw_size,
                         len(image), 0, 0, 0, 0, characteristics)
        image += body + bytes(raw_size - len(body))
    image[pe + 24:table] = optional
    return bytes(image + overlay)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


class PeChecksTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def verify(self, name, **options):
        path = self.base / (name + '.exe')
        path.write_bytes(pe_image(**options))
        return WINDOWS.verify_pe(path, name)

    def test_baseline_release_properties_pass(self):
        self.assertEqual(self.verify('codex'), {
            'stack_reserve': 8 << 20, 'imports': ['kernel32.dll', 'api-ms-win-core-synch-l1-2-0.dll'],
            'manifest': False})
        # Only RT_MANIFEST counts: the version resource beside it is not a manifest.
        self.assertIs(self.verify('codex-app-server', resources=VERSION_ONLY)['manifest'], False)
        self.assertIs(self.verify(SETUP, resources=SETUP_RESOURCES)['manifest'], True)
        # (resource ID, body) pairs: Windows applies only ID 1 to an exe.
        self.assertEqual(WINDOWS.inspect_pe(self.base / (SETUP + '.exe'))['manifests'], [(1, MANIFEST)])

    def test_lost_stack_static_crt_or_aslr_is_rejected(self):
        cases = {
            'stack reserve 1048576': dict(stack_reserve=1 << 20),
            'dynamic CRT imports vcruntime140.dll': dict(imports=BASELINE_IMPORTS + ('VCRUNTIME140.dll',)),
            'dynamic CRT imports api-ms-win-crt-runtime-l1-1-0.dll': dict(
                imports=BASELINE_IMPORTS + ('api-ms-win-crt-runtime-l1-1-0.dll',)),
            'dynamic CRT imports msvcrt.dll': dict(imports=BASELINE_IMPORTS + ('msvcrt.dll',)),
            'missing ASLR/NX characteristics 0x0$': dict(dll_characteristics=0),
            # High-entropy VA and NX without DYNAMIC_BASE.
            'missing ASLR/NX characteristics 0x8120$': dict(dll_characteristics=0x8120),
        }
        for message, options in cases.items():
            with self.subTest(message), self.assertRaisesRegex(RuntimeError, '^codex.exe: ' + message):
                self.verify('codex', **options)

    def test_manifest_must_be_exactly_the_baseline_setup_manifest(self):
        second_trust = MANIFEST.replace(b'</assembly>', MANIFEST[MANIFEST.index(b'<trustInfo'):
                                                                 MANIFEST.index(b'</assembly>')] + b'</assembly>')
        self.assertEqual(second_trust.count(b'<trustInfo'), 2)
        cases = [
            (SETUP, None, 'expected one embedded manifest with ID 1, found 0'),
            (SETUP, VERSION_ONLY, 'expected one embedded manifest with ID 1, found 0'),
            (SETUP, {RT_MANIFEST: {1: {0x409: MANIFEST}, 2: {0x409: MANIFEST}}},
             'expected one embedded manifest with ID 1, found 2'),
            # Windows ignores a manifest under any other ID, so the exe would run without one.
            (SETUP, {RT_MANIFEST: {2: {0x409: MANIFEST}}}, 'expected one embedded manifest with ID 1, found 1'),
            (SETUP, {RT_MANIFEST: {1: {0x409: second_trust}}}, 'unexpected embedded manifest'),
            (SETUP, {RT_MANIFEST: {1: {0x409: MANIFEST.replace(b'asInvoker', b'requireAdministrator')}}},
             'unexpected embedded manifest'),
            (SETUP, {RT_MANIFEST: {1: {0x409: MANIFEST[:-len(b'</assembly>')]}}}, 'unexpected embedded manifest'),
            ('codex-command-runner', SETUP_RESOURCES, 'unexpected embedded manifest'),
        ]
        for name, resources, message in cases:
            with self.subTest(name=name, message=message), \
                    self.assertRaisesRegex(RuntimeError, '^' + name + '.exe: ' + message + '$'):
                self.verify(name, resources=resources)

    def test_named_manifest_is_reported_without_an_id_and_rejected(self):
        # The high bit marks a named resource ID (the name string itself is not read).
        resources = {RT_MANIFEST: {0x80000000: {0x409: MANIFEST}}}
        with self.assertRaisesRegex(RuntimeError, '^' + SETUP + '.exe: expected one embedded manifest with ID 1, '
                                                               'found 1$'):
            self.verify(SETUP, resources=resources)
        self.assertEqual(WINDOWS.inspect_pe(self.base / (SETUP + '.exe'))['manifests'], [(None, MANIFEST)])

    def test_lld_merged_manifest_is_rejected_and_the_manifestuac_no_merge_passes(self):
        self.assertFalse(WINDOWS.manifest_is_as_invoker(LLD_MERGED_MANIFEST))
        with self.assertRaisesRegex(RuntimeError, '^' + SETUP + '.exe: unexpected embedded manifest$'):
            self.verify(SETUP, resources={**VERSION_ONLY, RT_MANIFEST: {1: {0x409: LLD_MERGED_MANIFEST}}})
        self.assertTrue(WINDOWS.manifest_is_as_invoker(MANIFESTUAC_NO_MANIFEST))
        checks = self.verify(SETUP, resources={**VERSION_ONLY, RT_MANIFEST: {1: {0x409: MANIFESTUAC_NO_MANIFEST}}})
        self.assertIs(checks['manifest'], True)


class CrossEnvironmentTests(unittest.TestCase):
    XWIN = Path('/srv/work/xwin/splat')
    V8 = {'RUSTY_V8_ARCHIVE': '/srv/cache/v8/archive.lib.gz', 'RUSTY_V8_SRC_BINDING_PATH': '/srv/cache/v8/binding.rs'}

    def test_target_toolchain_replaces_host_overrides(self):
        incoming = {'RUSTFLAGS': '-C target-cpu=native', 'CARGO_ENCODED_RUSTFLAGS': '-Copt-level=0',
                    'CC': 'gcc', 'CFLAGS': '-O3', 'UNRELATED_SETTING': 'kept'}
        with patch.dict(WINDOWS.os.environ, incoming):
            environment = WINDOWS.cross_environment(self.XWIN, self.V8)
            self.assertEqual(WINDOWS.os.environ['RUSTFLAGS'], incoming['RUSTFLAGS'])
        for name in ('RUSTFLAGS', 'CARGO_ENCODED_RUSTFLAGS', 'CC', 'CFLAGS'):
            self.assertNotIn(name, environment)
        self.assertEqual(environment['UNRELATED_SETTING'], 'kept')
        compiler = environment['CC_x86_64_pc_windows_msvc']
        self.assertEqual(compiler, str(WINDOWS.LLVM / 'clang-cl'))
        self.assertNotEqual(compiler, 'cl')
        self.assertEqual(environment['CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_LINKER'], str(WINDOWS.LLVM / 'lld-link'))
        # lld's default UAC manifest would merge into a manifest Windows rejects (14001).
        self.assertEqual(environment['CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_RUSTFLAGS'], '-Clink-arg=/MANIFESTUAC:NO')
        self.assertEqual(environment['LIB'].split(';'), [
            str(self.XWIN / 'crt/lib/x86_64'), str(self.XWIN / 'sdk/lib/ucrt/x86_64'),
            str(self.XWIN / 'sdk/lib/um/x86_64')])
        self.assertIn('/imsvc' + str(self.XWIN / 'crt/include'), environment['CFLAGS_x86_64_pc_windows_msvc'])
        self.assertEqual({name: environment[name] for name in self.V8}, self.V8)

    def test_profile_and_target_specific_overrides_are_dropped_or_replaced(self):
        # Linux variable names are case-sensitive; a plain mapping keeps them as
        # the build server sees them (Windows would upper-case os.environ keys).
        incoming = {
            'PATH': '/usr/bin', 'CARGO_HOME': '/srv/cargo',
            'CARGO_PROFILE_RELEASE_LTO': 'off', 'CARGO_PROFILE_RELEASE_DEBUG': '0',
            'TARGET_CC': 'gcc', 'TARGET_CFLAGS': '-O0',
            'CC_x86_64-pc-windows-msvc': 'cl', 'CFLAGS_x86_64-pc-windows-msvc': '-O0',
            'BINDGEN_EXTRA_CLANG_ARGS_x86_64_pc_windows_msvc': '-I/srv/include',
            'CFLAGS_x86_64_pc_windows_msvc': '-O0',
            'CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_RUSTFLAGS': '-Ctarget-feature=-crt-static',
        }
        with patch.object(WINDOWS.os, 'environ', dict(incoming)):
            environment = WINDOWS.cross_environment(self.XWIN, self.V8)
            self.assertEqual(WINDOWS.os.environ, incoming)
        for name in ('CARGO_PROFILE_RELEASE_LTO', 'CARGO_PROFILE_RELEASE_DEBUG', 'TARGET_CC', 'TARGET_CFLAGS',
                     'CC_x86_64-pc-windows-msvc', 'CFLAGS_x86_64-pc-windows-msvc',
                     'BINDGEN_EXTRA_CLANG_ARGS_x86_64_pc_windows_msvc'):
            self.assertNotIn(name, environment)
        self.assertEqual(environment['CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_RUSTFLAGS'], '-Clink-arg=/MANIFESTUAC:NO')
        self.assertTrue(environment['CFLAGS_x86_64_pc_windows_msvc'].startswith('--target=' + WINDOWS.TARGET + ' '))
        self.assertEqual(environment['CARGO_HOME'], '/srv/cargo')
        self.assertEqual(environment['PATH'].split(WINDOWS.os.pathsep), [str(WINDOWS.LLVM), '/usr/bin'])

    def test_whitespace_in_any_path_is_rejected(self):
        cases = [(Path('/srv/work/x win'), self.V8),
                 (self.XWIN, dict(self.V8, RUSTY_V8_ARCHIVE='/srv/cache/v8 copy/archive.lib.gz'))]
        for xwin, v8 in cases:
            with self.subTest(xwin=str(xwin)), self.assertRaisesRegex(RuntimeError, 'whitespace'):
                WINDOWS.cross_environment(xwin, v8)


class CrossBuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        if any(character.isspace() for character in str(self.base)):
            self.skipTest('cross_environment rejects whitespace in the temporary path')
        self.root = self.base / 'isolated-project'
        (self.root / 'runtime/codex-rs').mkdir(parents=True)
        (self.root / 'patches').mkdir()
        (self.root / 'patches/runtime-source.json').write_text(json.dumps(
            {'base_commit': BASE_COMMIT, 'patch_sha256': PATCH_SHA256, 'result_tree': RESULT_TREE}))
        self.llvm = self.base / 'llvm/bin'
        self.llvm.mkdir(parents=True)
        for tool in ('clang-cl', 'lld-link', 'llvm-lib'):
            (self.llvm / tool).write_bytes(b'')
        self.xwin = self.base / 'xwin/fixture-splat'
        for part, names in WINDOWS.XWIN_LIBRARIES.items():
            (self.xwin / part).mkdir(parents=True)
            for name in names:
                (self.xwin / part / name).write_bytes(b'lib ' + name.encode())
        (self.base / 'v8').mkdir()
        self.v8_files = {'RUSTY_V8_ARCHIVE': str(self.base / 'v8/archive.lib.gz'),
                         'RUSTY_V8_SRC_BINDING_PATH': str(self.base / 'v8/binding.rs')}
        for name, path in self.v8_files.items():
            Path(path).write_bytes(name.encode())
        self.cache = self.base / 'cache'
        self.calls = []
        self.broken = {}

    def tool(self, command, **kwargs):
        self.calls.append((command, kwargs))
        executable = Path(command[0]).name
        if executable == 'cargo' and command[1] == 'build':
            release = Path(command[command.index('--target-dir') + 1]) / WINDOWS.TARGET / 'release'
            release.mkdir(parents=True, exist_ok=True)
            for name in (command[index + 1] for index, item in enumerate(command) if item == '--bin'):
                options = dict(resources=SETUP_RESOURCES if name == SETUP else None,
                               overlay=b'fixture ' + name.encode() + (b' external_agents' if name == 'codex' else b''))
                options.update(self.broken.get(name, {}))
                (release / (name + '.exe')).write_bytes(pe_image(**options))
            (release / 'codex.pdb').write_bytes(b'PDB fixture')
            return subprocess.CompletedProcess(command, 0)
        if command[1:] in (['-V'], ['--version']):
            return subprocess.CompletedProcess(command, 0, executable + ' fixture\nsecond line\n', '')
        self.fail('Unexpected tool invocation: ' + repr(command))

    def run_build(self, tree=RESULT_TREE, tree_after_build=None):
        trees = [tree, tree if tree_after_build is None else tree_after_build]
        with ExitStack() as stack:
            stack.enter_context(patch.object(WINDOWS.platform, 'system', return_value='Linux'))
            stack.enter_context(patch.object(WINDOWS.platform, 'machine', return_value='x86_64'))
            stack.enter_context(patch.object(WINDOWS.shutil, 'which', return_value='cargo'))
            stack.enter_context(patch.object(WINDOWS, 'LLVM', self.llvm))
            stack.enter_context(patch.object(WINDOWS.os, 'sched_getaffinity', create=True, return_value=set(range(64))))
            stack.enter_context(patch.dict(WINDOWS.os.environ, {'RUSTFLAGS': '-C link-arg=/STACK:1048576', 'CC': 'gcc'}))
            self.source_tree = stack.enter_context(patch.object(WINDOWS, 'source_tree', side_effect=trees))
            self.prepare_v8 = stack.enter_context(patch.object(WINDOWS, 'prepare_v8', return_value=self.v8_files))
            stack.enter_context(patch.object(WINDOWS.subprocess, 'run', side_effect=self.tool))
            return WINDOWS.build(self.root, build_cache=self.cache, xwin=self.xwin)

    def assertNoPackage(self):
        # The package/ parent may remain; no package directory inside it may.
        packages = self.cache / 'windows-x86_64/package'
        self.assertEqual(sorted(packages.iterdir()) if packages.exists() else [], [])

    def test_build_packages_verified_executables_for_the_recorded_patch(self):
        with patch.object(WINDOWS, 'verify_pe', wraps=WINDOWS.verify_pe) as verify:
            result = self.run_build()
        target_dir = self.cache / 'windows-x86_64'
        # Checked before cargo and again after it.
        self.assertEqual(self.source_tree.call_args_list, [call(self.root / 'runtime')] * 2)
        self.prepare_v8.assert_called_once_with(self.root, 'x86_64', build_cache=self.cache, target=WINDOWS.TARGET)
        builds = [call for call in self.calls if call[0][1] == 'build']
        self.assertEqual(len(builds), 1)
        command, options = builds[0]

        def value(flag):
            return command[command.index(flag) + 1]

        self.assertEqual(command[:4], ['cargo', 'build', '--locked', '--release'])
        self.assertEqual(value('--target'), 'x86_64-pc-windows-msvc')
        self.assertEqual(value('-j'), '64')
        self.assertEqual(Path(value('--target-dir')), target_dir)
        bins = [command[index + 1] for index, item in enumerate(command) if item == '--bin']
        self.assertEqual(len(bins), 5)
        self.assertEqual(sorted(bins), sorted(STAGE.BINARIES))
        self.assertEqual(options['cwd'], self.root / 'runtime/codex-rs')
        self.assertIs(options['check'], True)
        environment = options['env']
        self.assertNotIn('RUSTFLAGS', environment)
        self.assertNotIn('CC', environment)
        self.assertEqual(environment['CC_x86_64_pc_windows_msvc'], str(self.llvm / 'clang-cl'))
        self.assertEqual(environment['RUSTY_V8_ARCHIVE'], self.v8_files['RUSTY_V8_ARCHIVE'])
        self.assertEqual(environment['LIB'].split(';')[0], str(self.xwin / 'crt/lib/x86_64'))

        package = Path(result['package_directory'])
        self.assertEqual(package.parent, target_dir / 'package')
        self.assertIn(PATCH_SHA256[:16], package.name)
        # The copies that ship are the ones verified, not cargo's outputs.
        self.assertEqual([checked.args for checked in verify.call_args_list],
                         [(package / (name + '.exe'), name) for name in WINDOWS.BINARIES])
        self.assertEqual(list(package.glob('.manifest-*')), [])
        manifest = json.loads((package / 'windows-build.json').read_text(encoding='utf-8'))
        self.assertEqual(len(manifest['files']), 5)
        for name in STAGE.BINARIES:
            exe = name + '.exe'
            data = (target_dir / WINDOWS.TARGET / 'release' / exe).read_bytes()
            self.assertEqual((package / exe).read_bytes(), data)
            self.assertEqual(manifest['files'][exe], {'sha256': sha256(data), 'size': len(data)})
            self.assertIs(manifest['pe_checks'][name]['manifest'], name == SETUP)
        self.assertEqual(result['files'], manifest['files'])
        self.assertEqual(manifest['symbols'], {'codex.pdb': sha256(b'PDB fixture')})
        self.assertEqual((package / 'symbols/codex.pdb').read_bytes(), b'PDB fixture')
        self.assertEqual((manifest['schema'], manifest['target'], manifest['profile']),
                         (1, WINDOWS.TARGET, 'release'))
        self.assertEqual((manifest['base_commit'], manifest['result_tree'], manifest['build_source_sha256']),
                         (BASE_COMMIT, RESULT_TREE, PATCH_SHA256))
        self.assertEqual(manifest['v8'], {
            'archive_sha256': sha256(b'RUSTY_V8_ARCHIVE'), 'binding_sha256': sha256(b'RUSTY_V8_SRC_BINDING_PATH')})
        self.assertEqual(manifest['xwin'], {'splat': 'fixture-splat',
                                            'libraries_sha256': WINDOWS.splat_digest(self.xwin)})
        self.assertEqual(manifest['toolchain'], {'rustc': 'rustc fixture', 'cargo': 'cargo fixture',
                                                 'clang_cl': 'clang-cl fixture', 'lld_link': 'lld-link fixture'})
        self.assertEqual((manifest['environment']['jobs'], manifest['environment']['cpus']), (64, 64))
        # The Windows staging side accepts exactly this package.
        self.assertEqual(STAGE._load_cross_build(self.root, package), manifest)

    def test_tree_mismatch_stops_before_cargo(self):
        with self.assertRaisesRegex(RuntimeError, 'runtime/ is not the recorded patch tree \\(' + 'e' * 40):
            self.run_build(tree='e' * 40)
        self.assertEqual(self.calls, [])
        self.prepare_v8.assert_not_called()
        self.assertFalse((self.cache / 'windows-x86_64').exists())

    def test_runtime_changed_during_the_build_writes_no_package(self):
        with self.assertRaisesRegex(RuntimeError, '^runtime/ changed during the build'):
            self.run_build(tree_after_build='e' * 40)
        self.assertEqual(len([command for command, _ in self.calls if command[1] == 'build']), 1)
        self.assertEqual(self.source_tree.call_count, 2)
        self.assertTrue((self.cache / 'windows-x86_64' / WINDOWS.TARGET / 'release/codex.exe').is_file())
        self.assertFalse((self.cache / 'windows-x86_64/package').exists())

    def test_failed_pe_check_writes_no_package(self):
        self.broken['codex-command-runner'] = dict(stack_reserve=1 << 20)
        with self.assertRaisesRegex(RuntimeError, '^codex-command-runner.exe: stack reserve 1048576'):
            self.run_build()
        self.assertTrue((self.cache / 'windows-x86_64' / WINDOWS.TARGET / 'release/codex.exe').is_file())
        self.assertNoPackage()

    def test_any_failure_after_the_package_is_created_removes_it(self):
        copy = shutil.copyfile
        release = self.cache / 'windows-x86_64' / WINDOWS.TARGET / 'release'

        def damaged_in_transit(source, target, *args, **kwargs):
            copy(source, target, *args, **kwargs)
            if Path(target).name == 'codex-app-server.exe':
                Path(target).write_bytes(pe_image(stack_reserve=1 << 20))
            return target

        def disk_full(source, target, *args, **kwargs):
            if Path(target).name == 'codex.pdb':
                raise OSError('No space left on device')
            return copy(source, target, *args, **kwargs)

        cases = [
            # The build output is sound; only the copy that would ship is not.
            ('damaged copy', RuntimeError, '^codex-app-server.exe: stack reserve 1048576', damaged_in_transit, {}),
            ('missing marker', RuntimeError, 'external-agent bridge marker', copy,
             {'codex': dict(overlay=b'fixture codex')}),
            ('copy error', OSError, 'No space left', disk_full, {}),
        ]
        for label, error, message, copier, broken in cases:
            with self.subTest(label):
                self.calls.clear()
                self.broken = broken
                with patch.object(WINDOWS.shutil, 'copyfile', side_effect=copier), \
                        self.assertRaisesRegex(error, message):
                    self.run_build()
                self.assertNoPackage()
                if label == 'damaged copy':
                    WINDOWS.verify_pe(release / 'codex-app-server.exe', 'codex-app-server')

    def test_splat_digest_identifies_library_contents(self):
        original = WINDOWS.splat_digest(self.xwin)
        # The identity is the libraries' contents, not where the splat lives.
        moved = self.base / 'xwin/moved-splat'
        shutil.copytree(self.xwin, moved)
        self.assertEqual(WINDOWS.splat_digest(moved), original)
        library = moved / 'sdk/lib/um/x86_64/kernel32.lib'
        library.write_bytes(library.read_bytes().upper())  # same size, other bytes
        self.assertNotEqual(WINDOWS.splat_digest(moved), original)


if __name__ == '__main__':
    unittest.main()
