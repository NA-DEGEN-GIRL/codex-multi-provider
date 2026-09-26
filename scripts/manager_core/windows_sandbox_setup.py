"""Compatibility repair for setup with an unrestricted working permission.

Only an explicitly requested setup that failed before provisioning with the
known permission conversion error is retried. A separate app-server receives
a process-local workspace-write override; the working runtime and the saved
sandbox_mode are never changed. Native requirements and UAC still apply.
"""
import json
import queue
import subprocess
import threading
import time


PROFILE_ERROR = 'only managed permission profiles can be enforced by the Windows sandbox'
COMPLETED = 'windowsSandbox/setupCompleted'


def run_setup(runtime, arguments, environment, params, *, timeout=600, popen=subprocess.Popen):
    child = popen([str(runtime), *arguments, '-c', 'sandbox_mode="workspace-write"'],
                  env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                  stderr=subprocess.DEVNULL,
                  creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    received = queue.Queue()

    def read():
        try:
            while True:
                line = child.stdout.readline(1024 * 1024 + 1)
                if not line or len(line) > 1024 * 1024:
                    break
                received.put(json.loads(line))
        except (OSError, ValueError):
            pass
        finally:
            received.put(None)

    def send(message):
        child.stdin.write(json.dumps(message).encode('utf-8') + b'\n')
        child.stdin.flush()

    threading.Thread(target=read, daemon=True).start()
    try:
        send({'id': 1, 'method': 'initialize', 'params': {
            'clientInfo': {'name': 'codex_workspace_sandbox_setup', 'version': '1'},
            'capabilities': {'experimentalApi': True}}})
        deadline = time.monotonic() + timeout
        while True:
            message = received.get(timeout=max(0.01, deadline - time.monotonic()))
            if message is None:
                raise RuntimeError('Sandbox setup helper exited before confirming installation.')
            if message.get('id') == 1 and 'method' not in message:
                if 'error' in message:
                    raise RuntimeError('Sandbox setup helper could not initialize.')
                send({'method': 'initialized'})
                send({'id': 2, 'method': 'windowsSandbox/setupStart', 'params': params})
            elif message.get('id') == 2 and 'error' in message:
                raise RuntimeError('Native sandbox setup rejected the installation request.')
            elif message.get('method') == COMPLETED:
                result = message.get('params', {})
                if result.get('mode') != params['mode'] or not isinstance(result.get('success'), bool):
                    raise RuntimeError('Sandbox setup returned an invalid completion.')
                return result
            elif 'id' in message and 'method' in message:
                # Setup must not request model work, credentials or tool execution.
                send({'id': message['id'], 'error': {'code': -32601, 'message': 'Setup-only helper.'}})
    except queue.Empty:
        raise RuntimeError('Sandbox setup confirmation timed out. Check the Windows permission dialog before retrying.') from None
    finally:
        try:
            child.stdin.close()
        except OSError:
            pass
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            # This helper has no turns. Do not kill a UAC installer or another
            # profile's runtime; closing its input allows normal setup cleanup.
            pass


class SandboxSetupRecovery:
    def __init__(self, runtime, arguments, environment, emit, *, runner=run_setup):
        self.runtime, self.arguments, self.environment = runtime, list(arguments), dict(environment)
        self.emit, self.runner = emit, runner
        self.pending = None
        self.accepted = self.recovering = False
        self.lock = threading.RLock()

    def request(self, message):
        """True only when a duplicate was consumed; otherwise forward unchanged."""
        if message.get('method') != 'windowsSandbox/setupStart' or 'id' not in message:
            return False
        params = message.get('params')
        if not isinstance(params, dict) or params.get('mode') not in ('elevated', 'unelevated'):
            return False
        with self.lock:
            if self.pending is not None:
                self.emit({'id': message['id'], 'error': {'code': -32046,
                           'message': 'Windows sandbox setup is already running.'}})
                return True
            self.pending = (message['id'], dict(params))
            self.accepted = False
        return False

    def response(self, message):
        """False suppresses only the known pre-installation failure notification."""
        with self.lock:
            if self.pending is None:
                return True
            request_id, params = self.pending
            if message.get('id') == request_id and 'method' not in message:
                self.accepted = (message.get('result') or {}).get('started') is True
                if not self.accepted:
                    self.pending = None
            elif message.get('method') == COMPLETED and not self.recovering:
                result = message.get('params') or {}
                if result.get('mode') != params['mode']:
                    return True
                if self.accepted and result.get('success') is False and result.get('error') == PROFILE_ERROR:
                    self.recovering = True
                    threading.Thread(target=self._recover, args=(params,), daemon=True).start()
                    return False
                self.pending = None
        return True

    def _recover(self, params):
        try:
            result = self.runner(self.runtime, self.arguments, self.environment, params)
        except Exception:
            # Do not leak a command line, environment, credential, or child output.
            result = {'mode': params['mode'], 'success': False,
                      'error': 'Windows sandbox setup could not be confirmed. Check the Windows permission dialog and retry.'}
        with self.lock:
            self.pending = None
            self.accepted = self.recovering = False
        self.emit({'method': COMPLETED, 'params': result})
