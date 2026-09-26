import io
import json
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.windows_sandbox_setup import SandboxSetupRecovery, run_setup, PROFILE_ERROR, COMPLETED


def start(identity=1):
    return {'id': identity, 'method': 'windowsSandbox/setupStart',
            'params': {'mode': 'elevated', 'cwd': 'C:/workspace'}}


def completed(error=PROFILE_ERROR, success=False):
    return {'method': COMPLETED, 'params': {'mode': 'elevated', 'success': success, 'error': error}}


class SandboxSetupTests(unittest.TestCase):
    def test_only_explicit_accepted_request_known_error_retries_once(self):
        emitted, calls = [], []
        entered, finish, done = threading.Event(), threading.Event(), threading.Event()
        def runner(*args):
            calls.append(args)
            entered.set()
            self.assertTrue(finish.wait(3))
            return {'mode': 'elevated', 'success': True, 'error': None}
        def emit(message):
            emitted.append(message)
            if message.get('method') == COMPLETED:
                done.set()
        shim = SandboxSetupRecovery('codex.exe', ['app-server'], {'CODEX_HOME': 'profile'}, emit, runner=runner)
        self.assertTrue(shim.response(completed()))
        self.assertFalse(shim.request(start()))
        shim.response({'id': 1, 'result': {'started': True}})
        self.assertFalse(shim.response(completed()))
        self.assertTrue(entered.wait(3))
        self.assertTrue(shim.request(start(2)))
        self.assertEqual(emitted[0]['error']['code'], -32046)
        finish.set()
        self.assertTrue(done.wait(3))
        self.assertEqual(len(calls), 1)
        self.assertEqual(emitted[-1]['params']['success'], True)
        self.assertEqual(calls[0][3], start()['params'])
        self.assertFalse(shim.request(start(3)))

    def test_success_uac_cancellation_and_other_errors_are_not_retried(self):
        for notification in (completed(success=True, error=None), completed('UAC cancelled'), completed('Access denied')):
            def forbidden(*args):
                self.fail('Unexpected setup retry')
            shim = SandboxSetupRecovery('', [], {}, lambda _: None, runner=forbidden)
            shim.request(start())
            shim.response({'id': 1, 'result': {'started': True}})
            self.assertTrue(shim.response(notification))
            self.assertIsNone(shim.pending)

    def test_rejected_start_cannot_trigger_setup(self):
        shim = SandboxSetupRecovery('', [], {}, lambda _: None)
        shim.request(start())
        shim.response({'id': 1, 'error': {'code': -1}})
        self.assertTrue(shim.response(completed()))
        self.assertIsNone(shim.pending)

    def test_setup_override_is_child_only_and_rpc_contains_no_work(self):
        class Input(io.BytesIO):
            def close(self):
                self.saved = self.getvalue()
                super().close()
        class Child:
            stdin = Input()
            stdout = io.BytesIO(b'\n'.join(json.dumps(x).encode() for x in [
                {'id': 1, 'result': {}}, {'id': 2, 'result': {'started': True}},
                completed(success=True, error=None)]) + b'\n')
            def wait(self, **kwargs): return 0
        calls = []
        def popen(args, **kwargs):
            calls.append((args, kwargs))
            return Child()
        args, env = ['app-server', '-c', 'sandbox_mode="danger-full-access"'], {'CODEX_HOME': 'profile'}
        before = args.copy(), env.copy()
        result = run_setup('codex.exe', args, env, start()['params'], popen=popen)
        self.assertTrue(result['success'])
        self.assertEqual((args, env), before)
        self.assertEqual(calls[0][0][-2:], ['-c', 'sandbox_mode="workspace-write"'])
        messages = [json.loads(row) for row in Child.stdin.saved.splitlines()]
        self.assertEqual([m['method'] for m in messages], ['initialize', 'initialized', 'windowsSandbox/setupStart'])


if __name__ == '__main__':
    unittest.main()
