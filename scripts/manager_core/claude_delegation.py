"""Authenticated, turn-scoped IPC for the native agent-control MCP bridge."""
import json
import os
from pathlib import Path
import sys
import threading
from multiprocessing import AuthenticationError
from multiprocessing.connection import Listener
from uuid import uuid4

from .claude_protocol import MAX_LINE_BYTES

TOOLS = frozenset(('spawn_agent', 'followup_task', 'send_message', 'list_agents', 'wait_agent', 'interrupt_agent'))
MAX_REQUEST_BYTES = 32768
GUIDANCE = ('For task delegation, use only the codex_agents MCP tools and their configured agent_type roles. '
            'These tools preserve the selected accounts, permissions, task limits, results, and cancellation. '
            'The built-in Agent and Task tools are disabled. Do not launch replacement agent processes through shell commands.')
LEAF_GUIDANCE = ('Task delegation is disabled by the selected execution preset in this task. '
                 'The built-in Agent and Task tools are disabled. Do not launch replacement agent processes through shell commands.')


class DelegationBridge:
    def __init__(self, emit, stopped, catalog, temporary_directory):
        if not isinstance(catalog, list) or len(catalog) > len(TOOLS):
            raise ValueError('Invalid native delegation tool catalog.')
        self.names = set()
        for tool in catalog:
            if (not isinstance(tool, dict) or not isinstance(tool.get('name'), str) or tool['name'] not in TOOLS
                    or tool['name'] in self.names or not isinstance(tool.get('inputSchema'), dict)):
                raise ValueError('Invalid native delegation tool catalog.')
            self.names.add(tool['name'])
        self.emit, self.stopped = emit, stopped
        self.pending, self.guard = {}, threading.Lock()
        self.key = os.urandom(32)
        self.temp = temporary_directory('codex-claude-agents-')
        self.catalog = Path(self.temp.name) / 'tools.json'
        self.catalog.write_text(json.dumps(catalog, ensure_ascii=False, allow_nan=False), encoding='utf-8')
        self.family = 'AF_PIPE' if os.name == 'nt' else 'AF_UNIX'
        self.address = (r'\\.\pipe\codex-claude-agents-' + str(uuid4()) if os.name == 'nt'
                        else str(Path(self.temp.name) / 'agents.sock'))
        self.listener = Listener(self.address, family=self.family, authkey=self.key)
        threading.Thread(target=self._accept, daemon=True).start()

    def configuration(self):
        return {'codex_agents': {'command': sys.executable,
                'args': ['-X', 'utf8', str(Path(__file__).with_name('claude_delegation_mcp.py')),
                         '--catalog', str(self.catalog)],
                'env': {'CODEX_AGENTS_ADDRESS': self.address, 'CODEX_AGENTS_FAMILY': self.family,
                        'CODEX_AGENTS_KEY': self.key.hex()}}}

    def _accept(self):
        while not self.stopped.is_set():
            try:
                connection = self.listener.accept()
            except AuthenticationError:
                continue
            except (OSError, EOFError):
                return
            threading.Thread(target=self._handle, args=(connection,), daemon=True).start()

    def _handle(self, connection):
        with connection:
            try:
                request = json.loads(connection.recv_bytes(MAX_REQUEST_BYTES))
                answer = self.call(request)
                connection.send_bytes(json.dumps(answer, ensure_ascii=False, allow_nan=False).encode('utf-8'))
            except (OSError, EOFError, ValueError):
                return

    def call(self, request):
        unavailable = {'error': 'Native agent delegation is unavailable or interrupted.'}
        if (not isinstance(request, dict) or set(request) != {'name', 'arguments'}
                or not isinstance(request.get('name'), str)
                or request['name'] not in self.names or not isinstance(request.get('arguments'), dict)):
            return {'error': 'Unsupported native agent request.'}
        if len(json.dumps(request, ensure_ascii=False, allow_nan=False).encode('utf-8')) > MAX_REQUEST_BYTES:
            return {'error': 'Native agent request exceeds the transport limit.'}
        request_id, arrived = str(uuid4()), threading.Event()
        slot = {'event': arrived, 'response': None}
        with self.guard:
            if self.stopped.is_set() or len(self.pending) >= 8:
                return unavailable
            self.pending[request_id] = slot
        try:
            self.emit({'type': 'agent_request', 'id': request_id,
                       'name': request['name'], 'arguments': request['arguments']})
            while not self.stopped.is_set() and not arrived.wait(.1):
                pass
            return slot['response'] if not self.stopped.is_set() and slot['response'] is not None else unavailable
        finally:
            with self.guard:
                self.pending.pop(request_id, None)

    def respond(self, response):
        if (not isinstance(response, dict) or not isinstance(response.get('id'), str)
                or ('result' not in response and not isinstance(response.get('error'), str))):
            return
        payload = {key: response[key] for key in ('result', 'error') if key in response}
        if len(json.dumps(payload, ensure_ascii=False).encode('utf-8')) > MAX_LINE_BYTES:
            payload = {'error': 'Native delegation result exceeded the transport limit.'}
        with self.guard:
            slot = self.pending.get(response['id'])
            if slot and slot['response'] is None:
                slot['response'] = payload
                slot['event'].set()

    def release(self):
        """Answer a stopped CLI process's waiting calls as unavailable; the bridge stays open."""
        with self.guard:
            for slot in self.pending.values():
                slot['event'].set()

    def close(self):
        self.stopped.set()
        self.listener.close()
        self.temp.cleanup()
