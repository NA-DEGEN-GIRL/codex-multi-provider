"""Check the production login launch environment with an empty disposable HOME.

No browser login, user credentials, model requests, or existing windows are used.
"""
import json
import queue
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from manager_core.instances import Instances
from manager_core.store import Store


def main():
    results=ROOT/'artifacts/results'
    results.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='login-runtime-',dir=results) as folder:
        fixture=Path(folder).resolve()
        assert fixture.is_relative_to(results.resolve())
        store=Store(fixture)
        profile=store.add_profile('login-fixture')
        profile.update(auth_mode='native',runtime_channel='packaged')
        home=Path(profile['home']);home.mkdir(parents=True,exist_ok=True)
        (home/'config.toml').write_text('cli_auth_credentials_store = "file"\n',encoding='utf-8')
        env=Instances(ROOT,store,None).environment(profile)
        runtime=Path(env['CODEX_CLI_PATH'])
        assert runtime.is_relative_to(ROOT/'artifacts/login-runtime')
        assert 'CODEX_MANAGER_AUTH_SOURCE' not in env
        assert 'CODEX_MANAGER_EXPECTED_ACCOUNT_FINGERPRINT' not in env
        process=subprocess.Popen([str(runtime),'app-server','--listen','stdio://'],env=env,cwd=fixture,
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        messages=queue.Queue()
        def receive():
            for line in process.stdout:
                try:messages.put(json.loads(line))
                except ValueError:messages.put({'invalid':True})
            messages.put(None)
        reader=threading.Thread(target=receive,daemon=True);reader.start()
        def request(identity,method,params):
            process.stdin.write((json.dumps(dict(id=identity,method=method,params=params))+'\n').encode())
            process.stdin.flush()
            for _ in range(50):
                message=messages.get(timeout=15)
                if message is None:raise RuntimeError('Login runtime exited during initialization')
                if message.get('id')==identity:
                    if 'error' in message:raise RuntimeError('Login runtime RPC failed: '+method)
                    return message.get('result')
            raise RuntimeError('Login runtime sent too many unrelated messages')
        try:
            request(1,'initialize',{'clientInfo':{'name':'manager_login_fixture','version':'1'},
                'capabilities':{'experimentalApi':True}})
            account=request(2,'account/read',{'refreshToken':False})
            assert account.get('account') is None,'Disposable login HOME must be signed out'
            assert process.poll() is None
            report={'passed':True,'runtime':str(runtime),'isolated_home':True,
                'initialize':'passed','account_read':'signed_out','borrowed_account_guard':False}
        finally:
            process.stdin.close()
            try:process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill();process.wait(timeout=5)
            reader.join(timeout=2);process.stdout.close()
    (results/'login-runtime-41.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report))


if __name__=='__main__':main()
