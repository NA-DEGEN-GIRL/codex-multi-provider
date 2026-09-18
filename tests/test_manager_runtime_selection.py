import json
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.runtime_selection import describe


class RuntimeSelectionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.profile = dict(id=str(uuid4()), generation=str(uuid4()), status='running', runtime_channel='managed')
        self.profile['runtime_state'] = dict(generation=self.profile['generation'], runtime_process_id=1234, runtime_observer_version=2)
        self.expected = self.root / 'artifacts/manager-runtime/releases/new/codex.exe'
        self.current = self.root / 'artifacts/manager-runtime/releases/old/codex.exe'
        self.descriptor = self.root / 'work/control-center/instances' / self.profile['id'] / 'runtime-admin' / (self.profile['generation'] + '.json')
        self.descriptor.parent.mkdir(parents=True)
        self.descriptor.write_text(json.dumps(dict(profile_id=self.profile['id'], generation=self.profile['generation'],
            runtime=dict(pid=1234, created=999), key_dpapi='must-not-escape')))

    def read(self, **changes):
        actual = dict(process_id=1234, process_created=999, executable_path=str(self.current))
        actual.update(changes)
        return describe(self.root, self.profile, identity=lambda _: actual, selected={'runtime': str(self.expected)})

    def test_actual_old_binary_is_not_reported_as_current_from_new_pointer(self):
        result = self.read()
        self.assertEqual(result['state'], 'outdated')
        self.assertEqual((result['current_release'], result['selected_release']), ('old', 'new'))
        self.assertTrue(result['restart_required'])
        self.assertNotIn('must-not-escape', json.dumps(result))

    def test_reused_pid_and_old_generation_cannot_certify_running_version(self):
        self.assertEqual(self.read(process_created=1000)['state'], 'unknown')
        self.profile['runtime_state']['generation'] = str(uuid4())
        self.assertEqual(self.read()['state'], 'unknown')

    def test_old_shared_proxy_is_reported_separately_from_rust_binary(self):
        self.profile['runtime_state']['shared_catalog'] = {'enabled': True}
        result = self.read(executable_path=str(self.expected))
        self.assertTrue(result['restart_required'])
        self.assertIn('작업 이름 변경', result['message'])
        self.profile['runtime_state']['record_edits_version'] = 1
        self.assertFalse(self.read(executable_path=str(self.expected))['restart_required'])
    def test_new_binary_and_stopped_process_are_distinguished(self):
        result = self.read(executable_path=str(self.expected))
        self.assertEqual(result['state'], 'current')
        self.assertFalse(result['restart_required'])
        self.profile['status'] = 'not_started'
        self.assertEqual(self.read()['state'], 'not_running')

    def test_old_observer_requires_one_new_launch_even_with_current_binary(self):
        self.profile['runtime_state'].pop('runtime_observer_version')
        result = self.read(executable_path=str(self.expected))
        self.assertTrue(result['restart_required'])
        self.assertIn('실행 상태 확인 수정 적용 대기', result['message'])


if __name__ == '__main__':
    unittest.main()
