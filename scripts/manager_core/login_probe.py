"""Quota-only verification with an access token held in a disposable process."""
import json,os,queue,shutil,subprocess,threading,time
from pathlib import Path
from uuid import uuid4

from .desktop_publication import _hash


def verification_runtime(root,app):
    # MSIX binaries can be readable but not directly executable outside the
    # packaged app. Stage exactly the official CLI bytes for both private login
    # windows and quota-only probes. Credentials stay in their own CODEX_HOME.
    # Digests come from desktop_publication's content-stamp cache, so unchanged
    # source and staged copies are not re-read. The stamp covers file identity,
    # size, mtime and NTFS ChangeTime: an edit that restores size and mtime is
    # still re-hashed, an unavailable stamp disables reuse, and a stamp that
    # changes mid-hash raises OSError instead of publishing a stale digest.
    source=Path(app['InstallLocation'])/'app/resources/codex.exe'
    destination=Path(root)/'artifacts/login-runtime'/app['Version']/'codex.exe'
    expected=_hash(source)
    if not destination.exists():
        destination.parent.mkdir(parents=True,exist_ok=True)
        temporary=destination.with_name('codex.'+uuid4().hex+'.tmp')
        try:
            shutil.copyfile(source,temporary)
            if expected!=_hash(temporary):raise RuntimeError('설치된 CLI 검사본의 무결성을 확인하지 못했습니다.')
            os.replace(temporary,destination)
        finally:
            if temporary.exists():temporary.unlink()
    if expected!=_hash(destination):raise RuntimeError('설치된 CLI 검사본의 무결성을 확인하지 못했습니다.')
    return destination


def verify(root,runtime,tokens,*,timeout=25):
    directory=Path(root)/'work/control-center/auth-probes'/str(uuid4())
    directory.mkdir(parents=True)
    (directory/'config.toml').write_text('cli_auth_credentials_store = "ephemeral"\n',encoding='utf-8')
    env={key:value for key,value in os.environ.items() if not key.upper().startswith(('CODEX_','OPENAI_','AZURE_OPENAI_','CHATGPT_')) and key.upper()!='ELECTRON_RUN_AS_NODE'}
    env['CODEX_HOME']=str(directory)
    process=subprocess.Popen([str(runtime),'app-server','--listen','stdio://'],cwd=directory,env=env,
                             stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                             creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    messages=queue.Queue(maxsize=128);deadline=time.monotonic()+timeout
    def receive():
        try:
            while True:
                line=process.stdout.readline(1024*1024+1)
                if not line:break
                if len(line)>1024*1024:break
                messages.put_nowait(json.loads(line))
        except (OSError,ValueError,queue.Full):pass
        try:messages.put_nowait(None)
        except queue.Full:pass
    reader=threading.Thread(target=receive,daemon=True);reader.start()
    def send(value):
        process.stdin.write(json.dumps(value,separators=(',',':')).encode('utf-8')+b'\n');process.stdin.flush()
    def request(identity,method,params):
        send(dict(id=identity,method=method,params=params))
        while True:
            remaining=deadline-time.monotonic()
            if remaining<=0:raise RuntimeError('로그인 연결 확인 시간이 초과되었습니다.')
            try:value=messages.get(timeout=remaining)
            except queue.Empty:raise RuntimeError('로그인 연결 확인 시간이 초과되었습니다.') from None
            if value is None:raise RuntimeError('로그인 연결 확인 프로세스가 종료되었습니다.')
            if value.get('id')==identity and 'method' not in value:
                if 'error' in value:raise RuntimeError(f'로그인 연결 확인 단계 실패: {method} (RPC {value["error"].get("code")})')
                return value.get('result')
            if 'id' in value and 'method' in value:
                send({'id':value['id'],'error':{'code':-32601,'message':'Login verification does not execute tools.'}})
    try:
        request(1,'initialize',{'clientInfo':{'name':'codex_manager_login_check','version':'1.0'},'capabilities':{'experimentalApi':True}})
        send({'method':'initialized'})
        request(2,'account/login/start',tokens.login_params())
        account=request(3,'account/read',{'refreshToken':False})
        if (account.get('account') or {}).get('type')!='chatgpt':raise RuntimeError('ChatGPT 계정 연결을 확인하지 못했습니다.')
        limits=request(4,'account/rateLimits/read',{})
        if not isinstance(limits,dict) or not (limits.get('rateLimits') or limits.get('rateLimitsByLimitId')):
            raise RuntimeError('계정의 사용량 서버 응답을 확인하지 못했습니다.')
        if (directory/'auth.json').exists():raise RuntimeError('연결 검사에서 임시 인증 정보가 파일로 저장되었습니다.')
        from .native_usage import normalize
        return dict(server_verified=True,account_type='chatgpt',quota_read=True,model_turns_started=0,
                    probe_auth_file_created=False,usage=normalize(limits))
    finally:
        try:process.stdin.close()
        except OSError:pass
        try:process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:process.wait(timeout=4)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=4)
        reader.join(timeout=1);process.stdout.close()
