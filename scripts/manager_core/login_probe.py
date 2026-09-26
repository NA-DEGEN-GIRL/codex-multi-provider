"""Quota-only verification with an access token held in a disposable process."""
import json,os,queue,shutil,stat,subprocess,sys,threading,time
from pathlib import Path
from uuid import UUID,uuid4

from .desktop_publication import _hash

_PROBES='work/control-center/auth-probes'
_ONEXC=sys.version_info>=(3,12)  # The service still accepts Python 3.11 (onerror only).


def _probe_home(root,directory):
    """Only <root>/work/control-center/auth-probes/<uuid> as a real directory."""
    base=Path(root)/_PROBES;directory=Path(directory)
    try:
        if directory.parent!=base or str(UUID(directory.name))!=directory.name:return None
        info=directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info,'st_file_attributes',0)&stat.FILE_ATTRIBUTE_REPARSE_POINT:
            return None  # Never follow a link or junction out of the probe area.
        if directory.resolve().parent!=base.resolve():return None
    except (OSError,ValueError):
        return None
    return info


def _writable(function,path,error):
    # Git in the app-server leaves read-only pack files. Clear only that
    # attribute on a plain file; everything else is left for a later purge.
    info=os.lstat(path)
    if (not isinstance(error,PermissionError) or not getattr(info,'st_file_attributes',0)&stat.FILE_ATTRIBUTE_READONLY
            or getattr(info,'st_file_attributes',0)&stat.FILE_ATTRIBUTE_REPARSE_POINT or stat.S_ISLNK(info.st_mode)):
        raise error
    os.chmod(path,stat.S_IWRITE)
    function(path)


def _discard(root,directory,*,attempts=3,delay=.2):
    """Remove one exited probe home. Never raises; locked homes stay for the purge."""
    for attempt in range(attempts):
        if _probe_home(root,directory) is None:return False
        try:
            # rmtree refuses a top-level link and removes inner junctions
            # without entering them.
            if _ONEXC:shutil.rmtree(directory,onexc=_writable)
            else:shutil.rmtree(directory,onerror=lambda function,path,info:_writable(function,path,info[1]))
            return True
        except OSError:
            # sqlite/WAL or a scanner can hold a file briefly after exit.
            if attempt+1<attempts:time.sleep(delay*(attempt+1))
        except Exception:
            return False  # Not transient; verify() calls this from finally.
    return False


def purge_stale_probes(root,older_than=3600,*,pause=.05):
    """Remove abandoned probe homes one at a time; returns how many were removed.

    Only UUID-named real directories older than older_than seconds qualify, so
    a live probe (at most 25 s) is never touched. Run it off request paths.
    The thread keeps normal priority: it runs Python and holds the GIL, so a
    lowered priority could stall backend request threads behind it.
    """
    base=Path(root)/_PROBES
    try:
        names=[entry.name for entry in os.scandir(base)]
    except OSError:
        return 0
    removed=0
    for name in names:
        directory=base/name
        info=_probe_home(root,directory)
        if info is None:continue
        newest=max(info.st_mtime,getattr(info,'st_birthtime',info.st_ctime))
        if time.time()-newest<older_than:continue
        removed+=_discard(root,directory,attempts=1)
        time.sleep(pause)  # Yield disk and GIL between directories.
    return removed


def purge_in_child(root,*,timeout=6*3600):
    """Run the purge in a low-priority child process and wait for it.

    Thousands of rmtree walks hold the GIL between syscalls; in the backend they
    slowed every state request. The child gets no manager credentials, and its
    stdin pipe ends when this process exits, so a full exit stops it too.
    """
    env={key:value for key,value in os.environ.items() if not key.upper().startswith('CODEX_MANAGER_')}
    process=subprocess.Popen([sys.executable,'-X','utf8','-m','manager_core.login_probe','--purge',str(Path(root).resolve())],
                             cwd=Path(__file__).resolve().parents[1],env=env,stdin=subprocess.PIPE,
                             stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                             creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    try:return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill();return process.wait()
    finally:process.stdin.close()


def _purge_main(root):
    def orphaned():
        sys.stdin.buffer.read()  # EOF once the backend that started us is gone
        os._exit(0)
    threading.Thread(target=orphaned,daemon=True).start()
    if os.name=='nt':
        # Its own process, so lowering CPU and I/O priority cannot stall backend threads.
        import ctypes
        from ctypes import wintypes
        kernel32=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel32.GetCurrentProcess.restype=wintypes.HANDLE
        kernel32.SetPriorityClass.argtypes=[wintypes.HANDLE,wintypes.DWORD]
        kernel32.SetPriorityClass(kernel32.GetCurrentProcess(),0x00100000)  # PROCESS_MODE_BACKGROUND_BEGIN
    return purge_stale_probes(root)


if __name__=='__main__' and sys.argv[1:2]==['--purge'] and len(sys.argv)==3:
    _purge_main(Path(sys.argv[2]))
    # The stdin watcher still blocks in read(); normal interpreter shutdown
    # would abort on that buffered reader, so leave without finalization.
    os._exit(0)


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
    directory=Path(root)/_PROBES/str(uuid4())
    directory.mkdir(parents=True)
    try:
        (directory/'config.toml').write_text('cli_auth_credentials_store = "ephemeral"\n',encoding='utf-8')
        env={key:value for key,value in os.environ.items() if not key.upper().startswith(('CODEX_','OPENAI_','AZURE_OPENAI_','CHATGPT_')) and key.upper()!='ELECTRON_RUN_AS_NODE'}
        env['CODEX_HOME']=str(directory)
        process=subprocess.Popen([str(runtime),'app-server','--listen','stdio://'],cwd=directory,env=env,
                                 stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                                 creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    except BaseException:
        _discard(root,directory);raise
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
        try:
            try:process.stdin.close()
            except OSError:pass
            try:process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:process.wait(timeout=4)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=4)
            reader.join(timeout=1);process.stdout.close()
        finally:
            # The auth.json check above has run. This throwaway CODEX_HOME holds
            # only app-server state (sqlite, logs, a plugin clone); 15-27 MB each
            # accumulated forever. A process that did not exit keeps its home.
            if process.poll() is not None:_discard(root,directory)
