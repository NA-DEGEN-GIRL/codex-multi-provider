"""Cold import + original/02/03/04 against one real native store; loopback only."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch
from uuid import uuid4

from test_shared_editing_headless import Client, Fixture, ROOT
from manager_core.canonical_storage import migrate
from manager_core.store import Store


def run(binary):
    binary = Path(binary).resolve(strict=True)
    output = ROOT/'artifacts/results'/('canonical-store-'+uuid4().hex[:8]); output.mkdir(parents=True)
    source, old = output/'original', output/'old-profile'
    fixture = Fixture(); clients = []; checks = {}
    report = dict(status='FAIL',real_model_calls=0,gui_used=False,validation_kind='canonical_storage_v1')
    def client(home, alias, **kwargs):
        instance = Client(binary,home,None,alias,fixture.server.server_port,str(uuid4()),**kwargs)
        clients.append(instance); return instance
    try:
        seed = client(source,'A',shared_append=True)
        project = seed.rpc('project/create',dict(name='common project',roots=[dict(path=str(output))],
                                               idempotencyKey=str(uuid4())))['project']['id']
        tid = seed.rpc('thread/start',dict(historyMode='paginated',cwd=str(output)))['thread']['id']
        seed.turn(tid,'ORIGINAL_INITIAL_CONTEXT')
        other = client(old,'B')
        legacy = other.rpc('thread/start',dict(historyMode='paginated',cwd=str(output)))['thread']['id']
        other.turn(legacy,'OLD_PROFILE_HISTORY_TO_KEEP')
        for c in clients: c.close()
        clients=[]
        with closing(sqlite3.connect(source/'state_5.sqlite')) as db, db:
            db.execute('UPDATE threads SET project_id=? WHERE id=?',(project,tid))
        store = Store(output)
        store.mutate(lambda data: data.update(sources=[dict(id='original:local',home=str(source),host_id='local',alias='original'),
            dict(id='old-profile',home=str(old),host_id='local',alias='old')]))
        with patch('manager_core.canonical_storage.require_closed'):
            report['migration'] = migrate(output,source)
        a=client(source,'A',shared_append=True)
        profiles=[client(output/alias,account,canonical_home=source) for alias,account in zip(('02','03','04'),'BCD')]
        ids = {tid,legacy}
        listings=[c.rpc('thread/list',dict(limit=100))['data'] for c in [a,*profiles]]
        report['listed_ids']=[[t['id'] for t in rows] for rows in listings]
        checks['original_and_three_profiles_same_unique_ids']=all({t['id'] for t in rows}==ids and len(rows)==2 for rows in listings)
        checks['project_membership_shared']=all([t['id'] for t in c.rpc('thread/list',dict(projectId=project))['data']]==[tid] for c in profiles)
        checks['old_profile_body_migrated']='OLD_PROFILE_HISTORY_TO_KEEP' in json.dumps(profiles[0].rpc('thread/read',dict(threadId=legacy,includeTurns=True)))
        # Imported JSONL has no history projection in the destination DB. The
        # desktop uses these paged reads, not thread/read's fallback parser.
        checks['imported_history_index_rebuilt']=all(
            'OLD_PROFILE_HISTORY_TO_KEEP' in json.dumps(c.rpc('thread/turns/list',
                dict(threadId=legacy,limit=10,itemsView='full')))
            for c in [a,*profiles])
        for c in [a,*profiles]: c.rpc('thread/resume',dict(threadId=tid,excludeTurns=True))
        a.turn(tid,'ORIGINAL_AFTER_ALL_PROFILES_OPEN')
        accounts=[fixture.calls[-1]['account']]
        profiles[0].rpc('thread/settings/update',dict(threadId=tid,model='gpt-5.6-sol',effort='low'))
        profiles[0].turn(tid,'PROFILE_02_CONTEXT')
        accounts.append(fixture.calls[-1]['account'])
        checks['managed_context_refresh']='ORIGINAL_AFTER_ALL_PROFILES_OPEN' in json.dumps(fixture.calls[-1]['input'])
        checks['chosen_model_effort_and_account']=fixture.calls[-1]['model']=='gpt-5.6-sol' and fixture.calls[-1]['reasoning']['effort']=='low' and fixture.calls[-1]['account']=='B'
        profiles[1].turn(tid,'PROFILE_03_CONTEXT')
        accounts.append(fixture.calls[-1]['account'])
        checks['profile_to_profile_context_refresh']='PROFILE_02_CONTEXT' in json.dumps(fixture.calls[-1]['input'])
        a.turn(tid,'ORIGINAL_RETURN_CONTEXT')
        accounts.append(fixture.calls[-1]['account'])
        checks['original_context_refresh']='PROFILE_03_CONTEXT' in json.dumps(fixture.calls[-1]['input'])
        profiles[2].turn(tid,'PROFILE_04_CONTEXT')
        accounts.append(fixture.calls[-1]['account'])
        report['turn_accounts']=accounts
        checks['each_profile_uses_selected_account']=accounts==['A','B','C','A','D']
        profiles[2].rpc('thread/name/set',dict(threadId=tid,name='canonical title'))
        checks['rename_visible_in_original']=a.rpc('thread/read',dict(threadId=tid,includeTurns=False))['thread']['name']=='canonical title'
        section=profiles[0].rpc('threadSection/create',dict(name='Shared order'))['section']['id']
        for task in [legacy,tid]: profiles[0].rpc('thread/section/move',dict(threadId=task,sectionId=section))
        profiles[0].rpc('thread/section/move',dict(threadId=tid,sectionId=section,beforeThreadId=legacy))
        pages=[]
        for c in [a,*profiles]:
            first=c.rpc('thread/list',dict(sectionId=section,sortKey='section_position',limit=1))
            second=c.rpc('thread/list',dict(sectionId=section,sortKey='section_position',limit=1,cursor=first['nextCursor']))
            pages.append([first['data'][0]['id'],second['data'][0]['id']])
        checks['section_order_pagination_supported']=all(page==[tid,legacy] for page in pages)
        # Native ancestry follows the persisted spawn graph, including indirect descendants.
        with closing(sqlite3.connect(source/'state_5.sqlite')) as db, db:
            db.execute('INSERT INTO thread_spawn_edges VALUES(?,?,?)',(tid,legacy,'completed'))
        checks['ancestry_supported']=all([r['id'] for r in c.rpc('thread/list',dict(ancestorThreadId=tid,sourceKinds=[]))['data']]==[legacy] for c in profiles)
        checks['no_profile_rollout_copies']=all(not list((output/alias/'sessions').rglob('*.jsonl')) for alias in ('02','03','04'))
        checks['all_processes_stay_alive']=all(c.proc.poll() is None for c in clients)
        profiles[2].rpc('thread/settings/update',dict(threadId=tid,model='gpt-5.6-sol',effort='low'))
        for c in clients: c.close()
        clients=[]
        cold=client(output/'04','D',canonical_home=source)
        resumed=cold.rpc('thread/resume',dict(threadId=tid,excludeTurns=True))
        checks['last_model_effort_after_reopen']=(resumed['model'],resumed['reasoningEffort'])==('gpt-5.6-sol','low')
        cold.close();clients=[]
        rollout=next((source/'sessions').rglob('*'+tid+'.jsonl'))
        ordinals=[json.loads(line)['ordinal'] for line in rollout.read_text(encoding='utf8').splitlines()]
        checks['durable_ordinals_increase']=all(a<b for a,b in zip(ordinals,ordinals[1:]))
        report.update(status='PASS' if all(checks.values()) else 'FAIL',checks=checks)
    except Exception as error:
        report.update(error=str(error),checks=checks)
    finally:
        for c in clients: c.close()
        fixture.close()
        with binary.open('rb') as stream: report['runtime_sha256']=hashlib.file_digest(stream,'sha256').hexdigest()
        report['finished_at']=datetime.now(timezone.utc).isoformat()
        (output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf8')
        print(json.dumps(dict(report=str(output/'report.json'),**report)))
    return report['status']=='PASS'


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--runtime',type=Path,required=True)
    raise SystemExit(0 if run(parser.parse_args().runtime) else 1)
