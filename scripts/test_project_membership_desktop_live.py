"""Real legacy drag/drop -> common runtime -> peer assignment projection.

An unimported legacy assignment (canonical threads.project_id NULL) must survive
desktop observation and mode refresh, including a host that already finished its
native read migration while a local move is still pending, and it must not
repeat NULL reads while blocked. Only a guard state change, a positive native
value or a confirmed peer removal may change it. Uses a hidden desktop with
disposable homes and loopback model responses only.
"""
import json
import time
import test_desktop_record_sync_live as fixture

PROBE = rb'''
(() => {
 const fs=require('node:fs'),path=require('node:path');
 const root=process.env.FIXTURE_ROOT,tid=process.env.FIXTURE_THREAD,signals=process.env.CODEX_RECORD_SIGNALS;
 const assignmentKey='thread-project-assignments',projectlessKey='projectless-thread-ids';
 const migrationKey='app-server-projects-migration-by-host';
 const proofDirectory=path.join(signals,'project-membership-proofs');
 const proofWriter=process.env.CODEX_MANAGER_PROFILE_ID||'00000000-0000-4000-8000-000000000001';
 const ownProofFile=path.join(proofDirectory,proofWriter+'.json'),foreignWriter='88888888-8888-4888-8888-888888888881';
 const sync=globalThis.__codexLocalWorkspaceSync,register=sync.register,registerBackend=sync.registerBackend;
 let state,backend,nativeWrite,keptId,phase=0,ticks=0,repeats=0,repeatStart=0,busy=false,pendingAfterSeed=false;
 let observedNullAdopts=0,centralNullAdopts=0,threadReads=0,projectWrites=0;
 function save(name,value){fs.writeFileSync(path.join(root,name),JSON.stringify(value));}
 function stored(){return state.getStored(assignmentKey)?.[tid]?.projectId??null;}
 function pending(){return state.getStored(migrationKey)?.[backend.migrationIdentity]?.pendingThreadAssignmentIds??[];}
 function projectless(){return (state.getStored(projectlessKey)||[]).includes(tid);}
 function migrate(change){
   const migrations=state.getStored(migrationKey)||{},previous=migrations[backend.migrationIdentity]||{};
   state.set(migrationKey,{...migrations,[backend.migrationIdentity]:{...previous,...change}});
 }
 function proofHas(file){
   try{
     const data=JSON.parse(fs.readFileSync(file,'utf8'));
     return data.version===1&&Array.isArray(data.thread_ids)&&data.thread_ids.includes(tid);
   }catch{return false;}
 }
 function anyProof(){
   try{return fs.readdirSync(proofDirectory).some(name=>name.endsWith('.json')&&proofHas(path.join(proofDirectory,name)));}
   catch{return false;}
 }
 function pause(ms){return new Promise(resolve=>setTimeout(resolve,ms));}
 sync.register=function(s,w){register(s,w);state=s;};
 sync.registerBackend=function(b){
   const native=b.writeThreadAssignment;
   registerBackend(b);
   if(b.cache.hostId!=='local'||backend)return;
   backend=b;nativeWrite=native;
   const adopt=b.threadAssignments.adopt;
   b.threadAssignments.adopt=function(id,projectId){
     if(id===tid&&projectId===null)observedNullAdopts++;
     return Reflect.apply(adopt,this,arguments);
   };
   const request=b.connection.sendAppServerRequest;
   b.connection.sendAppServerRequest=async function(method,params){
     if(params?.threadId===tid){
       if(method==='thread/read')threadReads++;
       if(method==='thread/metadata/update'&&typeof params.projectId==='string')projectWrites++;
     }
     return Reflect.apply(request,this,arguments);
   };
 };
 setInterval(async()=>{
  if(busy||!state||!backend?.projectsReady)return;busy=true;
  try{
   if(phase===0){
    backend.setThreadAssignmentsEnabled(false);
    const movedId='66666666-6666-4666-8666-666666666661',writer='77777777-7777-4777-8777-777777777771';
    keptId='66666666-6666-4666-8666-666666666662';
    const result=await backend.connection.sendAppServerRequest('project/create',{name:'MOVED_TASK_PROJECT',roots:[{path:root}],idempotencyKey:movedId});
    const project={id:movedId,name:'MOVED_TASK_PROJECT',rootPaths:[root],createdAt:Date.now(),updatedAt:Date.now(),serverId:result.project.id};
    // KEPT_TASK_PROJECT stays a legacy-only declaration. It never receives a
    // native server project id, so a task assigned to it stays unimported.
    const kept={id:keptId,name:'KEPT_TASK_PROJECT',rootPaths:[root],createdAt:Date.now(),updatedAt:Date.now()};
    const dir=path.join(signals,'local-workspaces');fs.mkdirSync(dir,{recursive:true});
    fs.writeFileSync(path.join(dir,writer+'.json'),JSON.stringify({version:1,projects:[[movedId,1,writer,project],[keptId,2,writer,kept]]}));
    phase=1;
   }else if(phase===1){
    const projects=state.getStored('local-projects')||{};
    const moved=Object.keys(projects).find(key=>projects[key].name==='MOVED_TASK_PROJECT');
    const kept=Object.keys(projects).find(key=>projects[key].name==='KEPT_TASK_PROJECT');
    if(!moved||!kept||!backend.serverProjectsByLegacyId.has(moved))return;
    keptId=kept;
    const assignment={projectKind:'local',projectId:kept};
    // A disabled-mode move is stored in legacy state only. The native store
    // write is the call the window host makes for it, so it also records the
    // pending migration marker before the task ever reaches native storage.
    await Reflect.apply(nativeWrite,backend,[tid,assignment,async()=>{
      state.set(assignmentKey,{...(state.getStored(assignmentKey)||{}),[tid]:assignment});
      state.set(projectlessKey,(state.getStored(projectlessKey)||[]).filter(value=>value!==tid));
    },undefined,'local']);
    const {thread}=await backend.connection.sendAppServerRequest('thread/read',{threadId:tid,includeTurns:false});
    if(thread.projectId!=null)throw Error('Fixture thread already has native membership.');
    if(backend.threadAssignments.matches(tid,null))throw Error('Legacy assignment is not visible to the native store.');
    pendingAfterSeed=pending().includes(tid);ticks=0;phase=2;
   }else if(phase===2){
    // The desktop observes this task through its own notification/listing path
    // and the adapter refreshes membership from native metadata.
    if(observedNullAdopts===0){
      if(ticks++>=40)throw Error('Desktop observation never reached the native assignment store.');
      backend.observeThreads([{id:tid,projectId:null}]);return;
    }
    const readsBefore=threadReads,observed=observedNullAdopts;
    backend.threadAssignments.adopt(tid,null);  // central store path a mode refresh uses
    centralNullAdopts=observedNullAdopts-observed;
    globalThis.__codexProjectMembership.refresh(backend);
    const {thread}=await backend.connection.sendAppServerRequest('thread/read',{threadId:tid,includeTurns:false});
    await globalThis.__codexProjectMembership.tick();
    const proof=anyProof();
    if(stored()!==keptId||projectless()||thread.projectId!=null||projectWrites!==0||proof)
      throw Error('Unimported legacy membership was not preserved: '+JSON.stringify({stored:stored(),
        projectless:projectless(),native:thread.projectId,projectWrites,proof,pending:pending()}));
    save('membership-preserved.json',{legacyProject:keptId,storedProject:stored(),nativeProjectId:null,
      observedNullAdoptAttempts:observed,centralNullAdoptAttempts:centralNullAdopts,canonicalReads:threadReads-readsBefore,
      projectWrites,pendingAfterSeed,pendingKept:pending().includes(tid),projectless:false,proofRecorded:proof});
    phase=3;
   }else if(phase===3){
    // A host can already be read-migrated while this task is still pending, and
    // an old confirmation can exist. The pending marker must still hold the NULL.
    if(repeats===0){
      migrate({threadAssignmentsReadMigrated:true});
      fs.mkdirSync(proofDirectory,{recursive:true});
      fs.writeFileSync(path.join(proofDirectory,foreignWriter+'.json'),JSON.stringify({version:1,thread_ids:[tid]}));
      await globalThis.__codexProjectMembership.tick();
      repeatStart=threadReads;
    }
    if(repeats<5){
      backend.observeThreads([{id:tid,projectId:null}]);repeats++;
      if(repeats<5)return;
      await pause(700);
    }
    const repeatReads=threadReads-repeatStart;
    const {thread}=await backend.connection.sendAppServerRequest('thread/read',{threadId:tid,includeTurns:false});
    if(stored()!==keptId||projectless()||thread.projectId!=null||repeatReads>1||!pending().includes(tid))
      throw Error('Pending legacy membership was not preserved: '+JSON.stringify({stored:stored(),
        projectless:projectless(),native:thread.projectId,repeatReads,pending:pending()}));
    save('membership-pending.json',{legacyProject:keptId,storedProject:stored(),nativeProjectId:null,
      readMigrated:true,foreignProofSeeded:true,repeatObservations:repeats,repeatReads,
      pendingKept:pending().includes(tid),projectWrites,projectless:false});
    ticks=0;phase=4;
   }else if(phase===4){
    // Dropping the pending marker changes the guard state; the blocked task has
    // to be requeued and the native NULL then applies without a restart.
    if(ticks===0)migrate({pendingThreadAssignmentIds:pending().filter(value=>value!==tid)});
    if((stored()!=null||!projectless())&&ticks++<40){
      await globalThis.__codexProjectMembership.tick();
      globalThis.__codexProjectMembership.refresh(backend);
      await pause(150);return;
    }
    if(stored()!=null||!projectless())
      throw Error('Guard state change did not apply the native NULL: '+JSON.stringify({stored:stored(),
        projectless:projectless(),pending:pending()}));
    const {thread}=await backend.connection.sendAppServerRequest('thread/read',{threadId:tid,includeTurns:false});
    save('membership-released.json',{storedProject:null,projectless:true,nativeProjectId:thread.projectId,releaseTicks:ticks});
    phase=5;
   }else if(phase===5){
    const id=Object.keys(state.getStored('local-projects')||{}).find(key=>state.getStored('local-projects')[key].name==='MOVED_TASK_PROJECT');
    if(!id||!backend.serverProjectsByLegacyId.has(id))return;
    const assignment={projectKind:'local',projectId:id};
    await backend.writeThreadAssignment(tid,assignment,async()=>{
      state.set(assignmentKey,{...state.getStored(assignmentKey),[tid]:assignment});
      state.set(projectlessKey,(state.getStored(projectlessKey)||[]).filter(value=>value!==tid));
    },undefined,'local');
    const {thread}=await backend.connection.sendAppServerRequest('thread/read',{threadId:tid,includeTurns:false});
    let proof=false;
    for(let attempt=0;attempt<20&&!proof;attempt++){
      await globalThis.__codexProjectMembership.tick();
      proof=proofHas(ownProofFile);
      if(!proof)await pause(150);
    }
    save('membership-moved.json',{nativeProject:backend.serverProjectsByLegacyId.get(id).id,
      storedProject:thread.projectId,legacyProject:id,proofRecorded:proof});
    phase=6;
   }else if(phase===6&&fs.existsSync(path.join(root,'peer-moved-out'))){
    if(state.getStored(assignmentKey)?.[tid]!=null)return;
    if(!projectless())return;
    save('membership-observed.json',{peer_removal_visible_without_restart:true});phase=7;
   }
  }catch(e){fs.writeFileSync(path.join(root,'membership-error.txt'),String(e));}
  finally{busy=false;}
 },200).unref();
})();
'''


