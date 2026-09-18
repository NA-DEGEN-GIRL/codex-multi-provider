"""Isolated native-runtime shared-editing regression. Uses loopback fixture replies.
Never reads user credentials or launches a desktop window."""
import argparse, hashlib, json, os, queue, subprocess, threading, time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4
from manager_core.app_transport import RuntimeObserver
from manager_core.catalog_projection import CatalogProjectionIds

ROOT = Path(__file__).resolve().parents[1]
class Fixture:
    def __init__(self, before_response=None):
        self.calls = []
        self.before_response = before_response
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
                account = {'Bearer fixture-'+alias:alias for alias in 'ABCD'}.get(self.headers.get('Authorization'))
                if account is None:
                    self.send_error(401); return
                owner.calls.append(dict(account=account, model=body.get('model'),
                                        reasoning=body.get('reasoning'), input=body.get('input')))
                item = dict(type='message', id='msg_'+uuid4().hex, role='assistant', status='completed',
                            content=[dict(type='output_text', text='PROFILE_'+account+'_DONE', annotations=[])])
                rid='resp_'+uuid4().hex
                events = [
                    dict(type='response.created', response=dict(id=rid,status='in_progress',output=[])),
                    dict(type='response.output_item.added', output_index=0,item=item),
                    dict(type='response.output_item.done', output_index=0,item=item),
                    dict(type='response.completed', response=dict(id=rid,status='completed',output=[item],
                         usage=dict(input_tokens=1,output_tokens=1,total_tokens=2)))]
                data=''.join('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n' for e in events).encode()
                if owner.before_response: owner.before_response(body)
                self.send_response(200); self.send_header('Content-Type','text/event-stream')
                self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
        self.server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
    def close(self): self.server.shutdown(); self.server.server_close()

