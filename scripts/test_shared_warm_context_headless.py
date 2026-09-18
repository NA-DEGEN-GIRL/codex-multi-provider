"""Actual app-server A -> B -> A context exchange; loopback responses only."""
import argparse
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from test_shared_editing_headless import Client, Fixture, ROOT


def run(binary):
    binary=Path(binary).resolve(strict=True)
    output=ROOT/'artifacts/results'/('shared-warm-'+uuid4().hex[:8]);output.mkdir(parents=True)
    fixture=Fixture();clients=[];checks={};report={'status':'FAIL','real_model_calls':0}
    try:
        original=output/'original';catalog=output/'catalog.json'
        a=Client(binary,original,None,'A',fixture.server.server_port,str(uuid4()),shared_append=True);clients.append(a)
        tid=a.rpc('thread/start',dict(historyMode='paginated',cwd=str(output)))['thread']['id']
        a.turn(tid,'SEED_CONTEXT')
        rollout=next(p for p in (original/'sessions').rglob('*.jsonl') if p.name.endswith(tid+'.jsonl'))
        catalog.write_text(json.dumps(dict(version=1,revision='warm-fixture',hostId='local',entries=[dict(
            projectionThreadId=str(uuid4()),threadId=tid,hostId='local',sourceStoreId='original:fixture',
            codexHome=str(original),rolloutPath=str(rollout))])))
        b=Client(binary,output/'profile-b',catalog,'B',fixture.server.server_port,str(uuid4()));clients.append(b)
        b.rpc('thread/resume',dict(threadId=tid,excludeTurns=True))
        a.turn(tid,'UNIQUE_A_AFTER_B_OPENED')
        b.turn(tid,'UNIQUE_B_AFTER_A')
        checks['managed_request_contains_original_new_message']='UNIQUE_A_AFTER_B_OPENED' in json.dumps(fixture.calls[-1]['input'])
        a.turn(tid,'UNIQUE_A_RETURN')
        checks['original_request_contains_managed_message']='UNIQUE_B_AFTER_A' in json.dumps(fixture.calls[-1]['input'])
        a.turn(tid,'UNIQUE_A_CONTINUE')
        checks['ordinary_followup_keeps_context']='UNIQUE_A_RETURN' in json.dumps(fixture.calls[-1]['input'])
        checks['accounts_stay_selected']=[call['account'] for call in fixture.calls]==['A','A','B','A','A']
        checks['both_processes_stay_alive']=a.proc.poll() is None and b.proc.poll() is None
        for client in clients:client.close()
        clients=[]
        rows=[json.loads(line) for line in rollout.read_text(encoding='utf8').splitlines()]
        ordinals=[row['ordinal'] for row in rows]
        checks['ordinals_strictly_increase']=all(x<y for x,y in zip(ordinals,ordinals[1:]))
        report.update(status='PASS' if all(checks.values()) else 'FAIL',checks=checks)
    except Exception as error:
        report.update(error=str(error),checks=checks)
    finally:
        for client in clients:client.close()
        fixture.close()
        with binary.open('rb') as stream:report['runtime_sha256']=hashlib.file_digest(stream,'sha256').hexdigest()
        (output/'report.json').write_text(json.dumps(report,indent=2))
        print(json.dumps({'report':str(output/'report.json'),**report}),flush=True)
    return report['status']=='PASS'


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--runtime',type=Path,required=True)
    raise SystemExit(0 if run(parser.parse_args().runtime) else 1)
