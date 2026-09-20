"""Small SSH version RPC. Stock mutations always require explicit confirmation."""
import json
import re
import shlex
from .release_code import script_path
from .remote import RemoteError, login_shell_command

BOOTSTRAP = '''import json,sys,types
payload=json.loads(sys.stdin.buffer.read(131073))
module=types.ModuleType('stock_versions')
exec(compile(payload['source'],'<stock-versions>','exec'),module.__dict__)
try:
 result=module.dispatch(payload['request'],payload['source'])
 print(json.dumps({'ok':True,'result':result}))
except Exception as error:
 code=str(error)
 allowed={'stock_update_requires_confirmation','stock_update_requires_current_observation',
          'stock_versions_changed_refresh_required','stock_update_helper_changed'}
 print(json.dumps({'ok':False,'code':code if code in allowed else 'stock_version_operation_failed'}))
'''


def _request(root, remote, alias, request):
    alias = remote._alias(alias)
    source = script_path(root, 'scripts/remote_helpers/stock_versions.py').read_text(encoding='utf-8')
    payload = json.dumps(dict(source=source, request=request)).encode()
    if len(payload) > 131072:
        raise ValueError('SSH version helper exceeds its size limit')
    command = login_shell_command('exec python3 -c ' + shlex.quote(BOOTSTRAP))
    response = remote._run(alias, command, input=payload, timeout=100)
    try:
        if response.returncode or len(response.stdout) > 65536:
            raise ValueError()
        # Login shells may print banners; only accept exactly one protocol object.
        frames = [json.loads(line) for line in response.stdout.decode('utf-8').splitlines()
                  if line.startswith('{')]
        if len(frames) != 1 or frames[0].get('ok') is not True:
            code = frames[0].get('code') if len(frames) == 1 else None
            if code == 'stock_versions_changed_refresh_required':
                raise RemoteError(code, 'SSH 기본 Codex 상태가 바뀌었습니다. 다시 확인한 뒤 적용하세요.')
            raise ValueError()
        result = frames[0]['result']
        if not isinstance(result, dict) or result.get('safe_auto_update') is not False:
            raise ValueError()
        return result
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise RemoteError('stock_version_operation_failed', 'SSH 기본 Codex 상태를 확인하지 못했습니다. 기존 작업은 유지합니다.') from None


def probe(root, remote, alias):
    return _request(root, remote, alias, dict(operation='inspect'))


def update(root, remote, alias, observation_id, confirmed):
    if confirmed is not True or not isinstance(observation_id, str) or not re.fullmatch('[0-9a-f]{64}', observation_id):
        raise ValueError('기본 Codex 작업 종료 확인과 최신 버전 확인이 필요합니다.')
    return _request(root, remote, alias, dict(operation='update', confirmed=True, observation_id=observation_id))
