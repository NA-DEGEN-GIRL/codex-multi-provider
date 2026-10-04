"""Model-callable delegation tools backed by the current native task's controller."""
import argparse
import json
import os
from pathlib import Path
import sys
from multiprocessing.connection import Client

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from manager_core.claude_delegation import MAX_REQUEST_BYTES, TOOLS
from manager_core.claude_protocol import MAX_LINE_BYTES, ProtocolError, encode_message


def call_native(name, arguments):
    try:
        payload = json.dumps({'name': name, 'arguments': arguments}, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if not isinstance(name, str) or name not in TOOLS or not isinstance(arguments, dict) or len(payload) > MAX_REQUEST_BYTES:
            return {'error': 'Unsupported or oversized native agent request.'}
        with Client(os.environ['CODEX_AGENTS_ADDRESS'], family=os.environ['CODEX_AGENTS_FAMILY'],
                    authkey=bytes.fromhex(os.environ['CODEX_AGENTS_KEY'])) as connection:
            connection.send_bytes(payload)
            response = json.loads(connection.recv_bytes(MAX_LINE_BYTES))
            if isinstance(response, dict) and ('result' in response or isinstance(response.get('error'), str)):
                return response
    except (OSError, EOFError, ValueError, KeyError):
        pass
    return {'error': 'Native agent delegation is unavailable or interrupted.'}


def dispatch(request, catalog):
    method, request_id = request.get('method'), request.get('id')
    if request_id is None:
        return None
    params = request.get('params') or {}
    if not isinstance(params, dict):
        return {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': -32602, 'message': 'Invalid params'}}
    if method == 'initialize':
        result = {'protocolVersion': params.get('protocolVersion', '2024-11-05'),
                  'capabilities': {'tools': {}}, 'serverInfo': {'name': 'codex_agents', 'version': '1.0'}}
    elif method == 'ping':
        result = {}
    elif method == 'tools/list':
        result = {'tools': catalog}
    elif method == 'tools/call':
        name, arguments = params.get('name'), params.get('arguments', {})
        names = {tool['name'] for tool in catalog}
        response = (call_native(name, arguments) if isinstance(name, str) and name in names
                    else {'error': 'This native agent tool is not available in the current task.'})
        result = {'content': [{'type': 'text', 'text': json.dumps(response.get('result', response), ensure_ascii=False)}],
                  'isError': 'error' in response}
    else:
        return {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': -32601, 'message': 'Method not found'}}
    return {'jsonrpc': '2.0', 'id': request_id, 'result': result}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', required=True)
    args = parser.parse_args(argv)
    path = Path(args.catalog)
    if path.stat().st_size > MAX_LINE_BYTES:
        return 1
    catalog = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(catalog, list) or len(catalog) > len(TOOLS)
            or any(not isinstance(tool, dict) or not isinstance(tool.get('name'), str)
                   or tool['name'] not in TOOLS for tool in catalog)):
        return 1
    for line in iter(lambda: sys.stdin.buffer.readline(MAX_LINE_BYTES + 1), b''):
        if len(line) > MAX_LINE_BYTES:
            return 1
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                return 1
            response = dispatch(request, catalog)
            if response is not None:
                sys.stdout.buffer.write(encode_message(response))
                sys.stdout.buffer.flush()
        except (ValueError, UnicodeError, ProtocolError):
            return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