def wait_file(path):
    end=time.monotonic()+30
    while time.monotonic()<end:
        if path.exists():return json.loads(path.read_text())
        error=path.parent/'membership-error.txt'
        if error.exists():raise RuntimeError(error.read_text())
        time.sleep(.1)
    raise TimeoutError('Membership fixture timed out: '+path.name)


if __name__=='__main__':
    checks={}
    def moved(writer,tid,output):
        preserved=wait_file(output/'membership-preserved.json')
        checks['null_observation_reached_native_adopt']=(preserved['observedNullAdoptAttempts']>=1
            and preserved['centralNullAdoptAttempts']>=1 and preserved['canonicalReads']>=1)
        checks['unimported_legacy_assignment_preserved']=(
            preserved['storedProject']==preserved['legacyProject'] and preserved['nativeProjectId'] is None
            and preserved['projectless'] is False and preserved['projectWrites']==0)
        checks['null_observation_keeps_pending_move']=preserved['pendingAfterSeed'] is True and preserved['pendingKept'] is True
        checks['null_observation_records_no_proof']=preserved['proofRecorded'] is False
        pending=wait_file(output/'membership-pending.json')
        checks['pending_read_migrated_null_preserved']=(
            pending['storedProject']==pending['legacyProject'] and pending['nativeProjectId'] is None
            and pending['pendingKept'] is True)
        checks['blocked_null_observations_do_not_repeat_reads']=pending['repeatReads']<=1
        released=wait_file(output/'membership-released.json')
        checks['guard_state_change_applies_null_without_restart']=(
            released['storedProject'] is None and released['projectless'] is True and released['nativeProjectId'] is None)
        result=wait_file(output/'membership-moved.json')
        checks['legacy_drag_committed_to_canonical_store']=result['storedProject']==result['nativeProject']
        checks['native_move_records_monotonic_proof']=result['proofRecorded'] is True
        peer=writer.rpc('thread/read',{'threadId':tid,'includeTurns':False})['thread']
        checks['other_provider_sees_moved_project']=peer['projectId']==result['nativeProject']
        writer.rpc('thread/metadata/update',{'threadId':tid,'projectId':''})
        writer.rpc('thread/list',{'limit':1})
        (output/'peer-moved-out').touch()
        checks.update(wait_file(output/'membership-observed.json'))
    fixture.DIAGNOSTIC=PROBE+fixture.DIAGNOSTIC
    before=set((fixture.ROOT/'artifacts/results').glob('desktop-sync-*'))
    passed=fixture.run(before_writes=moved)
    output=(set((fixture.ROOT/'artifacts/results').glob('desktop-sync-*'))-before).pop()
    report=json.loads((output/'report.json').read_text())
    expected={'null_observation_reached_native_adopt','unimported_legacy_assignment_preserved',
        'null_observation_keeps_pending_move','null_observation_records_no_proof','pending_read_migrated_null_preserved',
        'blocked_null_observations_do_not_repeat_reads','guard_state_change_applies_null_without_restart',
        'legacy_drag_committed_to_canonical_store','native_move_records_monotonic_proof',
        'other_provider_sees_moved_project','peer_removal_visible_without_restart'}
    report['checks'].update(checks)
    report['passed']=passed and expected<=set(checks) and all(checks.values())
    (output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    print(json.dumps({'passed':report['passed'],'membership_checks':checks,'output':str(output)}))
    raise SystemExit(not report['passed'])
