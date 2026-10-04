"""Offline packaging contracts; native build and tools use synthetic ELF files."""
from contextlib import ExitStack
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('package_runtime_test', ROOT / 'scripts/remote_helpers/package_runtime.py')
PACKAGE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(PACKAGE)


class PackageRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / 'isolated-project'
        (self.root / 'runtime/codex-rs').mkdir(parents=True)
        (self.root / 'patches').mkdir()
        (self.root / 'patches/runtime-source.json').write_text(json.dumps({'patch_sha256': 'a' * 64}))
        self.calls = []
        self.pinned = []

    @staticmethod
    def elf(body):
        header = bytearray(64)
        header[:6] = b'\x7fELF\x02\x01'
        header[18:20] = struct.pack('<H', 62)
        return bytes(header) + body

    def tool(self, command, **kwargs):
        self.calls.append((command, kwargs))
        executable = Path(command[0]).name
        if executable == 'cargo':
            release = Path(command[command.index('--target-dir') + 1]) / 'release'
            release.mkdir(parents=True, exist_ok=True)
            if 'codex-bwrap' in command:
                (release / 'bwrap').write_bytes(self.elf(b'bwrap DEBUG'))
            else:
                pinned = kwargs['env']['CODEX_BWRAP_SHA256']
                self.assertEqual(pinned, PACKAGE.digest(release / 'bwrap'))
                self.pinned.append(pinned)
                markers = [b'external_agents', b'CODEX_MANAGER_MANAGED_SOURCES',
                           b'CODEX_MANAGER_SHARED_CATALOG', b'invalid managed source catalog',
                           b'invalid mixed source catalog legacy identity', *PACKAGE.REMOTE_CAPABILITY_MARKERS.values()]
                (release / 'codex').write_bytes(self.elf(b'\0'.join(markers) + b' DEBUG'))
                (release / 'codex-code-mode-host').write_bytes(self.elf(b'host DEBUG'))
        elif executable == 'strip':
            path = Path(command[-1])
            path.write_bytes(path.read_bytes().removesuffix(b' DEBUG'))
        elif executable == 'objcopy':
            Path(command[-1]).write_bytes(b'DEBUG SYMBOLS')
        elif executable == 'bwrap':
            return subprocess.CompletedProcess(command, 0, 'bubblewrap 0.11.0\n', '')
        elif executable == 'codex':
            return subprocess.CompletedProcess(command, 0, 'codex-cli 0.153.4\n', '')
        else:
            self.fail('Unexpected tool invocation: ' + repr(command))
        return subprocess.CompletedProcess(command, 0, '', '')

    def run_build(self, cache=None):
        with ExitStack() as stack:
            stack.enter_context(patch.object(PACKAGE.platform, 'system', return_value='Linux'))
            stack.enter_context(patch.object(PACKAGE.platform, 'machine', return_value='x86_64'))
            stack.enter_context(patch.object(PACKAGE.shutil, 'which', return_value='cargo'))
            v8 = stack.enter_context(patch.object(PACKAGE, 'prepare_v8', return_value={'RUSTY_V8_ARCHIVE': 'fixture-v8'}))
            stack.enter_context(patch.object(PACKAGE.subprocess, 'run', side_effect=self.tool))
            result = PACKAGE.build(self.root, build_cache=cache)
        expected_cache = cache.resolve() if cache is not None else self.root / 'work/remote-build'
        v8.assert_called_once_with(self.root.resolve(), 'x86_64', build_cache=expected_cache)
        manifest = json.loads((Path(result['bundle_directory']) / 'manifest.json').read_text())
        self.assertEqual(manifest['bwrap_sha256'], self.pinned[-1])
        self.assertEqual(manifest['build_source_sha256'], 'a' * 64)
        for entry in manifest['files']:
            self.assertEqual(entry['sha256'], PACKAGE.digest(Path(result['bundle_directory']) / entry['path']))
        for flag in ('external_bridge_present', 'managed_sources_present', 'source_catalog_present',
                     'mixed_source_catalog_present', *PACKAGE.REMOTE_CAPABILITY_MARKERS):
            self.assertIs(manifest[flag], True)
        self.assertIs(manifest['native_gui_ssh_verified'], False)
        return result

    def test_explicit_cache_reuses_target_and_preserves_prior_package_and_symbols(self):
        cache = self.base / 'shared-cache'
        prior = cache / 'linux-x86_64/package'
        prior.mkdir(parents=True)
        sentinel = prior / 'codex.debug'
        sentinel.write_bytes(b'KEEP PREVIOUS SYMBOLS')
        first = self.run_build(cache)
        second = self.run_build(cache)
        self.assertEqual(sentinel.read_bytes(), b'KEEP PREVIOUS SYMBOLS')
        self.assertNotEqual(first['staging_directory'], second['staging_directory'])
        for result in (first, second):
            staged = Path(result['staging_directory'])
            self.assertTrue(staged.is_relative_to(self.root / 'work/remote-build'))
            self.assertEqual((staged / 'codex.debug').read_bytes(), b'DEBUG SYMBOLS')
            self.assertEqual(Path(result['bundle_directory']), self.root / 'artifacts/remote/linux-x86_64')
        for command, _ in self.calls:
            if command[0] == 'cargo':
                self.assertEqual(Path(command[command.index('--target-dir') + 1]), cache / 'linux-x86_64')
        self.assertTrue((cache / 'linux-x86_64/release/codex').read_bytes().endswith(b' DEBUG'))

    def test_default_build_retains_existing_target_and_package_locations(self):
        result = self.run_build()
        self.assertEqual(Path(result['staging_directory']), self.root / 'work/remote-build/linux-x86_64/package')

    def test_explicit_v8_cache_verifies_and_reuses_both_pinned_files(self):
        (self.root / 'runtime/codex-rs/Cargo.lock').write_text('[[package]]\nname="v8"\nversion="150.4.0"\n')
        cache = self.base / 'shared-cache'
        directory = cache / 'v8/x86_64-unknown-linux-gnu/150.4.0'
        directory.mkdir(parents=True)
        names = ('librusty_v8_ptrcomp_sandbox_release_x86_64-unknown-linux-gnu.a.gz',
                 'src_binding_ptrcomp_sandbox_release_x86_64-unknown-linux-gnu.rs')
        lines = []
        for name in names:
            data = ('pinned fixture: ' + name).encode()
            (directory / name).write_bytes(data)
            lines.append(hashlib.sha256(data).hexdigest() + '  ' + name)
        with patch.object(PACKAGE.urllib.request, 'urlopen', return_value=io.BytesIO('\n'.join(lines).encode())) as request:
            result = PACKAGE.prepare_v8(self.root, 'x86_64', build_cache=cache)
        request.assert_called_once()
        self.assertTrue(request.call_args.args[0].startswith('https://github.com/openai/codex/releases/download/rusty-v8-v150.4.0/'))
        self.assertEqual(result, {'RUSTY_V8_ARCHIVE': str(directory / names[0]),
                                  'RUSTY_V8_SRC_BINDING_PATH': str(directory / names[1])})
        self.assertFalse((self.root / 'work').exists())


if __name__ == '__main__':
    unittest.main()
