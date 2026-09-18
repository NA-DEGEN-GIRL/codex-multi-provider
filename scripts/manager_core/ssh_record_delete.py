"""Asynchronous, explicit catalog deletion; normal RPC input never waits on SSH."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shlex

from .proxy_auth import AuthProxyResult
from .release_code import script_path
from .remote import RemoteManager, RemoteError
from .source_catalog import projection_id


ERRORS={
    'remote_disk_full':'SSH 서버 공간이 부족해 삭제용 런타임을 준비하지 못했습니다. 대화는 삭제하지 않았습니다.',
    'record_busy':'이 작업을 사용 중인 연결이 있습니다. 해당 작업을 닫은 뒤 삭제하세요.',
    'record_referenced':'다른 작업이 이 대화의 기록을 참조하고 있어 삭제할 수 없습니다.',
    'delete_runtime_required':'이 SSH 서버에 공통 기록 삭제를 지원하는 런타임 준비가 필요합니다. SSH 연결 준비에서 업데이트하세요.',
    'native_delete_failed':'SSH 원본 기록 삭제를 완료하지 못했습니다. 연결 상태와 해당 작업의 사용 여부를 확인하세요.',
}


def delete_remote(root, profile_id, generation, alias, revision, projection, origin):
    from .store import Store
    profile=Store(root).profile(profile_id)
    if profile.get('generation')!=generation:raise ValueError('stale profile')
    binding=next(b for b in profile['remote_bindings'] if b['alias']==alias and b['revision']==revision)
    if projection_id(origin['sourceStoreId'],origin['canonicalThreadId'])!=projection:
        raise ValueError('invalid projection')
    remote=RemoteManager(root)
    artifact=remote._artifact('linux','x86_64')
    executable=artifact['directory']/'codex'
    # This adapter needs canonical storage; older read-only runtimes stay intact.
    import mmap
    with executable.open('rb') as stream, mmap.mmap(stream.fileno(),0,access=mmap.ACCESS_READ) as data:
        if data.find(b'CODEX_RECORD_HOME')<0 or data.find(b'CODEX_MANAGER_SHARED_EXECUTION')<0:
            return {'status':'failed','code':'delete_runtime_required'}
    payload=dict(origin=origin,projection=projection,bundle=artifact['bundle_id'],
        sha256=next(f['sha256'] for f in artifact['files'] if f['path']=='codex'),host_identity=binding['host_identity'])
    source=script_path(root,'scripts/remote_helpers/delete_record.py').read_text(encoding='utf-8')
    command=shlex.join([binding['remote_python'],'-c',source])
    result=remote._run(alias,command,input=json.dumps(payload).encode(),timeout=65)
    if result.returncode:raise ValueError('transport failed')
    value=json.loads(result.stdout)
    if value.get('code')=='delete_runtime_required':
        # The helper guarantees it has not attempted deletion in this case.
        # Stage a compatible runtime without replacing any running connection.
        models=profile['policy']['model_ids'] if profile['policy']['enabled'] else []
        from .model_settings import render_options
        prepared=remote.prepare(alias,profile_id,profile['home'],models,**render_options(profile))
        if not prepared.get('prepared') or prepared.get('host_identity')!=binding['host_identity']:
            raise ValueError('remote preparation changed')
        result=remote._run(alias,command,input=json.dumps(payload).encode(),timeout=65)
        if result.returncode:raise ValueError('transport failed')
        value=json.loads(result.stdout)
    return value


class RecordDelete:
    def __init__(self, origins, execute):
        self.origins,self.execute=origins,execute
        self.worker=ThreadPoolExecutor(max_workers=1,thread_name_prefix='ssh-record-delete')
        self.pending={}

    def submit(self,message):
        if message.get('method')!='thread/delete' or type(message.get('id')) not in (str,int):return False
        params=message.get('params')
        if not isinstance(params,dict) or set(params)!={'threadId'}:return False
        try:origin=self.origins.lookup(params['threadId'])
        except (ValueError,TypeError,AttributeError):return False
        if origin is None:return False
        if len(self.pending)>=8 or message['id'] in self.pending:return False
        tid=params['threadId']
        self.pending[message['id']]=(tid,self.worker.submit(self.execute,tid,origin))
        return True

    def poll(self):
        result=AuthProxyResult()
        for identity,(tid,future) in list(self.pending.items()):
            if not future.done():continue
            del self.pending[identity]
            try:value=future.result()
            except RemoteError as error:value={'code':error.code}
            except Exception:value={'code':'native_delete_failed'}
            if value.get('status')=='deleted':
                result.frontend.extend([{'id':identity,'result':{}},
                    {'method':'thread/deleted','params':{'threadId':tid}}])
            else:result.frontend.append({'id':identity,'error':{'code':-32600,
                'message':ERRORS.get(value.get('code'),ERRORS['native_delete_failed'])}})
        return result

    def close(self):
        self.worker.shutdown(wait=False,cancel_futures=True)
