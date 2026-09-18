"""Generated primary provider config + shared task continuation, loopback only."""
import json
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch
from manager_core.providers import ProviderRegistry
from manager_core.external_profile import ExternalProfile
from manager_core.runtime_build import resolve
from test_shared_editing_headless import Client, Fixture, ROOT


def run():
    output=ROOT/'artifacts/results'/('external-profile-'+uuid4().hex[:8]);output.mkdir(parents=True)
    fixture=Fixture();clients=[];checks={};report={'passed':False,'real_model_calls':0}
    try:
        binary=Path(resolve(ROOT)['runtime']);port=fixture.server.server_port
        canonical=output/'original'
        original=Client(binary,canonical,None,'A',port,str(uuid4()),shared_append=True);clients.append(original)
        tid=original.rpc('thread/start',dict(historyMode='paginated',cwd=str(output)))['thread']['id']
        original.turn(tid,'SHARED_ORIGINAL_MESSAGE')
        registry=ProviderRegistry(output)
        saved=registry.save({'name':'External fixture','base_url':'https://fixture.invalid/v1','protocol':'responses'},
            {'wire_model_id':'api-test-model','reasoning_effort':'high'})
        data=registry._read();data['models'][0]['capabilities']['verified']=True
        registry.path.write_text(json.dumps(data))
        home=output/'external';home.mkdir()
        rendered=registry.render_for_host(home,False,[],
            'approval_policy="never"\ncli_auth_credentials_store="ephemeral"\n[features]\nshell_tool=false\n',primary_model_id=saved['model']['id'])
        for relative,body in rendered['files'].items():
            target=home/relative;target.parent.mkdir(parents=True,exist_ok=True);target.write_text(body,encoding='utf-8')
        config=rendered['files']['config.toml'].replace('https://fixture.invalid/v1','http://127.0.0.1:'+str(port)+'/v1')
        adapter=ExternalProfile({'CODEX_MANAGER_PRIMARY_MODEL':json.dumps(rendered['primary'])})
        key_name=registry._env_name(saved['provider'])
        external=Client(binary,home,None,'B',port,str(uuid4()),canonical_home=canonical,config_text=config,
            extra_environment={key_name:'fixture-B'},request_adapter=adapter.request);clients.append(external)
        resumed=external.rpc('thread/resume',dict(threadId=tid,excludeTurns=True,model='gpt-6-astra',modelProvider='openai'))
        checks['same_canonical_task']=resumed['thread']['id']==tid
        checks['selected_primary_provider']=resumed['modelProvider']==rendered['primary']['model_provider']
        external.turn(tid,'SHARED_EXTERNAL_MESSAGE')
        api_calls=[c for c in fixture.calls if c['account']=='B']
        checks['api_model_received_original_history']=bool(api_calls) and all(c['model']=='api-test-model' for c in api_calls) and 'SHARED_ORIGINAL_MESSAGE' in json.dumps(api_calls[-1]['input'])
        # Original stays running, reads API profile writes from the same store.
        latest=original.rpc('thread/read',dict(threadId=tid,includeTurns=True))
        checks['original_reads_external_reply']='SHARED_EXTERNAL_MESSAGE' in json.dumps(latest)
        original.close();clients.remove(original)
        original=Client(binary,canonical,None,'A',port,str(uuid4()),shared_append=True);clients.append(original)
        # The desktop adapter explicitly restores this profile's configured provider.
        cold=original.rpc('thread/resume',dict(threadId=tid,excludeTurns=True,modelProvider='fixture',model='gpt-5.5'))
        checks['cold_original_preserves_native_provider']=cold['modelProvider']=='fixture'
        original.turn(tid,'BACK_TO_ORIGINAL')
        checks['original_uses_own_model']=fixture.calls[-1]['account']=='A' and fixture.calls[-1]['model']=='gpt-5.5'
        checks['no_chatgpt_credentials']=not (home/'auth.json').exists()
        checks['only_shared_rollout']=not list((home/'sessions').rglob('*.jsonl'))
        report.update(passed=all(checks.values()),checks=checks)
    except Exception as e:report.update(error=str(e),checks=checks)
    finally:
        for c in reversed(clients):c.close()
        fixture.close();(output/'report.json').write_text(json.dumps(report,indent=2))
        print(output);print(json.dumps(report),flush=True)
    return report['passed']

if __name__=='__main__':raise SystemExit(0 if run() else 1)
