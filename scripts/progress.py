"""Live, readable app-server activity without account or reasoning payloads."""
import json
from pathlib import Path
import re
import sys
import time


class Progress:
    def __init__(self, output, home=None, stream=None, clock=time.monotonic):
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.home = Path(home) if home else None
        self.stream = stream if stream is not None else sys.stdout
        self.clock = clock
        self.started = clock()
        self.last_event = self.started
        self.last_heartbeat = self.started
        self.log = (self.output / 'progress.log').open('w', encoding='utf-8', buffering=1)
        self.journal = (self.output / 'events.jsonl').open('w', encoding='utf-8', buffering=1)
        self.threads = {}
        self.route_checks = {}
        self.pending = {}
        self.streamed = set()
        self.output_sizes = {}
        self.completed = set()
        self.secrets = set()

    def add_secret(self, value):
        if value:
            self.secrets.add(value)

    def clean(self, value):
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                normalized = re.sub(r'[^a-z]', '', key.lower())
                if normalized in {'accesstoken', 'refreshtoken', 'apikey', 'authorization',
                                  'chatgptaccountid', 'accountid', 'encryptedcontent'}:
                    result[key] = '[redacted]'
                elif key == 'type' and item == 'reasoning':
                    return {'type': 'reasoning', 'omitted': True}
                else:
                    result[key] = self.clean(item)
            return result
        if isinstance(value, list):
            return [self.clean(item) for item in value]
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, '[redacted]')
            value = re.sub(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer [redacted]', value)
            return re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[redacted]', value)
        return value

    def write_report(self, report):
        # Readers always see a complete JSON document, including while running.
        temporary = self.output / 'report.json.tmp'
        temporary.write_text(json.dumps(self.clean(report), indent=2, ensure_ascii=False), encoding='utf-8')
        temporary.replace(self.output / 'report.json')

    def label(self, thread_id):
        if not thread_id:
            return '실험실'
        info = self.threads.get(thread_id, {})
        name = info.get('name') or thread_id[-8:]
        model = info.get('model')
        return f'{name} · {model}' if model else name

    def emit(self, text, thread_id=None):
        elapsed = int(self.clock() - self.started)
        prefix = f'[{elapsed // 60:02d}:{elapsed % 60:02d}] [{self.label(thread_id)}] '
        lines = self.clean(str(text)).replace('\r\n', '\n').replace('\r', '\n').splitlines()
        rendered = ''.join(prefix + line + '\n' for line in lines if line.strip())
        if rendered:
            self.log.write(rendered)
            self.log.flush()
            self.stream.write(rendered)
            self.stream.flush()

    def register_thread(self, thread_id, model=None, provider=None, name=None):
        if not thread_id:
            return
        info = self.threads.setdefault(thread_id, {})
        old_model = info.get('model')
        for key, value in [('model', model), ('provider', provider), ('name', name)]:
            if value:
                info[key] = value
        if model and model != old_model:
            self.emit(f'모델 연결: {model} ({provider or info.get("provider", "provider 확인 중")})', thread_id)

    def discover_route(self, thread_id):
        if not self.home or not thread_id or self.threads.get(thread_id, {}).get('model'):
            return
        now = self.clock()
        if now - self.route_checks.get(thread_id, -100) < 2:
            return
        self.route_checks[thread_id] = now
        # Only inspect this known lab thread, and stop before reading its conversation.
        for file in (self.home / 'sessions').rglob(f'*{thread_id}.jsonl'):
            provider = None
            try:
                with file.open(encoding='utf-8') as source:
                    for index, line in enumerate(source):
                        if index > 100:
                            break
                        event = json.loads(line)
                        payload = event.get('payload', {})
                        if event.get('type') == 'session_meta':
                            provider = payload.get('model_provider')
                        elif event.get('type') == 'turn_context':
                            self.register_thread(thread_id, payload.get('model'), provider)
                            return
            except (OSError, json.JSONDecodeError):
                pass  # A just-created rollout may not have flushed its first turn yet.

    def safe_event(self, event):
        method = event.get('method', '')
        if 'id' in event or 'reasoning' in method.lower():
            return None
        if not method.startswith(('thread/', 'turn/', 'item/', 'error', 'mcpServer/')):
            return None
        item = event.get('params', {}).get('item', {})
        if item.get('type') in ('reasoning', 'userMessage'):
            return None
        return self.clean(event)

    def consume(self, event):
        safe = self.safe_event(event)
        if safe is not None:
            self.journal.write(json.dumps(safe, ensure_ascii=False) + '\n')
            self.journal.flush()
        method = event.get('method', '')
        params = event.get('params', {})
        thread_id = params.get('threadId')
        self.last_event = self.clock()
        self.discover_route(thread_id)
        if method == 'thread/started':
            thread = params.get('thread', {})
            self.register_thread(thread.get('id'), thread.get('model'), thread.get('modelProvider'),
                                 thread.get('agentRole') or '부모')
        elif method == 'turn/started':
            self.emit('작업 시작', thread_id)
        elif method == 'turn/completed':
            self.flush(thread_id=thread_id)
            turn = params.get('turn', {})
            self.emit('작업 상태: ' + str(turn.get('status', 'unknown')), thread_id)
            if turn.get('error'):
                self.emit('오류: ' + json.dumps(self.clean(turn['error']), ensure_ascii=False), thread_id)
        elif method in ('item/agentMessage/delta', 'item/commandExecution/outputDelta'):
            kind = '메시지' if method == 'item/agentMessage/delta' else '출력'
            key = (thread_id, params.get('itemId'), kind)
            text = params.get('delta', '')
            if text:
                self.streamed.add(key)
                pending, since = self.pending.get(key, ('', self.clock()))
                self.pending[key] = (pending + text, since)
        elif method in ('item/started', 'item/completed'):
            item = params.get('item', {})
            kind = item.get('type')
            done = method == 'item/completed'
            identity = (thread_id, item.get('id'), kind, done)
            if identity in self.completed:
                return safe
            self.completed.add(identity)
            if kind == 'agentMessage' and done:
                key = (thread_id, item.get('id'), '메시지')
                self.flush(key=key)
                if key not in self.streamed and item.get('text'):
                    self.emit('메시지: ' + item['text'], thread_id)
            elif kind == 'commandExecution':
                key = (thread_id, item.get('id'), '출력')
                if done:
                    self.flush(key=key)
                    if key not in self.streamed and item.get('aggregatedOutput'):
                        self.emit_output(key, item['aggregatedOutput'])
                    self.emit(f'명령 완료: {item.get("status", "unknown")} · 종료 코드 {item.get("exitCode")}', thread_id)
                else:
                    self.flush()
                    command = item.get('command', '')
                    if len(command) > 1600:
                        command = command[:1600] + ' … (전체 명령: events.jsonl)'
                    self.emit('명령 실행: ' + command, thread_id)
            elif kind == 'subAgentActivity' and not done:
                child = item.get('agentThreadId')
                self.register_thread(child, name=item.get('agentPath'))
                self.discover_route(child)
                self.emit(f'자식 활동: {item.get("kind", "unknown")} → {self.label(child)}', thread_id)
            elif kind == 'fileChange':
                names = ', '.join(str(change.get('path', '')) for change in item.get('changes', []))
                self.emit(f'파일 변경 {"완료" if done else "시작"}: {names}', thread_id)
            elif kind in ('collabAgentToolCall', 'mcpToolCall', 'dynamicToolCall', 'webSearch'):
                self.flush()
                tool = item.get('tool') or item.get('name') or kind
                self.emit(f'도구 {"완료" if done else "시작"}: {tool}', thread_id)
            elif kind == 'reasoning' and not done:
                # An activity indicator only; never display the reasoning payload.
                self.flush()
                self.emit('모델 응답 생성 중', thread_id)
        elif method == 'error':
            self.emit('오류: ' + json.dumps(self.clean(params), ensure_ascii=False), thread_id)
        self.tick()
        return safe

    def emit_output(self, key, text):
        size = self.output_sizes.get(key, 0)
        available = max(0, 12000 - size)
        if available:
            self.emit('출력: ' + text[:available], key[0])
        if size <= 12000 < size + len(text):
            self.emit('출력이 길어 화면 표시를 줄였습니다. 전체 출력: events.jsonl', key[0])
        self.output_sizes[key] = size + len(text)

    def flush(self, key=None, thread_id=None, aged=False):
        for identity, (text, since) in list(self.pending.items()):
            if key is not None and identity != key:
                continue
            if thread_id is not None and identity[0] != thread_id:
                continue
            if aged and self.clock() - since < 0.35 and '\n' not in text:
                continue
            self.pending.pop(identity)
            if identity[2] == '출력':
                self.emit_output(identity, text)
            else:
                self.emit('메시지: ' + text, identity[0])

    def tick(self):
        self.flush(aged=True)
        now = self.clock()
        if now - self.last_heartbeat >= 15:
            quiet = int(now - self.last_event)
            self.emit(f'실행 중 · 마지막 이벤트 {quiet}초 전' if quiet >= 5 else '실행 중 · 서버 이벤트 수신 중')
            self.last_heartbeat = now
        for thread_id in list(self.threads):
            self.discover_route(thread_id)

    def close(self):
        self.flush()
        self.log.close()
        self.journal.close()