class Client:
    def __init__(self,binary,home,catalog,alias,port,writer_id, *, new_thread_home=None, shared_append=False, canonical_home=None, config_text=None, extra_environment=None, request_adapter=None, launch_command=None):
        self.request_adapter = request_adapter
        self.projections = CatalogProjectionIds(None)
        self.observer = RuntimeObserver(str(uuid4()), read_only_projection=self.projections)
        home.mkdir(exist_ok=True)
        default_config = (
            'model="gpt-5.5"\nmodel_provider="fixture"\nmodel_reasoning_effort="medium"\n'
            'cli_auth_credentials_store="ephemeral"\napproval_policy="never"\n'
            '[features]\nshell_tool=false\n'
            '[model_providers.fixture]\nname="Loopback fixture"\nbase_url="http://127.0.0.1:'+str(port)+'/v1"\n'
            'env_key="LOCAL_FIXTURE_TOKEN"\nwire_api="responses"\n')
        (home/'config.toml').write_text(config_text if config_text is not None else default_config, encoding='utf-8')
        env={k:v for k,v in os.environ.items() if k.upper() in
             {'SYSTEMROOT','WINDIR','PATH','TEMP','TMP','USERPROFILE','LOCALAPPDATA','APPDATA'}}
        env.update(CODEX_HOME=str(home), CODEX_MANAGER_SHARED_CATALOG=str(catalog),
                   CODEX_MANAGER_SHARED_EXECUTION='1',CODEX_MANAGER_SHARED_WRITER_ID=writer_id,
                   LOCAL_FIXTURE_TOKEN='fixture-'+alias)
        if catalog is None:
            for name in ('CODEX_MANAGER_SHARED_CATALOG', 'CODEX_MANAGER_SHARED_EXECUTION', 'CODEX_MANAGER_SHARED_WRITER_ID'):
                env.pop(name, None)
        if new_thread_home is not None:
            env['CODEX_MANAGER_NEW_THREAD_HOME'] = str(new_thread_home)
        if shared_append:
            env['CODEX_RECORD_SHARED_APPEND'] = '1'
        if canonical_home is not None:
            env.update(CODEX_RECORD_HOME=str(canonical_home), CODEX_SQLITE_HOME=str(canonical_home),
                       CODEX_MANAGER_SHARED_EXECUTION='1', CODEX_RECORD_SHARED_APPEND='1',
                       CODEX_MANAGER_SHARED_WRITER_ID=writer_id)
        env.update(extra_environment or {})
        self.proc=subprocess.Popen([*(launch_command or [str(binary)]),'app-server','--listen','stdio://'],cwd=home,env=env,
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.messages=queue.Queue(); self.events=[]; self.errors=[]; self.counter=0
        def read():
            for line in self.proc.stdout:
                try:self.messages.put(json.loads(line))
                except ValueError:pass
            self.messages.put(None)
        def stderr():
            for line in self.proc.stderr:
                self.errors.append(line.decode('utf-8','replace')[:1000])
                if len(self.errors)>40:self.errors.pop(0)
        threading.Thread(target=read,daemon=True).start()
        threading.Thread(target=stderr,daemon=True).start()
        self.rpc('initialize',dict(clientInfo=dict(name='shared_editing_fixture',version='1'),
                 capabilities=dict(experimentalApi=True)))
        self.send(dict(method='initialized'))
    def next(self,deadline):
        message=self.messages.get(timeout=max(.1,deadline-time.monotonic()))
        if message is None:raise RuntimeError('Fixture runtime exited: '+''.join(self.errors[-3:]))
        self.observer.consume('server', message)
        if 'method' in message:
            self.events.append(message)
            if 'id' in message:
                self.send(dict(id=message['id'],error=dict(code=-32601,message='Fixture does not grant additional permissions')))
        return message
    def send(self,value):
        if self.request_adapter:value=self.request_adapter(value)
        self.observer.consume('client', value)
        self.proc.stdin.write(json.dumps(value).encode()+b'\n');self.proc.stdin.flush()
    def event(self, method, matches, timeout=25):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for event in self.events:
                if event.get('method') == method and matches(event.get('params', {})):
                    return event['params']
            self.next(deadline)
        raise TimeoutError(method)
    def rpc(self,method,params):
        self.counter+=1; identity=self.counter
        self.send(dict(id=identity,method=method,params=params));deadline=time.monotonic()+60
        while time.monotonic()<deadline:
            message=self.next(deadline)
            if message.get('id')==identity and 'method' not in message:
                if 'error' in message:raise RuntimeError(method+': '+str(message['error']))
                return message['result']
        raise TimeoutError(method)
    def turn(self,tid,text):
        result=self.rpc('turn/start',dict(threadId=tid,input=[dict(type='text',text=text,text_elements=[])]))
        turn=result['turn']['id'];deadline=time.monotonic()+60
        while time.monotonic()<deadline:
            for e in self.events:
                if e.get('method')=='turn/completed' and e['params']['turn']['id']==turn:
                    assert e['params']['turn']['status']=='completed', e['params']['turn']
                    return
            self.next(deadline)
        raise TimeoutError('turn/completed')
    def close(self):
        self.proc.stdin.close()
        try:self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill();self.proc.wait(timeout=5) # Only this isolated fixture.
        self.proc.stdout.close(); self.proc.stderr.close()
        self.projections.close()

def run(binary, history_mode='legacy'):
    binary=Path(binary).resolve(strict=True)
    output=ROOT/'artifacts/results'/('shared-editing-'+uuid4().hex[:8]);output.mkdir(parents=True)
    source=output/'source';sessions=source/'sessions/2026/09/15';sessions.mkdir(parents=True)
    tid=str(uuid4());projection=str(uuid4())
    rollout=sessions/('rollout-2026-09-15T00-00-00-'+tid+'.jsonl')
    rows=[dict(timestamp='2026-09-15T00:00:00Z',type='session_meta',payload=dict(
        session_id=tid,id=tid,timestamp='2026-09-15T00:00:00Z',cwd=str(output),
        originator='fixture',cli_version='test',source='cli',model_provider='fixture',history_mode='legacy')),
        dict(timestamp='2026-09-15T00:00:01Z',type='event_msg',
             payload=dict(type='user_message',message='original question',kind='plain'))]
    rollout.write_text(''.join(json.dumps(r)+'\n' for r in rows),encoding='utf-8')
    (source/'auth.json').write_text('SOURCE_CREDENTIAL_SENTINEL')
    (source/'config.toml').write_text('SOURCE_CONFIG_SENTINEL')
    catalog=output/'catalog.json'
    catalog.write_text(json.dumps(dict(version=1,revision='fixture',hostId='local',entries=[dict(
        projectionThreadId=projection,threadId=tid,hostId='local',sourceStoreId='original:fixture',
        codexHome=str(source),rolloutPath=str(rollout))])),encoding='utf-8')
    fixture=Fixture();clients=[];checks={};report=dict(status='FAIL',gui_used=False,real_model_calls=0,
        validation_kind='shared_record_execution_v1',history_mode=history_mode)
    try:
        if history_mode == 'paginated':
            seed=Client(binary,source,catalog,'A',fixture.server.server_port,str(uuid4()));clients.append(seed)
            tid=seed.rpc('thread/start',dict(historyMode='paginated',cwd=str(output)))['thread']['id']
            seed.turn(tid,'original question')
            seed.close();clients=[]
            rollout=next(p for p in (source/'sessions').rglob('*.jsonl') if p.name.endswith(tid+'.jsonl'))
            document=json.loads(catalog.read_text())
            document['entries'][0].update(threadId=tid,rolloutPath=str(rollout))
            catalog.write_text(json.dumps(document),encoding='utf-8')
            (source/'auth.json').write_text('SOURCE_CREDENTIAL_SENTINEL')
            (source/'config.toml').write_text('SOURCE_CONFIG_SENTINEL')
            fixture.calls.clear()
        ids=[str(uuid4()),str(uuid4())]
        a=Client(binary,output/'account-a',catalog,'A',fixture.server.server_port,ids[0]);clients.append(a)
        b=Client(binary,output/'account-b',catalog,'B',fixture.server.server_port,ids[1]);clients.append(b)
        listed=a.rpc('thread/list',dict(limit=10,sourceKinds=['cli','vscode','appServer','unknown']))['data']
        report['listed_id_matches'] = [dict(matches=x['id']==tid, source=x.get('source'), accepts=x.get('canAcceptDirectInput')) for x in listed]
        checks['canonical_list'] = any(x['id']==tid and x.get('canAcceptDirectInput') is not False for x in listed)
        # Discover a new external task after subscribing. The old runtime used
        # canonical IDs for listing but read-only projections for these events.
        added_id, added_projection = str(uuid4()), str(uuid4())
        added_rollout = sessions / ('rollout-2026-09-15T00-00-00-' + added_id + '.jsonl')
        added_rows = json.loads(json.dumps(rows))
        added_rows[0]['payload'].update(id=added_id, session_id=added_id)
        added_rollout.write_text(''.join(json.dumps(row)+'\n' for row in added_rows), encoding='utf-8')
        document = json.loads(catalog.read_text())
        document['entries'].append(dict(projectionThreadId=added_projection, threadId=added_id,
            hostId='local', sourceStoreId='original:fixture', codexHome=str(source), rolloutPath=str(added_rollout)))
        from manager_core.store import atomic_json
        atomic_json(catalog, document)
        added = a.event('thread/started', lambda p: p.get('thread', {}).get('id') == added_id)['thread']
        checks['live_membership_matches_list'] = (added.get('canAcceptDirectInput') is not False
            and not (added.get('extra') or {}).get('managedRecord'))
        checks['live_membership_preserves_observer'] = a.observer.snapshot()['stream_complete']
        assert checks['live_membership_matches_list'] and checks['live_membership_preserves_observer'], 'Live catalog event disagrees with editable listing.'
        ar=a.rpc('thread/resume',dict(threadId=projection,excludeTurns=True))
        br=b.rpc('thread/resume',dict(threadId=tid,excludeTurns=True))
        checks['both_profiles_can_open'] = ar['thread']['id']==br['thread']['id']==tid
        a.rpc('thread/settings/update',dict(threadId=tid,model='gpt-5.5',effort='high'))
        a.turn(tid,'Reply A with the fixture response.')
        b.rpc('thread/settings/update',dict(threadId=tid,model='gpt-5.6-sol',effort='low'))
        b.turn(tid,'Reply B with the fixture response.')
        checks['selected_credentials_used'] = {x['account'] for x in fixture.calls}=={'A','B'}
        checks['effort_applied_to_requests'] = any(x['account']=='A' and (x['reasoning'] or {}).get('effort')=='high' for x in fixture.calls) and any(x['account']=='B' and (x['reasoning'] or {}).get('effort')=='low' for x in fixture.calls)
        b.rpc('thread/name/set',dict(threadId=tid,name='shared editing verified'))
        a.event('thread/name/updated', lambda p: p.get('threadId') == tid and p.get('threadName') == 'shared editing verified')
        checks['live_name_uses_canonical_id'] = True
        document['entries'] = [entry for entry in document['entries'] if entry['threadId'] != added_id]
        atomic_json(catalog, document)
        a.event('thread/deleted', lambda p: p.get('threadId') == added_id)
        checks['live_removal_uses_canonical_id'] = True
        a.rpc('thread/loaded/list', {})
        snapshot = a.observer.snapshot()
        checks['activity_remains_observable'] = snapshot['stream_complete'] and snapshot['active_turn_count'] == 0
        for c in clients:c.close()
        clients=[]
        text=rollout.read_text(encoding='utf-8')
        parsed=[json.loads(line) for line in text.splitlines()]
        checks['same_record_contains_both_replies'] = 'PROFILE_A_DONE' in text and 'PROFILE_B_DONE' in text and parsed[0]['payload']['id']==tid
        checks['source_credentials_and_config_unchanged'] = (source/'auth.json').read_text()=='SOURCE_CREDENTIAL_SENTINEL' and (source/'config.toml').read_text()=='SOURCE_CONFIG_SENTINEL'
        c=Client(binary,output/'account-a',catalog,'A',fixture.server.server_port,ids[0]);clients.append(c)
        again=c.rpc('thread/resume',dict(threadId=tid,excludeTurns=True))
        if history_mode == 'paginated':
            turns=c.rpc('thread/turns/list',dict(threadId=tid,limit=10,itemsView='full'))
            report['paginated_turn_count'] = len(turns['data'])
            report['paginated_turn_ids'] = [t['id'] for t in turns['data']]
            report['paginated_next_cursor'] = turns.get('nextCursor')
            checks['paged_history_available'] = len(turns['data']) >= 3
        checks['cold_resume_keeps_effort'] = again.get('reasoningEffort')=='low'
        checks['cold_resume_keeps_model'] = again.get('model')=='gpt-5.6-sol'
        checks['cold_resume_keeps_title'] = again['thread'].get('name')=='shared editing verified'
        report.update(status='PASS' if all(checks.values()) else 'FAIL',checks=checks,
                      fixture_requests=[{k:v for k,v in x.items() if k!='input'} for x in fixture.calls],
                      resumed_model=again.get('model'),resumed_effort=again.get('reasoningEffort'))
    except Exception as e:
        report.update(error=str(e),checks=checks)
    finally:
        for c in clients:c.close()
        fixture.close()
        with Path(binary).open('rb') as stream:report['runtime_sha256']=hashlib.file_digest(stream,'sha256').hexdigest()
        report['finished_at']=datetime.now(timezone.utc).isoformat()
        path=output/'report.json';path.write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(dict(report=str(path),**report)))
    return report['status']=='PASS'
if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--runtime',type=Path,required=True)
    parser.add_argument('--history-mode',choices=['legacy','paginated'],default='legacy')
    args=parser.parse_args()
    raise SystemExit(0 if run(args.runtime,args.history_mode) else 1)
