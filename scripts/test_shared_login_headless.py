"""New-account/relogin history through the real managed runtime and stdio proxy.

All history and credentials are disposable fixtures. Model traffic is loopback.
OAuth is only started/cancelled; no browser or user account is used.
"""
import base64
import json
from pathlib import Path
import sqlite3
import sys
import time
import re
from uuid import uuid4

from test_shared_editing_headless import Client,Fixture,ROOT
from manager_core.proxy_auth import account_fingerprint
from manager_core.runtime_build import resolve


def run():
    binary=Path(resolve(ROOT)['runtime'])
    output=ROOT/'artifacts/results'/('shared-login-'+uuid4().hex[:8]);output.mkdir(parents=True)
    shared=output/'original'
    fixture=Fixture();clients=[];checks={}
    report={'status':'FAIL','real_model_calls':0,'user_windows_changed':False}
    try:
        seed=Client(binary,shared,None,'A',fixture.server.server_port,str(uuid4()),shared_append=True)
        clients.append(seed)
        project=seed.rpc('project/create',{'name':'login fixture project','roots':[{'path':str(output)}],
            'idempotencyKey':str(uuid4())})['project']['id']
        tid=seed.rpc('thread/start',{'historyMode':'paginated','cwd':str(output)})['thread']['id']
        seed.turn(tid,'COMMON_HISTORY_BEFORE_NEW_ACCOUNT')
        seed.close();clients=[]
        with sqlite3.connect(shared/'state_5.sqlite') as db:
            db.execute('UPDATE threads SET project_id=? WHERE id=?',(project,tid))
        source_config=(shared/'config.toml').read_text(encoding='utf-8')
        for name,registered in [('new-account',False),('relogin',True)]:
            profile_id=str(uuid4());generation=str(uuid4());home=output/name
            status=output/'work/control-center/instances'/profile_id/'runtime-state.json'
            env={'CODEX_MANAGER_ROOT':str(output),'CODEX_MANAGER_PROFILE_ID':profile_id,
                'CODEX_MANAGER_GENERATION':generation,'CODEX_MANAGER_OBSERVER_PATH':str(status),
                'CODEX_MANAGER_REAL_RUNTIME':str(binary),'CODEX_MANAGER_NATIVE_LOGIN':'1'}
            if registered:env['CODEX_MANAGER_EXPECTED_ACCOUNT_FINGERPRINT']=account_fingerprint('fixture-account')
            client=Client(binary,home,None,'B',fixture.server.server_port,profile_id,canonical_home=shared,
                config_text=source_config.replace('cli_auth_credentials_store="ephemeral"','cli_auth_credentials_store="file"'),
                extra_environment=env,launch_command=[sys.executable,str(ROOT/'scripts/manager_core/runtime_proxy.py')])
            clients.append(client);pid=client.proc.pid
            checks[name+'_history_before_login']=any(t['id']==tid for t in client.rpc('thread/list',{'limit':20,'modelProviders':[]})['data'])
            checks[name+'_project_membership']=any(t['id']==tid for t in client.rpc('thread/list',{'projectId':project,'modelProviders':[]})['data'])
            checks[name+'_full_body_before_login']='COMMON_HISTORY_BEFORE_NEW_ACCOUNT' in json.dumps(
                client.rpc('thread/read',{'threadId':tid,'includeTurns':True}))
            login=client.rpc('account/login/start',{'type':'chatgpt'})
            checks[name+'_oauth_available']=bool(login.get('authUrl') and login.get('loginId'))
            client.rpc('account/login/cancel',{'loginId':login['loginId']})
            # The browser callback is not automated. Simulate only the persisted
            # identity for guard/record-continuity testing; inference stays local.
            payload=base64.urlsafe_b64encode(json.dumps({'exp':time.time()+3600,
                'https://api.openai.com/auth':{'chatgpt_account_id':'fixture-account'}}).encode()).decode().rstrip('=')
            jwt='fixture.'+payload+'.signature'
            (home/'auth.json').write_text(json.dumps({'tokens':{'account_id':'fixture-account',
                'access_token':jwt,'id_token':jwt,'refresh_token':'fixture-not-a-credential'}}))
            client.rpc('thread/resume',{'threadId':tid,'excludeTurns':True})
            client.turn(tid,'SAME_PROCESS_AFTER_'+name)
            checks[name+'_same_process_after_login']=client.proc.pid==pid and client.proc.poll() is None
            checks[name+'_one_history_store']=not list((home/'sessions').rglob('*.jsonl'))
            deadline=time.monotonic()+8
            binding={}
            while time.monotonic()<deadline:
                try:binding=json.loads(status.read_text()).get('native_account',{})
                except (OSError,ValueError):pass
                if binding.get('matches'):break
                time.sleep(.1)
            checks[name+'_bound_identity_published']=binding.get('account_fingerprint')==account_fingerprint('fixture-account') and binding.get('matches') is True
            client.close();clients=[]
        reader=Client(binary,shared,None,'A',fixture.server.server_port,str(uuid4()),shared_append=True)
        clients.append(reader)
        body=json.dumps(reader.rpc('thread/read',{'threadId':tid,'includeTurns':True}))
        checks['original_reads_both_profile_updates']=all('SAME_PROCESS_AFTER_'+name in body for name in ('new-account','relogin'))
        checks['original_credentials_untouched']=not (shared/'auth.json').exists()
        checks['only_loopback_model_calls']=len(fixture.calls)==3
        report.update(status='PASS' if all(checks.values()) else 'FAIL',checks=checks)
    except Exception as error:
        # Never emit auth URLs or RPC payloads from an OAuth failure.
        detail=re.sub(r'https?://[^\s\"\']+','[url]',str(error))
        report.update(error_type=type(error).__name__,error=detail[:800],stage_checks=checks)
    finally:
        for client in clients:client.close()
        fixture.close()
        (output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps({'report':str(output/'report.json'),**report}))
    return report['status']=='PASS'


if __name__=='__main__':raise SystemExit(0 if run() else 1)
