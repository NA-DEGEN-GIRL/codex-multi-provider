"""Drive a separate Codex CLI app-server with isolated state and transient auth.

Only access tokens are read from the existing login. Refresh tokens are never
copied, logged or used. The app-server's external-auth API keeps auth in memory.
"""
import argparse
import base64
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import uuid

from prepare_profiles import ROOT, prepare, reasoning_effort_for_model
from progress import Progress


def decrypt_key():
    file = ROOT / 'profiles/deepseek.dpapi'
    if not file.exists():
        raise RuntimeError('DeepSeek key is not configured. Open scripts/settings.ps1 first.')
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    raw = file.read_bytes()
    buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    source = Blob(len(raw), buffer)
    target = Blob()
    crypt = ctypes.windll.crypt32.CryptUnprotectData
    crypt.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    if not crypt(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.data, target.size).decode('utf-8')
    finally:
        ctypes.memset(target.data, 0, target.size)
        ctypes.windll.kernel32.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


def existing_auth():
    file = Path.home() / '.codex/auth.json'
    data = json.loads(file.read_text(encoding='utf-8'))
    tokens = data.get('tokens') or {}
    token = tokens.get('access_token')
    account = tokens.get('account_id')
    if not token or not account:
        raise RuntimeError('No readable existing ChatGPT access token. Use the isolated CLI login.')
    payload = token.split('.')[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
    if claims.get('exp', 0) <= time.time() + 120:
        raise RuntimeError('Existing access token expired; use Codex normally to refresh login, then retry.')
    return {'type': 'chatgptAuthTokens', 'accessToken': token, 'chatgptAccountId': account}


def collect_routes(home, parent_id):
    routes = []
    for file in (home / 'sessions').rglob('*.jsonl'):
        metadata = None
        contexts = []
        try:
            for line in file.read_text(encoding='utf-8').splitlines():
                row = json.loads(line)
                if row.get('type') == 'session_meta':
                    metadata = row['payload']
                elif row.get('type') == 'turn_context':
                    contexts.append(row['payload'])
        except (OSError, json.JSONDecodeError):
            continue
        if not metadata:
            continue
        source = metadata.get('source', {})
        if metadata.get('id') != parent_id and parent_id not in json.dumps(source):
            continue
        routes.append({'id': metadata.get('id'), 'provider': metadata.get('model_provider'),
                       'source': source, 'models': list(dict.fromkeys(c.get('model') for c in contexts)),
                       'turn_count': len(contexts),
                       'reasoning_efforts': list(dict.fromkeys(c.get('effort') for c in contexts))})
    return routes


class Client:
    def __init__(self, binary, home, workspace, progress, key=None, desktop_host=False):
        self.progress = progress
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith('CODEX_')}
        env['CODEX_HOME'] = str(home)
        if key:
            env['CODEX_EXTERNAL_DEEPSEEK_API_KEY'] = key
        command = [str(binary)]
        if desktop_host:
            command += ['-c', 'features.code_mode_host=true']
        command += ['app-server', '--listen', 'stdio://']
        self.process = subprocess.Popen(command,
            cwd=workspace, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace',
            creationflags=subprocess.CREATE_NO_WINDOW)
        self.messages = queue.Queue()
        self.stderr = []
        self.events = []
        self.responses = {}
        self.counter = 0
        self.active = {}
        self.turn_efforts = {}
        threading.Thread(target=self.read_stdout, daemon=True).start()
        threading.Thread(target=self.read_stderr, daemon=True).start()
        self.request('initialize', {'clientInfo': {'name': 'codex_provider_lab', 'title': 'Codex provider lab', 'version': '0.1.0'}, 'capabilities': {'experimentalApi': True}})
        self.send({'method': 'initialized'})

    def read_stdout(self):
        for line in self.process.stdout:
            try:
                self.messages.put(json.loads(line))
            except json.JSONDecodeError:
                pass
        self.messages.put(None)

    def read_stderr(self):
        for line in self.process.stderr:
            self.stderr.append(line)
            if len(self.stderr) > 300:
                del self.stderr[:100]

    def send(self, value):
        self.process.stdin.write(json.dumps(value, ensure_ascii=False) + '\n')
        self.process.stdin.flush()

    def next_message(self, timeout):
        end = time.monotonic() + timeout
        while True:
            try:
                message = self.messages.get(timeout=max(0.01, min(0.35, end - time.monotonic())))
                break
            except queue.Empty:
                self.progress.tick()
                if time.monotonic() >= end:
                    raise TimeoutError('Timed out waiting for a Codex app-server event')
        if message is None:
            raise RuntimeError('Codex app-server exited unexpectedly: ' + ''.join(self.stderr[-5:])[:1500])
        if 'id' in message and 'method' not in message:
            self.responses[message['id']] = message
        elif 'id' in message:
            # Never grant escalations or arbitrary MCP permissions in unattended tests.
            method = message.get('method', '')
            if method == 'account/chatgptAuthTokens/refresh':
                auth = existing_auth()
                self.progress.add_secret(auth['accessToken'])
                self.progress.add_secret(auth['chatgptAccountId'])
                self.send({'id': message['id'], 'result': {k: v for k, v in auth.items() if k != 'type'}})
            elif 'requestApproval' in method:
                self.progress.emit('추가 승인이 필요한 도구 요청을 거절했습니다: ' + method)
                self.send({'id': message['id'], 'result': {'decision': 'decline'}})
            else:
                self.send({'id': message['id'], 'error': {'code': -32601, 'message': 'Not supported by lab client'}})
        else:
            safe = self.progress.consume(message)
            if safe is not None:
                self.events.append(safe)
            method = message.get('method', '')
            params = message.get('params', {})
            if method == 'turn/started':
                self.active[params.get('threadId')] = params.get('turn', {}).get('id')
            elif method == 'turn/completed':
                self.active.pop(params.get('threadId'), None)
        return message

    def request(self, method, params, timeout=90):
        self.counter += 1
        identity = self.counter
        self.send({'id': identity, 'method': method, 'params': params})
        end = time.monotonic() + timeout
        while identity not in self.responses:
            self.next_message(max(0.1, end - time.monotonic()))
            if time.monotonic() >= end:
                raise TimeoutError(method)
        response = self.responses.pop(identity)
        if 'error' in response:
            raise RuntimeError(method + ': ' + json.dumps(response['error'], ensure_ascii=False))
        return response.get('result')

    def turn(self, thread_id, prompt, timeout=300):
        start = len(self.events)
        result = self.request('turn/start', {'threadId': thread_id, 'input': [{'type': 'text', 'text': prompt}], 'effort': self.turn_efforts.get(thread_id, 'low')})
        turn_id = result['turn']['id']
        end = time.monotonic() + timeout
        while True:
            for event in self.events[start:]:
                if event.get('method') == 'turn/completed':
                    params = event['params']
                    if params.get('threadId') == thread_id and params['turn']['id'] == turn_id:
                        if params['turn']['status'] != 'completed':
                            raise RuntimeError('Turn did not complete: ' + json.dumps(params['turn'], ensure_ascii=False))
                        return [e['params']['item'].get('text', '') for e in self.events[start:]
                                if e.get('method') == 'item/completed' and e['params'].get('threadId') == thread_id
                                and e['params']['item'].get('type') == 'agentMessage']
            self.next_message(max(0.1, end - time.monotonic()))
            if time.monotonic() >= end:
                raise TimeoutError('Model turn exceeded lab timeout')

    def close(self):
        for thread, turn in list(self.active.items()):
            if thread and turn:
                try:
                    self.request('turn/interrupt', {'threadId': thread, 'turnId': turn}, timeout=15)
                except Exception:
                    pass
        self.process.stdin.close()
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            # This process belongs to the test; no other Codex process is touched.
            self.process.terminate()
            self.process.wait(timeout=15)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['upstream', 'runtime', 'installed'], default='runtime')
    parser.add_argument('--scenario', choices=['baseline', 'deepseek', 'mixed', 'selection', 'mock-mixed', 'custom'], default='baseline')
    parser.add_argument('--prompt-file', type=Path, help='UTF-8 task text for custom runs in a fresh lab workspace')
    parser.add_argument('--desktop-host', action='store_true',
                        help='Test the code_mode_host setting used by the installed desktop app')
    args = parser.parse_args()
    custom_prompt = None
    if args.scenario == 'custom':
        if not args.prompt_file:
            raise RuntimeError('Custom runs require --prompt-file.')
        custom_prompt = args.prompt_file.read_text(encoding='utf-8-sig').strip()
        if not custom_prompt:
            raise RuntimeError('The task is empty.')
    mock = None
    if args.scenario == 'mock-mixed':
        from mock_provider import MockProvider
        mock = MockProvider()
    provider = prepare({'base_url': mock.url, 'model': 'mock-external'} if mock else None)
    mode = 'upstream' if args.mode == 'installed' else args.mode
    binary = ROOT / 'artifacts' / mode / 'codex.exe'
    if args.mode == 'installed':
        binary = Path.home() / 'AppData/Local/OpenAI/Codex/bin/7ac07f4ce733f89a/codex.exe'
    if not binary.exists():
        raise RuntimeError('Runtime has not been built: ' + str(binary))
    if args.mode == 'runtime' and b'external_agents' not in binary.read_bytes():
        raise RuntimeError('Patched runtime is not ready: the external-agent implementation is absent.')
    key = 'lab-mock-token' if mock else decrypt_key() if args.scenario != 'baseline' else None
    run_id = time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
    workspace = ROOT / 'tests/sandbox' / run_id
    workspace.mkdir(parents=True)
    subprocess.run(['git', 'init', '-q', str(workspace)], check=True)
    secret_marker = 'fixture-' + uuid.uuid4().hex
    (workspace / 'input.txt').write_text(secret_marker, encoding='utf-8')
    home = ROOT / 'profiles' / mode
    report = {'run': run_id, 'mode': args.mode, 'scenario': args.scenario, 'desktop_host': args.desktop_host, 'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(), 'workspace': str(workspace), 'provider': provider, 'status': 'RUNNING'}
    output = ROOT / 'artifacts/results' / run_id
    output.mkdir(parents=True)
    progress = Progress(output, home)
    progress.add_secret(key)
    progress.write_report(report)
    progress.emit(f'실험 시작: {args.mode} / {args.scenario}')
    if args.desktop_host:
        progress.emit('앱 런타임 설정: features.code_mode_host=true')
    progress.emit('작업 폴더: ' + str(workspace))
    progress.emit('진행 로그: ' + str(output / 'progress.log'))
    client = None
    try:
        progress.emit('Codex 런타임을 시작하고 로그인 연결을 확인합니다.')
        client = Client(binary, home, workspace, progress, key, desktop_host=args.desktop_host)
        auth = existing_auth()
        progress.add_secret(auth['accessToken'])
        progress.add_secret(auth['chatgptAccountId'])
        client.request('account/login/start', auth)
        params = {'model': 'gpt-6-astra', 'cwd': str(workspace), 'approvalPolicy': 'never', 'sandbox': 'workspace-write',
                  'developerInstructions': 'The user explicitly authorizes native subagent delegation and choosing GPT models or the configured DeepSeek role according to task complexity. Keep tests inside the assigned workspace. Never read credentials. Do not delegate beyond one level. Report actual failures. External children use external_agents with agent_type deepseek and fresh context; GPT children use collaboration.'}
        if args.scenario == 'deepseek':
            params.update(model=provider['model'], modelProvider='deepseek_external', config={'model_catalog_json': str(home / 'deepseek-models.json')})
        started = client.request('thread/start', params)
        thread = started['thread']['id']
        if args.scenario == 'deepseek':
            client.turn_efforts[thread] = reasoning_effort_for_model(provider['model'])
        report['parent'] = {'id': thread, 'model': started['model'], 'provider': started['modelProvider']}
        progress.register_thread(thread, started['model'], started['modelProvider'], '부모')
        progress.write_report(report)
        assert started['model'] == params['model'], 'Unexpected parent model substitution'
        if args.scenario == 'baseline':
            messages = client.turn(thread, 'This is an authorized CLI smoke test. Use a shell tool to read input.txt. Then answer exactly BASELINE_OK followed by its contents. Do not spawn agents.')
            assert any('BASELINE_OK' in m and secret_marker in m for m in messages), 'Baseline marker missing'
        elif args.scenario == 'custom':
            messages = client.turn(thread, custom_prompt, timeout=600)
        elif args.scenario == 'deepseek':
            messages = client.turn(thread, 'Read input.txt with a tool. Create result.txt containing its contents followed by |deepseek. Use a shell tool to read result.txt and verify it. Do not spawn agents. Then say DIRECT_OK.')
            assert (workspace / 'result.txt').read_text(encoding='utf-8').strip() == secret_marker + '|deepseek', 'External file result incorrect'
        else:
            prompt = 'This is an authorized native V2 cross-provider test. Do not read or edit input.txt yourself. Spawn a DeepSeek child using external_agents.spawn_agent, agent_type deepseek, task_name external_worker, fork_turns none. Ask it to read input.txt, create result.txt with its contents followed by |deepseek, and verify the file using a tool. Also spawn a GPT child with collaboration.spawn_agent using model gpt-5.6-sol, fork_turns none, task_name gpt_worker; ask it to compute 37*19 and report it. Wait for both children to finish; retain their IDs. Report their IDs and results. Do not substitute yourself or GPT for a failed DeepSeek child.'
            if args.scenario == 'selection':
                prompt = 'Use native subagents for these two independent tasks. You may choose the configured DeepSeek role or a GPT model based on complexity and cost; make the choice yourself and state it. Task one: read input.txt and copy it to selection.txt. Task two: independently verify 37*19. Do not perform the subtasks yourself. Wait for results and report actual model choices.'
            messages = client.turn(thread, prompt, timeout=420)
            if args.scenario == 'selection':
                assert (workspace / 'selection.txt').read_text(encoding='utf-8').strip() == secret_marker, 'Autonomously delegated file result incorrect'
            if args.scenario in ('mixed', 'mock-mixed'):
                assert (workspace / 'result.txt').read_text(encoding='utf-8').strip() == secret_marker + '|deepseek', 'Delegated file result incorrect'
                messages += client.turn(thread, 'Send a followup_task to the SAME external_worker DeepSeek child. Ask it to append |followup to result.txt and verify it using a tool. Wait for completion and report the child ID. Do not create a replacement child or edit the file yourself.', timeout=300)
                assert (workspace / 'result.txt').read_text(encoding='utf-8').strip() == secret_marker + '|deepseek|followup', 'Same-child followup file result incorrect'
        report['messages'] = messages
        report['status'] = 'COMPLETED' if args.scenario == 'custom' else 'PASS'
    except Exception as error:
        report['status'] = 'FAIL'
        report['error'] = progress.clean(str(error))[:3000]
        progress.emit('실험 오류: ' + report['error'])
    finally:
        progress.emit('실행 기록을 정리합니다.')
        if client:
            try:
                client.close()
            except Exception as error:
                report['status'] = 'FAIL'
                report['cleanup_error'] = progress.clean(str(error))[:3000]
                report.setdefault('error', 'Runtime cleanup failed: ' + report['cleanup_error'])
                progress.emit('런타임 종료 오류: ' + report['cleanup_error'])
            # Only filtered notifications are retained; account and reasoning payloads are omitted.
            (output / 'events.json').write_text(json.dumps(client.events, indent=2, ensure_ascii=False), encoding='utf-8')
        if 'parent' in report:
            report['routes'] = collect_routes(home, report['parent']['id'])
            flash_routes = [r for r in report['routes'] if r['provider'] == 'deepseek_external' and 'deepseek-flash' in r['models']]
            if any(r['reasoning_efforts'] != ['max'] for r in flash_routes):
                report.update(status='FAIL', error='DeepSeek Flash ran without the required max reasoning effort.')
            if report['status'] == 'PASS' and args.scenario == 'selection':
                children = [r for r in report['routes'] if r['id'] != report['parent']['id']]
                if len(children) < 2 or any(not r['models'] for r in children):
                    report.update(status='FAIL', error='Autonomous selection did not produce two recorded child model routes.')
            if report['status'] == 'PASS' and args.scenario in ('mixed', 'mock-mixed'):
                children = [r for r in report['routes'] if r['id'] != report['parent']['id']]
                external = [r for r in children if r['provider'] == 'deepseek_external' and provider['model'] in r['models']]
                native = [r for r in children if r['provider'] == 'openai' and 'gpt-5.6-sol' in r['models']]
                if not external or not native or max(r['turn_count'] for r in external) < 2:
                    report.update(status='FAIL', error='File checks passed but recorded mixed-provider/same-child routing evidence is incomplete.')
        if mock:
            report['mock_calls'] = mock.calls
            if any(c['encrypted_payload_present'] or c['openai_account_header_present'] for c in mock.calls):
                report.update(status='FAIL', error='Native encrypted payload or OpenAI account header reached mock external provider.')
            mock.close()
            prepare()
        progress.write_report(report)
        progress.emit('최종 상태: ' + report['status'])
        progress.emit('결과 보고서: ' + str(output / 'report.json'))
        progress.close()
    print(json.dumps(progress.clean(report), indent=2, ensure_ascii=False), flush=True)
    return 0 if report['status'] in ('PASS', 'COMPLETED') else 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        import msvcrt
        lock_path = ROOT / 'work/live-test.lock'
        lock_path.parent.mkdir(exist_ok=True)
        with lock_path.open('a+b') as lock:
            lock.seek(0)
            if not lock.read(1):
                lock.write(b'0')
                lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError('Another lab test is already running; wait for it to finish.')
            sys.exit(main())
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(2)
