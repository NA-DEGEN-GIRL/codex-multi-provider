import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from progress import Progress


def event(method, thread='parent', **params):
    return {'method': method, 'params': {'threadId': thread, **params}}


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 0.0
        self.output = Path(self.directory.name)
        self.stream = io.StringIO()
        self.progress = Progress(self.output, stream=self.stream, clock=lambda: self.now)
        self.addCleanup(self.progress.close)

    def test_live_output_and_report_exist_before_completion(self):
        self.progress.write_report({'status': 'RUNNING'})
        self.progress.register_thread('parent', 'gpt-6-astra', 'openai', 'parent')
        self.progress.consume(event('item/started', item={
            'type': 'commandExecution', 'id': 'tool', 'command': 'python test.py'}))
        self.progress.consume(event('item/commandExecution/outputDelta', itemId='tool', delta='first result'))
        self.now = 0.5
        self.progress.tick()
        self.assertEqual(json.loads((self.output / 'report.json').read_text())['status'], 'RUNNING')
        log = (self.output / 'progress.log').read_text(encoding='utf-8')
        self.assertIn('python test.py', log)
        self.assertIn('first result', log)
        self.assertIn('gpt-6-astra', self.stream.getvalue())
        rows = [json.loads(line) for line in (self.output / 'events.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]['method'], 'item/commandExecution/outputDelta')

    def test_interleaved_children_keep_identity_and_do_not_repeat_complete_text(self):
        self.progress.register_thread('parent', 'gpt-6-astra', 'openai', 'parent')
        self.progress.register_thread('child', 'deepseek-flash', 'deepseek_external', '/root/implement')
        self.progress.consume(event('item/agentMessage/delta', itemId='message', delta='parent words'))
        self.progress.consume(event('item/agentMessage/delta', 'child', itemId='message', delta='child words'))
        self.now = 0.5
        self.progress.tick()
        for thread, text in [('parent', 'parent words'), ('child', 'child words')]:
            self.progress.consume(event('item/completed', thread, item={
                'type': 'agentMessage', 'id': 'message', 'text': text}))
        log = self.stream.getvalue()
        self.assertEqual(log.count('parent words'), 1)
        self.assertEqual(log.count('child words'), 1)
        self.assertIn('[parent · gpt-6-astra]', log)
        self.assertIn('[/root/implement · deepseek-flash]', log)

    def test_reasoning_and_auth_are_not_persisted_or_displayed(self):
        self.progress.add_secret('secret-api-value')
        self.progress.consume({'id': 42, 'method': 'account/chatgptAuthTokens/refresh',
                               'params': {'accessToken': 'secret-auth-value'}})
        self.progress.consume(event('item/reasoning/textDelta', delta='hidden-analysis-one'))
        self.progress.consume(event('item/completed', item={
            'type': 'reasoning', 'id': 'r', 'content': ['hidden-analysis-two']}))
        self.progress.consume(event('turn/completed', turn={'status': 'completed', 'items': [
            {'type': 'reasoning', 'content': ['hidden-analysis-three']},
            {'type': 'agentMessage', 'text': 'visible summary'}]}))
        self.progress.consume(event('item/completed', item={
            'type': 'agentMessage', 'id': 'a', 'text': 'value secret-api-value Bearer abc.def.xyz'}))
        data = self.stream.getvalue() + (self.output / 'events.jsonl').read_text(encoding='utf-8')
        for forbidden in ('hidden-analysis', 'secret-auth-value', 'secret-api-value', 'abc.def.xyz'):
            self.assertNotIn(forbidden, data)
        self.assertIn('visible summary', data)
        self.assertIn('[redacted]', data)

    def test_idle_heartbeat_and_late_route_are_visible(self):
        home = self.output / 'home'
        sessions = home / 'sessions'
        sessions.mkdir(parents=True)
        self.progress.home = home
        self.progress.register_thread('child-id', name='/root/review')
        self.progress.tick()
        (sessions / 'rollout-child-id.jsonl').write_text('\n'.join([
            json.dumps({'type': 'session_meta', 'payload': {'model_provider': 'openai'}}),
            json.dumps({'type': 'turn_context', 'payload': {'model': 'gpt-5.6-luna'}}),
        ]), encoding='utf-8')
        self.now = 16
        self.progress.tick()
        log = self.stream.getvalue()
        self.assertIn('16초 전', log)
        self.assertIn('gpt-5.6-luna', log)
        self.assertIn('/root/review', log)


if __name__ == '__main__':
    unittest.main()
