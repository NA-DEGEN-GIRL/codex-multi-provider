"""Deterministic Responses fixture. This is not a live DeepSeek model."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import uuid


def encrypted_payload_present(value):
    if isinstance(value, dict):
        return any((k in ('encrypted_content', 'encrypted_function_args') and bool(v)) or encrypted_payload_present(v)
                   for k, v in value.items())
    if isinstance(value, list):
        return any(encrypted_payload_present(v) for v in value)
    return False


class MockProvider:
    def __init__(self):
        self.calls = []
        self.step = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                size = int(self.headers.get('Content-Length', '0'))
                raw = self.rfile.read(size)
                if self.headers.get('Content-Encoding'):
                    self.send_error(415, 'Mock expects uncompressed JSON')
                    return
                data = json.loads(raw)
                if self.headers.get('Authorization') != 'Bearer lab-mock-token':
                    self.send_error(401, 'Wrong provider credential')
                    return
                owner.calls.append({'model': data.get('model'), 'path': self.path,
                                    'input_types': [x.get('type') for x in data.get('input', [])],
                                    'encrypted_payload_present': encrypted_payload_present(data.get('input', [])),
                                    'openai_account_header_present': bool(self.headers.get('ChatGPT-Account-Id')),
                                    'tool_names': [t.get('name') for t in data.get('tools', [])]})
                functions = []
                for tool in data.get('tools', []):
                    if tool.get('type') == 'namespace':
                        functions.extend((tool.get('name'), t) for t in tool.get('tools', []))
                    else:
                        functions.append((None, tool))
                shell = next(((ns, t) for ns, t in functions if t.get('name') in ('shell_command', 'exec_command', 'shell')), None)
                inputs = data.get('input', [])
                last = inputs[-1] if inputs else {}
                messages = [x for x in inputs if x.get('type') == 'message' and x.get('role') == 'user']
                task = json.dumps(messages[-1] if messages else {}).lower()
                if last.get('type') != 'function_call_output' and shell:
                    ns, tool = shell
                    if 'append' in task and '|followup' in task:
                        command = "$v = Get-Content -LiteralPath result.txt -Raw; if (-not $v.Trim().EndsWith('|followup')) { [IO.File]::WriteAllText((Join-Path $PWD 'result.txt'), $v.Trim() + '|followup') }; Get-Content -LiteralPath result.txt"
                    elif 'create' in task:
                        command = "$v = Get-Content -LiteralPath input.txt -Raw; [IO.File]::WriteAllText((Join-Path $PWD 'result.txt'), $v.Trim() + '|deepseek'); Get-Content -LiteralPath result.txt"
                    else:
                        command = "Get-Content -LiteralPath result.txt"
                    properties = tool.get('parameters', {}).get('properties', {})
                    args = {'cmd' if 'cmd' in properties else 'command': command}
                    if tool['name'] == 'shell':
                        args['command'] = ['pwsh.exe', '-NoProfile', '-Command', command]
                    item = {'type': 'function_call', 'id': 'fc_' + uuid.uuid4().hex, 'call_id': 'call_' + uuid.uuid4().hex,
                            'name': tool['name'], 'arguments': json.dumps(args), 'status': 'completed'}
                    if ns:
                        item['namespace'] = ns
                else:
                    item = {'type': 'message', 'id': 'msg_' + uuid.uuid4().hex, 'role': 'assistant', 'status': 'completed',
                            'content': [{'type': 'output_text', 'text': 'MOCK_EXTERNAL_DONE. Tool output: ' + json.dumps(last.get('output', 'No tool result'))[:1800], 'annotations': []}]}
                owner.step += 1
                identity = 'resp_' + uuid.uuid4().hex
                events = [
                    ('response.created', {'type': 'response.created', 'response': {'id': identity, 'status': 'in_progress', 'output': []}}),
                    ('response.output_item.added', {'type': 'response.output_item.added', 'output_index': 0, 'item': item}),
                    ('response.output_item.done', {'type': 'response.output_item.done', 'output_index': 0, 'item': item}),
                    ('response.completed', {'type': 'response.completed', 'response': {'id': identity, 'status': 'completed', 'output': [item], 'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}})]
                body = ''.join('event: ' + name + '\ndata: ' + json.dumps(payload) + '\n\n' for name, payload in events).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/v1'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
