import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from uuid import uuid4
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from manager_core.record_signals import RecordSignals


class RecordSignalTests(unittest.TestCase):
    def test_archive_and_restore_survive_coalescing_with_later_notifications(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, thread = str(uuid4()), str(uuid4())
            signals = RecordSignals(directory, profile)
            try:
                for method, kind in [('thread/archived', 'archived'), ('thread/unarchived', 'unarchived'), ('thread/deleted', 'deleted')]:
                    signals.observe(dict(method=method, params=dict(threadId=thread)))
                    signals.observe(dict(method='thread/name/updated', params=dict(threadId=thread, name='PRIVATE')))
                    signals._flush()
                    saved = json.loads(signals.path.read_text())
                    self.assertEqual(saved['changes'][0][3], kind)
                    self.assertNotIn('PRIVATE', json.dumps(saved))
            finally:
                signals.close()

    def test_project_move_and_projectless_move_publish_content_free_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, thread = str(uuid4()), str(uuid4())
            signals = RecordSignals(directory, profile)
            try:
                for project in ('audio', None):
                    signals.observe({'method': 'thread/project/updated',
                        'params': {'threadId': thread, 'projectId': project}})
                signals._flush()
                data=json.loads((Path(directory)/(profile+'.json')).read_text())
                self.assertEqual(data['changes'], [[thread,2,'local','changed']])
                self.assertNotIn('projectId', json.dumps(data))
            finally:
                signals.close()

    def test_proxy_publishes_runtime_change_without_altering_protocol(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile, thread = str(uuid4()), str(uuid4())
            runtime = root / 'fixture.py'
            runtime.write_text('import json\nprint(json.dumps(' + repr({
                'method': 'item/agentMessage/delta', 'params': {'threadId': thread, 'delta': 'PRIVATE TEXT'}
            }) + '),flush=True)\n')
            bootstrap = root / 'bootstrap.py'
            scripts = Path(__file__).resolve().parents[1] / 'scripts'
            bootstrap.write_text('import sys\nfrom pathlib import Path\nsys.path.insert(0,' + repr(str(scripts)) + ')\n'
                'from manager_core.runtime_proxy import proxy\n'
                'raise SystemExit(proxy(Path(sys.executable),[sys.argv[1]],Path(sys.argv[2]),sys.argv[3],'
                '{"CODEX_MANAGER_REAL_RUNTIME":sys.executable,"CODEX_MANAGER_RECORD_SIGNALS":sys.argv[4]}))\n')
            env = {k:v for k,v in os.environ.items() if not k.upper().startswith('CODEX_')}
            result = subprocess.run([sys.executable, str(bootstrap), str(runtime), str(root/'status.json'),
                profile, str(root/'signals')], env=env, input='', capture_output=True, text=True, timeout=15,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['params']['delta'], 'PRIVATE TEXT')
            saved = (root/'signals'/(profile+'.json')).read_text()
            self.assertNotIn('PRIVATE TEXT', saved)
            self.assertEqual(json.loads(saved)['changes'], [[thread,1,'local','changed']])

    def test_publishes_only_bounded_ids_not_payloads_or_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, thread = str(uuid4()), str(uuid4())
            signals = RecordSignals(directory,profile)
            signals.observe(dict(method='account/updated',params=dict(token='SECRET')))
            signals.observe(dict(id=4,method='turn/started',params=dict(threadId=thread)))
            signals.observe(dict(method='item/agentMessage/delta',params=dict(threadId=thread,delta='PRIVATE PROMPT')))
            signals.close()
            text=(Path(directory)/(profile+'.json')).read_text()
            self.assertNotIn('SECRET',text);self.assertNotIn('PRIVATE PROMPT',text)
            self.assertEqual(json.loads(text)['changes'],[[thread,1,'local','changed']])

    def test_coalesces_and_limits_pending_history(self):
        with tempfile.TemporaryDirectory() as directory:
            profile=str(uuid4());signals=RecordSignals(directory,profile)
            for _ in range(400):
                thread=str(uuid4())
                for __ in range(4): signals.observe(dict(method='thread/started',params=dict(thread=dict(id=thread))))
            signals.close()
            value=json.loads((Path(directory)/(profile+'.json')).read_text())
            self.assertEqual(len(value['changes']),256)
            self.assertEqual(value['changes'][-1],[thread,1600,'local','changed'])


if __name__=='__main__':unittest.main()
