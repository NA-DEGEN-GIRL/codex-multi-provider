import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from remote_helpers.workspace_dependencies import dependencies, reply


class WorkspaceDependenciesTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        (self.root/'runtime.json').write_text(json.dumps({'targetPlatform':'linux','bundleVersion':'fixture'}))
        for name in ('dependencies/node/node_modules','dependencies/bin/override',
                     'dependencies/bin/fallback','dependencies/python/lib/python3.12/site-packages'):
            (self.root/name).mkdir(parents=True)
        for name in ('dependencies/node/bin/node','dependencies/python/bin/python3'):
            path=self.root/name;path.parent.mkdir(parents=True);path.write_bytes(b'fixture');path.chmod(0o700)

    def test_read_only_discovery_uses_installed_root(self):
        before=sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*'))
        value=dependencies(self.root)
        self.assertEqual(value['bundleVersion'],'fixture')
        self.assertTrue(Path(value['pythonLibrariesPath']).is_relative_to(self.root))
        self.assertEqual(before,sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*')))

    def test_windows_bundle_is_not_native_linux(self):
        (self.root/'runtime.json').write_text('{"targetPlatform":"win32"}')
        with self.assertRaises(ValueError):dependencies(self.root)

    def test_missing_runtime_returns_sanitized_tool_error(self):
        (self.root/'runtime.json').write_text('invalid private fixture text')
        result=reply({'id':1,'method':'tools/call','params':{'name':'load_workspace_dependencies'}},self.root)
        self.assertTrue(result['result']['isError'])
        self.assertNotIn('private fixture',json.dumps(result))

    def test_wrong_metadata_type_preserves_request_id(self):
        for value in ('[]', 'null'):
            (self.root/'runtime.json').write_text(value)
            result=reply({'id':42,'method':'tools/call','params':{'name':'load_workspace_dependencies'}},self.root)
            self.assertEqual(result['id'],42)
            self.assertTrue(result['result']['isError'])

    def test_executable_cannot_be_a_directory(self):
        path=self.root/'dependencies/node/bin/node';path.unlink();path.mkdir()
        with self.assertRaises(ValueError):dependencies(self.root)

    @unittest.skipIf(os.name=='nt','POSIX execute permission')
    def test_executable_requires_execute_permission(self):
        (self.root/'dependencies/node/bin/node').chmod(0o600)
        with self.assertRaises(ValueError):dependencies(self.root)

    def test_stdio_protocol_discovery_and_unknown_method(self):
        result=reply({'id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26'}})
        self.assertEqual(result['result']['protocolVersion'],'2025-03-26')
        tool=reply({'id':2,'method':'tools/list'})['result']['tools'][0]
        self.assertTrue(tool['annotations']['readOnlyHint'])
        self.assertIsNone(reply({'method':'notifications/initialized'}))
        self.assertEqual(reply({'id':3,'method':'execute'})['error']['code'],-32601)


if __name__=='__main__':unittest.main()
