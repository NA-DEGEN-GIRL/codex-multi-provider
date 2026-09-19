import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core import original_sync_bundle as bundle
import start_synced_original as launcher
from test_manager_desktop_bundle import archive


class OriginalSyncBundleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'installed'
        (self.source / 'resources').mkdir(parents=True)
        (self.source / 'ChatGPT.exe').write_bytes(b'unchanged-executable')
        (self.source / 'chrome.dll').write_bytes(b'dL7pKGdnNz796PbbjQWNKmHXBZaB9tsX\x01\x09101100011')
        self.archive = self.source / 'resources/app.asar'
        self.body = archive(self.archive, b';'.join(bundle.PATCHES))
        self.app = dict(executable=str(self.source / 'ChatGPT.exe'), Version='fixture')

    def test_copy_preserves_installed_assets_and_archive_offsets(self):
        original = self.archive.read_bytes()
        executable = bundle.prepare(self.root, self.app)
        self.assertNotEqual(executable, self.source / 'ChatGPT.exe')
        self.assertEqual(self.archive.read_bytes(), original)
        self.assertEqual(executable.read_bytes(), b'unchanged-executable')
        with (executable.parent / 'resources/app.asar').open('rb') as stream:
            header, base = bundle.read_header(stream)
            entries = dict(bundle._entries(header))
            contents = {}
            for name, entry in entries.items():
                stream.seek(base + int(entry['offset']))
                contents[name] = stream.read(entry['size'])
        self.assertEqual(contents['.vite/build/before.bin'], b'FIRST')
        self.assertEqual(contents['.vite/build/after.bin'], b'AFTER')
        main = contents['.vite/build/main.js']
        self.assertLess(main.index(b'globalThis.__codexSignalFiles={create'),
                        main.index(b'const files=globalThis.__codexSignalFiles.create'))
        for replacement in bundle.PATCHES.values():
            self.assertIn(replacement, main)
        self.assertEqual(entries['.vite/build/main.js']['integrity']['hash'], hashlib.sha256(main).hexdigest())
        with patch.object(bundle.shutil, 'copytree', side_effect=AssertionError('cached copy expected')):
            self.assertEqual(bundle.prepare(self.root, self.app), executable)

    def test_unknown_version_fails_without_modifying_source(self):
        archive(self.archive, b'unknown implementation;' + b';'.join(bundle.PATCHES))
        self.archive.write_bytes(self.archive.read_bytes().replace(b'if(this.assertActive(),', b'if(this.newImplementation(),'))
        before = self.archive.read_bytes()
        with self.assertRaises(ValueError):
            bundle.prepare(self.root, self.app)
        self.assertEqual(self.archive.read_bytes(), before)
        self.assertFalse(list((self.root / 'artifacts/original-sync-desktop').glob('*/original-sync.json')))

    def test_tampered_companion_is_not_reused(self):
        executable = bundle.prepare(self.root, self.app)
        (executable.parent / 'resources/app.asar').write_bytes(b'changed')
        self.assertEqual(bundle.prepare(self.root, self.app), executable)
        self.assertNotEqual((executable.parent / 'resources/app.asar').read_bytes(), b'changed')


class OriginalSyncLauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / '.codex').mkdir()
        (self.root / '.codex/config.toml').write_text('# original settings')
        (self.root / 'roaming/Codex').mkdir(parents=True)
        self.app = {'executable': str(self.root / 'installed/ChatGPT.exe'), 'Version': 'fixture'}
        self.copy = self.root / 'artifacts/original-sync-desktop/fixture/ChatGPT.exe'
        for patcher in (patch.object(launcher, 'find_app', return_value=self.app),
                        patch.object(launcher.Path, 'home', return_value=self.root),
                        patch.dict(os.environ, {'APPDATA': str(self.root / 'roaming')}),
                        patch.object(launcher, 'prepare', return_value=self.copy),
                        patch.object(launcher, 'resolve', return_value={'runtime':'validated-runtime',
                            'capabilities':{'shared_append_envelopes':True,'shared_history_refresh':True}})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_running_original_is_never_restarted_or_forwarded_to(self):
        with patch.object(launcher, 'original_processes', return_value=[{'ExecutablePath': self.app['executable']}]), \
                patch.object(launcher.subprocess, 'Popen') as spawn:
            self.assertTrue(launcher.launch(self.root, check=True)['original_running'])
            with self.assertRaises(ValueError):
                launcher.launch(self.root)
            spawn.assert_not_called()

    def test_original_identity_and_runtime_are_retained(self):
        environment = dict(os.environ, CODEX_HOME='wrong-managed-home', CODEX_CLI_PATH='managed-runtime',
                           CODEX_MANAGER_DESKTOP_PIPE='managed-pipe', ELECTRON_RUN_AS_NODE='1')
        with patch.object(launcher, 'original_processes', return_value=[]), patch.dict(os.environ, environment), \
                patch.object(launcher.subprocess, 'Popen', return_value=Mock(pid=123)) as spawn:
            result = launcher.launch(self.root)
        env = spawn.call_args.kwargs['env']
        self.assertEqual(env['CODEX_HOME'], str(self.root / '.codex'))
        self.assertEqual(env['CODEX_ELECTRON_USER_DATA_PATH'], str(self.root / 'roaming/Codex'))
        self.assertEqual(env['CODEX_RECORD_SIGNALS'], str(self.root / 'work/control-center/record-signals'))
        self.assertEqual(env['CODEX_CLI_PATH'], 'validated-runtime')
        self.assertEqual(env['CODEX_RECORD_SHARED_APPEND'], '1')
        self.assertNotIn('CODEX_MANAGER_DESKTOP_PIPE', env)
        self.assertNotIn('ELECTRON_RUN_AS_NODE', env)
        self.assertEqual(result['process_id'], 123)

    def test_restored_private_binary_without_profile_arguments_blocks_forwarding(self):
        process = {'ExecutablePath':str(self.root/'artifacts/managed-desktop/old/ChatGPT.exe'),
                   'CommandLine':'ChatGPT.exe'}
        with patch.object(launcher, 'original_processes', return_value=[process]), \
                patch.object(launcher.subprocess, 'Popen') as spawn:
            self.assertTrue(launcher.launch(self.root, check=True)['requires_normal_close'])
            with self.assertRaises(ValueError): launcher.launch(self.root)
            spawn.assert_not_called()
        process['CommandLine'] = 'ChatGPT.exe --user-data-dir="'+str(self.root/'profiles/04/ui')+'"'
        with patch.object(launcher, 'original_processes', return_value=[process]):
            self.assertFalse(launcher.launch(self.root, check=True)['original_running'])

    def test_previous_installed_version_and_quoted_ui_argument(self):
        ui=self.root/'roaming/Codex'
        old=Path(os.environ.get('ProgramFiles', r'C:\Program Files'))/'WindowsApps/OpenAI.Codex_26.908.4834.0_x64__2p2nqsd0c76g0/app/ChatGPT.exe'
        row={'ExecutablePath':str(old),'CommandLine':'ChatGPT.exe "--user-data-dir='+str(ui)+'"'}
        self.assertTrue(launcher.uses_original_ui(row,self.root,Path(self.app['executable']),ui))
        row['CommandLine']='ChatGPT.exe "--user-data-dir='+str(self.root/'different profile/ui')+'"'
        self.assertFalse(launcher.uses_original_ui(row,self.root,Path(self.app['executable']),ui))


if __name__ == '__main__':
    unittest.main()
