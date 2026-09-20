import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from stage_manager_bundle import stage
from manager_core.release_code import runtime_revision


class ReleaseBundleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name in ('control_center.py', 'manager_core/runtime_proxy.py', 'manager_core/ssh_shim.py'):
            path = self.root / 'scripts' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('original code\n')

    def release(self, name):
        release = self.root / 'artifacts/manager/releases' / name
        (release / 'ssh').mkdir(parents=True)
        (release / 'Codex.ControlCenter.RuntimeProxy.dll').write_bytes(b'bridge')
        (release / 'ssh/ssh.dll').write_bytes(b'ssh bridge')
        (release / 'codex-workspace-service.exe').write_bytes(b'rust service')
        (release / 'shell-compatibility.json').write_text(json.dumps(
            dict(version=1, revision=74, service_protocol=27)), encoding='utf-8')
        return release

    def test_ui_only_release_and_different_directory_do_not_change_runtime_revision(self):
        first, second = self.release('one'), self.release('two')
        (first / 'Codex.ControlCenter.dll').write_bytes(b'old UI')
        (second / 'Codex.ControlCenter.dll').write_bytes(b'new UI')
        (second / 'shell-compatibility.json').write_text(json.dumps(
            dict(version=1, revision=75, service_protocol=27)), encoding='utf-8')
        one, two = stage(self.root, first), stage(self.root, second)
        self.assertEqual(one['runtime_revision'], two['runtime_revision'])
        self.assertEqual(one['service_revision'], two['service_revision'])
        self.assertEqual(two['shell_compatibility']['revision'], 75)
        self.assertEqual(runtime_revision(second / 'Codex.ControlCenter.RuntimeProxy.exe'), two['runtime_revision'])

    def test_existing_runtime_cannot_load_later_workspace_edits(self):
        first = self.release('one')
        before = stage(self.root, first)
        source = self.root / 'scripts/manager_core/runtime_proxy.py'
        source.write_text('new runtime code\n')
        after = stage(self.root, self.release('two'))
        self.assertNotEqual(before['runtime_revision'], after['runtime_revision'])
        self.assertEqual((first / 'scripts/manager_core/runtime_proxy.py').read_text(), 'original code\n')
        with self.assertRaises(RuntimeError): stage(self.root, first)

    def test_service_update_is_independent_from_inner_codex_runtime(self):
        first, second = self.release('one'), self.release('two')
        (first / 'codex-workspace-service.exe').write_bytes(b'old service')
        (second / 'codex-workspace-service.exe').write_bytes(b'new service')
        one, two = stage(self.root, first), stage(self.root, second)
        self.assertEqual(one['runtime_revision'], two['runtime_revision'])
        self.assertNotEqual(one['service_revision'], two['service_revision'])

    def test_missing_bridge_does_not_publish_manifest(self):
        release = self.release('one')
        (release / 'ssh/ssh.dll').unlink()
        with self.assertRaises(RuntimeError): stage(self.root, release)
        self.assertFalse((release / 'runtime-manifest.json').exists())

    def test_missing_or_invalid_compatibility_cannot_publish_a_release(self):
        for index, info in enumerate((None, {}, [], dict(version=1, revision=True, service_protocol=27),
                                      dict(version=2, revision=74, service_protocol=27),
                                      dict(version=1, revision=74, service_protocol='27'))):
            with self.subTest(info=info):
                release = self.release(str(index))
                metadata = release / 'shell-compatibility.json'
                if info is None:
                    metadata.unlink()
                else:
                    metadata.write_text(json.dumps(info), encoding='utf-8')
                with self.assertRaises(RuntimeError): stage(self.root, release)
                self.assertFalse((release / 'runtime-manifest.json').exists())

    def test_private_window_adapter_is_frozen_and_part_of_revision(self):
        source = self.root / 'scripts/manager_core/desktop_window_host.cjs'
        source.write_text('original adapter')
        first = self.release('one')
        before = stage(self.root, first)
        source.write_text('updated adapter')
        after = stage(self.root, self.release('two'))
        self.assertEqual((first / 'scripts/manager_core/desktop_window_host.cjs').read_text(), 'original adapter')
        self.assertNotEqual(before['runtime_revision'], after['runtime_revision'])


if __name__ == '__main__': unittest.main()
