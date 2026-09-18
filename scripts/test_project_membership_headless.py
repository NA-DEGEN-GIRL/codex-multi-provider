"""Real native project moves across one original and three profile clients."""
import json
from pathlib import Path
from uuid import uuid4

from test_shared_editing_headless import Client, Fixture, ROOT
from manager_core.runtime_build import resolve
from manager_core.app_preferences import prepare


def run():
    output = ROOT / 'artifacts/results' / ('project-membership-' + uuid4().hex[:8])
    output.mkdir(parents=True)
    fixture = Fixture()
    clients, checks = [], {}
    report = {'real_model_calls': 0}
    try:
        binary = Path(resolve(ROOT)['runtime'])
        source = output / 'original'
        original = Client(binary, source, None, 'A', fixture.server.server_port, str(uuid4()), shared_append=True)
        clients.append(original)
        projects = {name: original.rpc('project/create', dict(name=name, roots=[dict(path=str(output/name))],
            idempotencyKey=str(uuid4())))['project']['id'] for name in ('asset','audio')}
        tid = original.rpc('thread/start', dict(cwd=str(output), historyMode='paginated'))['thread']['id']
        original.turn(tid, 'PROJECT_MEMBERSHIP_FIXTURE')
        original.rpc('thread/metadata/update',dict(threadId=tid,projectId=projects['asset']))
        state = {'thread-project-assignments': {tid: dict(projectKind='local',projectId='audio')},
            'app-server-project-id-by-legacy-project-id-by-host': {'local:'+str(source): projects},
            'app-server-projects-migration-by-host': {'local:'+str(source): dict(projectsMigrated=True,
                threadAssignmentsMigrated=False,threadAssignmentsReadMigrated=True,pendingThreadAssignmentIds=[tid])}}
        (source/'.codex-global-state.json').write_text(json.dumps(state),encoding='utf-8')
        for alias,account in zip(('02','03','04'),'BCD'):
            home=output/alias
            prepare(home,source,canonical=True)
            prepared=json.loads((home/'.codex-global-state.json').read_text())
            checks[alias+'_pending_move']=prepared['thread-project-assignments'][tid]['projectId']=='audio'
            client=Client(binary,home,None,account,fixture.server.server_port,str(uuid4()),canonical_home=source)
            clients.append(client)
            client.rpc('thread/read',dict(threadId=tid,includeTurns=False))
        # The same native metadata operation used by desktop migration and UI.
        for owner,name in [(clients[1],'audio'),(original,'asset'),(clients[2],'audio'),(original,None)]:
            project = projects[name] if name else ''
            owner.rpc('thread/metadata/update',dict(threadId=tid,projectId=project))
            checks['move_'+str(len(checks))] = all(
                client.rpc('thread/read',dict(threadId=tid,includeTurns=False))['thread']['projectId']==(project or None)
                for client in clients)
            for pid in projects.values():
                expected=[tid] if pid==project else []
                checks['listing_'+str(len(checks))]=all(
                    [t['id'] for t in client.rpc('thread/list',dict(projectId=pid))['data']]==expected for client in clients)
        checks['body_kept']=all('PROJECT_MEMBERSHIP_FIXTURE' in json.dumps(client.rpc(
            'thread/read',dict(threadId=tid,includeTurns=True))) for client in clients)
        report.update(passed=all(checks.values()),checks=checks)
    except Exception as error:
        report.update(passed=False,error=str(error),checks=checks)
    finally:
        for client in clients:client.close()
        fixture.close()
        (output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(dict(report=str(output/'report.json'),**report)))
    return report['passed']


if __name__=='__main__':
    raise SystemExit(0 if run() else 1)
