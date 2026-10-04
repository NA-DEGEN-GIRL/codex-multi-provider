"""Small stdio MCP permission tool, connected only to its authenticated runner."""
import json
import os
import sys
from multiprocessing.connection import Client

if __package__ in (None, ''):
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from manager_core.claude_protocol import MAX_LINE_BYTES, ProtocolError, encode_message


TOOL = {'name': 'approve',
        'description': 'Internal Claude permission handler v1. Do not invoke as a model tool.',
        'inputSchema': {'type': 'object', 'properties': {
            'tool_name': {'type': 'string'}, 'input': {'type': 'object'},
            'tool_use_id': {'type': 'string'}}, 'required': ['tool_name', 'input']}}


def permission(arguments):
    deny = {'behavior': 'deny', 'message': 'Claude permission bridge is unavailable.'}
    try:
        address = os.environ['CLAUDE_BRIDGE_ADDRESS']
        family = os.environ['CLAUDE_BRIDGE_FAMILY']
        key = bytes.fromhex(os.environ['CLAUDE_BRIDGE_KEY'])
        # JSON bytes avoid deserializing arbitrary pickle objects from IPC.
        with Client(address, family=family, authkey=key) as connection:
            payload = json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode('utf-8')
            if len(payload) > MAX_LINE_BYTES:
                return deny
            connection.send_bytes(payload)
            response = json.loads(connection.recv_bytes(MAX_LINE_BYTES))
            if isinstance(response, dict) and response.get('behavior') in ('allow', 'deny'):
                return response
    except (OSError, EOFError, ValueError, KeyError):
        pass
    return deny


def dispatch(request):
    method, request_id = request.get('method'), request.get('id')
    if request_id is None:
        return None
    if method == 'initialize':
        result = {'protocolVersion': (request.get('params') or {}).get('protocolVersion', '2024-11-05'),
                  'capabilities': {'tools': {}}, 'serverInfo': {'name': 'codex_bridge', 'version': '1.0'}}
    elif method == 'ping':
        result = {}
    elif method == 'tools/list':
        result = {'tools': [TOOL]}
    elif method == 'tools/call' and (request.get('params') or {}).get('name') == 'approve':
        arguments = (request.get('params') or {}).get('arguments') or {}
        result = {'content': [{'type': 'text', 'text': json.dumps(permission(arguments), ensure_ascii=False)}]}
    else:
        return {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': -32601, 'message': 'Method not found'}}
    return {'jsonrpc': '2.0', 'id': request_id, 'result': result}


def main():
    while True:
        line = sys.stdin.buffer.readline(MAX_LINE_BYTES + 1)
        if not line:
            return 0
        if len(line) > MAX_LINE_BYTES:
            return 1
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                return 1
            response = dispatch(request)
            if response is not None:
                sys.stdout.buffer.write(encode_message(response))
                sys.stdout.buffer.flush()
        except (ValueError, UnicodeError, ProtocolError):
            return 1


if __name__ == '__main__':
    raise SystemExit(main())
