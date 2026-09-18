"""Verify generated model settings against the native runtime using loopback only."""
import argparse
import json
from pathlib import Path
from uuid import uuid4
from manager_core.providers import ProviderRegistry
from manager_core.external_profile import ExternalProfile
from test_shared_editing_headless import Client, Fixture, ROOT


def run(binary):
    output=ROOT/'artifacts/results'/('model-settings-'+uuid4().hex[:8]);output.mkdir(parents=True)
    fixture=Fixture();client=None;checks={};report={'passed':False,'real_model_calls':0}
    try:
        registry=ProviderRegistry(output)
        model=registry.save({'name':'Fixture','base_url':'https://fixture.invalid/v1','protocol':'responses'},
            {'wire_model_id':'deepseek-flash','reasoning_effort':'max','capabilities':{'verified':True}})['model']
        home=output/'profile';home.mkdir()
        rendered=registry.render_for_host(home,False,[],
            'approval_policy="never"\ncli_auth_credentials_store="ephemeral"\n[features]\nshell_tool=false\n',
            primary_model_id=model['id'],primary_settings={'context_window':1048576,'auto_compact_percent':85,'reasoning_effort':'max'})
        for relative,content in rendered['files'].items():
            path=home/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(content,encoding='utf-8')
        config=rendered['files']['config.toml'].replace('https://fixture.invalid/v1','http://127.0.0.1:'+str(fixture.server.server_port)+'/v1')
        provider=registry._read()['providers'][0]
        adapter=ExternalProfile({'CODEX_MANAGER_PRIMARY_MODEL':json.dumps(rendered['primary'])})
        client=Client(binary,home,None,'B',fixture.server.server_port,str(uuid4()),config_text=config,
            extra_environment={registry._env_name(provider):'fixture-B'},request_adapter=adapter.request)
        actual=client.rpc('config/read',{'includeLayers':False})['config']
        checks['context_1m']=actual['model_context_window']==1048576
        checks['compact_85_percent']=actual['model_auto_compact_token_limit']==891289
        models=client.rpc('model/list',{})['data']
        selected=next(m for m in models if m['model']=='deepseek-flash')
        checks['picker_efforts']=[m['reasoningEffort'] for m in selected['supportedReasoningEfforts']]==['none','low','high','max']
        checks['default_max']=selected['defaultReasoningEffort']=='max'
        tid=client.rpc('thread/start',{'cwd':str(output)})['thread']['id']
        client.turn(tid,'MAX_SETTING')
        checks['max_on_wire']=fixture.calls[-1]['reasoning']['effort']=='max'
        client.rpc('thread/settings/update',{'threadId':tid,'model':'deepseek-flash','effort':'medium'})
        client.turn(tid,'CHANGED_TO_HIGH')
        checks['medium_maps_high_and_persists']=fixture.calls[-1]['reasoning']['effort']=='high'
        client.rpc('thread/settings/update',{'threadId':tid,'model':'deepseek-flash','effort':'low'})
        client.turn(tid,'CHANGED_TO_LOW')
        checks['low_on_wire']=fixture.calls[-1]['reasoning']['effort']=='low'
        checks['same_model_all_calls']=all(c['model']=='deepseek-flash' for c in fixture.calls)
        report.update(passed=all(checks.values()),checks=checks)
    except Exception as error:report.update(error=str(error),checks=checks)
    finally:
        if client:client.close()
        fixture.close();(output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps({'report':str(output/'report.json'),**report}))
    return report['passed']


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--runtime',required=True,type=Path);args=parser.parse_args()
    raise SystemExit(0 if run(args.runtime) else 1)
