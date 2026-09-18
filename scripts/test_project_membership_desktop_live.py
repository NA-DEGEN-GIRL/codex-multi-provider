"""Real legacy drag/drop -> common runtime -> peer assignment projection.

Uses hidden desktop/disposable homes and loopback model responses only.
"""
import json
import time
import test_desktop_record_sync_live as fixture

PROBE = rb'''
(() => {
 const fs=require('node:fs'),path=require('node:path');
 const root=process.env.FIXTURE_ROOT,tid=process.env.FIXTURE_THREAD;
 const sync=globalThis.__codexLocalWorkspaceSync,register=sync.register,registerBackend=sync.registerBackend;
 let state,backend,phase=0,busy=false,project;
 sync.register=function(s,w){register(s,w);state=s;};
 sync.registerBackend=function(b){registerBackend(b);if(b.cache.hostId==='local')backend=b;};
 setInterval(async()=>{
  if(busy||!state||!backend?.projectsReady)return;busy=true;
  try{
   if(phase===0){
    backend.setThreadAssignmentsEnabled(false);
    const id='66666666-6666-4666-8666-666666666661',writer='77777777-7777-4777-8777-777777777771';
    const result=await backend.connection.sendAppServerRequest('project/create',{name:'MOVED_TASK_PROJECT',roots:[{path:root}],idempotencyKey:id});
    project={id,name:'MOVED_TASK_PROJECT',rootPaths:[root],createdAt:Date.now(),updatedAt:Date.now(),serverId:result.project.id};
    const dir=path.join(process.env.CODEX_RECORD_SIGNALS,'local-workspaces');fs.mkdirSync(dir,{recursive:true});
    fs.writeFileSync(path.join(dir,writer+'.json'),JSON.stringify({version:1,projects:[[id,1,writer,project]]}));
    phase=1;
   }else if(phase===1){
    const id=Object.keys(state.getStored('local-projects')||{}).find(id=>state.getStored('local-projects')[id].name==='MOVED_TASK_PROJECT');
    if(!id||!backend.serverProjectsByLegacyId.has(id))return;
    const assignment={projectKind:'local',projectId:id};
    await backend.writeThreadAssignment(tid,assignment,async()=>{
     state.set('thread-project-assignments',{...state.getStored('thread-project-assignments'),[tid]:assignment});
     state.set('projectless-thread-ids',(state.getStored('projectless-thread-ids')||[]).filter(id=>id!==tid));
    },undefined,'local');
    const {thread}=await backend.connection.sendAppServerRequest('thread/read',{threadId:tid,includeTurns:false});
    fs.writeFileSync(path.join(root,'membership-moved.json'),JSON.stringify({nativeProject:backend.serverProjectsByLegacyId.get(id).id,storedProject:thread.projectId,legacyProject:id}));
    phase=2;
   }else if(phase===2&&fs.existsSync(path.join(root,'peer-moved-out'))){
    if(state.getStored('thread-project-assignments')?.[tid]!=null)return;
    if(!(state.getStored('projectless-thread-ids')||[]).includes(tid))return;
    fs.writeFileSync(path.join(root,'membership-observed.json'),JSON.stringify({peer_removal_visible_without_restart:true}));phase=3;
   }
  }catch(e){fs.writeFileSync(path.join(root,'membership-error.txt'),String(e));}
  finally{busy=false;}
 },200).unref();
})();
'''


def wait_file(path):
    end=time.monotonic()+20
    while time.monotonic()<end:
        if path.exists():return json.loads(path.read_text())
        error=path.parent/'membership-error.txt'
        if error.exists():raise RuntimeError(error.read_text())
        time.sleep(.1)
    raise TimeoutError('Membership fixture timed out: '+path.name)


if __name__=='__main__':
    checks={}
    def moved(writer,tid,output):
        result=wait_file(output/'membership-moved.json')
        assert result['storedProject']==result['nativeProject']
        checks['legacy_drag_committed_to_canonical_store']=True
        peer=writer.rpc('thread/read',{'threadId':tid,'includeTurns':False})['thread']
        assert peer['projectId']==result['nativeProject']
        checks['other_provider_sees_moved_project']=True
        writer.rpc('thread/metadata/update',{'threadId':tid,'projectId':''})
        writer.rpc('thread/list',{'limit':1})
        (output/'peer-moved-out').touch()
        checks.update(wait_file(output/'membership-observed.json'))
    fixture.DIAGNOSTIC=PROBE+fixture.DIAGNOSTIC
    before=set((fixture.ROOT/'artifacts/results').glob('desktop-sync-*'))
    passed=fixture.run(before_writes=moved)
    output=(set((fixture.ROOT/'artifacts/results').glob('desktop-sync-*'))-before).pop()
    report=json.loads((output/'report.json').read_text())
    report['checks'].update(checks);report['passed']=passed and len(checks)==3 and all(checks.values())
    (output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    print(json.dumps({'passed':report['passed'],'membership_checks':checks,'output':str(output)}))
    raise SystemExit(not report['passed'])
