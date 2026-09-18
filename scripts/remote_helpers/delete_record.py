"""Delete one registered source task through native Codex, never through SQL/rm.

The selected desktop authenticates before invoking this helper. Its source is a
server-observed catalog identity, not a caller-supplied filesystem path. A private
temporary HOME has no credentials; only record/SQLite storage targets the source.
Native deletion retains descendant, writer-lock and fork-reference checks.
"""
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from uuid import UUID, uuid5

NAMESPACE = UUID('14a0f21b-b529-45fc-bd9e-b07637424fa3')


def read(path):
    if path.is_symlink() or path.stat().st_size > 4*1024*1024:
        raise ValueError('invalid catalog')
    return json.loads(path.read_text(encoding='utf-8'))


def source_home(base, request):
    origin=request['origin'];tid=origin['canonicalThreadId'];sid=origin['sourceStoreId']
    if origin['hostId']!='local' or str(UUID(tid))!=tid or not isinstance(sid,str):
        raise ValueError('invalid origin')
    if str(uuid5(NAMESPACE,'local\0'+sid+'\0'+tid))!=request['projection']:
        raise ValueError('invalid projection')
    data=read(base/'catalog-mixed-sources.json')
    managed=read(base/'catalog-sources.json')
    if data.get('version')!=3 or managed.get('version')!=2:
        raise ValueError('invalid catalog')
    candidates=[s for s in data.get('legacySources',[])+managed.get('sources',[])
                if s.get('sourceStoreId')==sid and s.get('hostId')=='local']
    if len(candidates)!=1:raise ValueError('ambiguous source')
    home=Path(candidates[0]['codexHome'])
    if not home.is_absolute() or not home.is_dir():raise ValueError('invalid home')
    return home.resolve()


def native_delete(executable, home, thread_id, private_parent):
    with tempfile.TemporaryDirectory(prefix='.record-delete-',dir=private_parent) as temporary:
        env={k:v for k,v in os.environ.items() if not k.startswith(('CODEX_','OPENAI_'))}
        env.update(CODEX_HOME=temporary,CODEX_RECORD_HOME=str(home),CODEX_SQLITE_HOME=str(home),
                   CODEX_MANAGER_SHARED_EXECUTION='1')
        child=subprocess.Popen([str(executable),'app-server','--listen','stdio://'],env=env,
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        messages=queue.Queue(maxsize=1024)
        def receive():
            try:
                for line in child.stdout:
                    if len(line)>8*1024*1024:break
                    messages.put(json.loads(line),timeout=1)
            except (ValueError,queue.Full):pass
            finally:
                try:messages.put(None,timeout=1)
                except queue.Full:pass
        reader=threading.Thread(target=receive,daemon=True);reader.start()
        def send(value):
            child.stdin.write((json.dumps(value)+'\n').encode());child.stdin.flush()
        def response(identity):
            deadline=time.monotonic()+45
            while True:
                value=messages.get(timeout=max(.01,deadline-time.monotonic()))
                if value is None:raise ValueError('native closed')
                if value.get('id')==identity:return value
                if time.monotonic()>deadline:raise TimeoutError()
        try:
            send(dict(id=1,method='initialize',params={'clientInfo':{'name':'managed_record_delete','version':'1'},
                'capabilities':{'experimentalApi':True}}))
            if 'error' in response(1):raise ValueError('initialize failed')
            send(dict(method='initialized'))
            # No resume, model request, account login, or source config/auth read.
            send(dict(id=2,method='thread/delete',params={'threadId':thread_id}))
            result=response(2)
            if 'error' in result:
                error=str(result['error'].get('message',''))
                reason='record_busy' if any(w in error.lower() for w in ('writer','lock','busy','owned')) else (
                    'record_referenced' if 'references' in error.lower() else 'native_delete_failed')
                return {'status':'failed','code':reason}
            return {'status':'deleted'}
        finally:
            child.stdin.close()
            try:child.wait(timeout=5)
            except subprocess.TimeoutExpired:child.terminate();child.wait(timeout=5)
            reader.join(timeout=2);child.stdout.close()


def delete(request):
    base=Path.home()/'.local/share/codex-control-center'
    identity=Path('/etc/machine-id').read_text().strip()
    actual=hashlib.sha256((identity+'\\0'+str(os.getuid())+'\\0'+str(Path.home())).encode()).hexdigest()
    if actual!=request['host_identity']:raise ValueError('host identity changed')
    home=source_home(base,request)
    bundle=request['bundle'];digest=request['sha256']
    if not re.fullmatch('[A-Za-z0-9_.-]{1,128}',bundle) or not re.fullmatch('[0-9a-f]{64}',digest):
        raise ValueError('invalid runtime')
    executable=base/'runtime'/bundle/'codex'
    if not executable.is_file():return {'status':'failed','code':'delete_runtime_required'}
    if any(p.is_symlink() for p in (executable,*executable.parents)):raise ValueError('runtime link')
    with executable.open('rb') as stream:
        if hashlib.file_digest(stream,'sha256').hexdigest()!=digest:raise ValueError('runtime hash')
    # Revalidate enrollment immediately before starting the native operation.
    if source_home(base,request)!=home:raise ValueError('source changed')
    return native_delete(executable,home,request['origin']['canonicalThreadId'],base)


if __name__=='__main__':
    try:print(json.dumps(delete(json.load(sys.stdin))))
    except (OSError,ValueError,KeyError,TypeError,queue.Empty,TimeoutError):
        print(json.dumps({'status':'failed','code':'native_delete_failed'}))
