// Project declarations and task membership are separate native operations.
// Some desktop builds save drag/drop only in legacy UI state. Commit those
// explicit local moves through the common runtime, and project peer metadata
// back through the native assignment store without navigating any window.
(() => {
  const root=process.env.CODEX_RECORD_SIGNALS||process.env.CODEX_MANAGER_RECORD_SIGNALS;
  if(!root)return;
  const fs=require('node:fs').promises,path=require('node:path');
  const writer=process.env.CODEX_MANAGER_PROFILE_ID||'00000000-0000-4000-8000-000000000001';
  const directory=path.join(root,'project-membership-proofs'),file=path.join(directory,writer+'.json');
  const proven=new Set(),owned=new Set(),wakeups=new Set(),connections=new WeakSet();
  let proofDirty=false,proofRunning=false;
  const registered=new WeakSet(),drainers=new WeakMap(),uuid=/^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i;
  const files=globalThis.__codexSignalFiles.create(directory,{
    accept:n=>n.endsWith('.json')&&uuid.test(n.slice(0,-5)),maxBytes:8*1024*1024});
  const assignmentKey='thread-project-assignments',migrationKey='app-server-projects-migration-by-host';
  function confirm(id){
    if(typeof id!=='string'||!uuid.test(id))return;
    proven.add(id);
    if(!owned.has(id)){owned.add(id);proofDirty=true;}
  }
  async function proofTick(){
    if(proofRunning)return;proofRunning=true;
    try{
      await files.scan((name,data)=>{
          if(data?.version!==1||!Array.isArray(data.thread_ids)||data.thread_ids.length>131072||data.thread_ids.some(id=>typeof id!=='string'||!uuid.test(id)))return false;
          for(const id of data.thread_ids){proven.add(id);if(name===writer+'.json')owned.add(id);}
      });
      if(proofDirty&&owned.size<=131072){
        const size=owned.size,temp=file+'.'+process.pid+'.tmp';
        await fs.writeFile(temp,JSON.stringify({version:1,thread_ids:[...owned].sort()}),'utf8');
        await fs.rename(temp,file);if(owned.size===size)proofDirty=false;
      }
      for(const wake of wakeups)if(!wake())wakeups.delete(wake);
    }catch{/* Keep legacy membership while storage is unavailable; retry later. */}
    finally{proofRunning=false;}
  }
  globalThis.__codexProjectMembership={refresh:backend=>drainers.get(backend)?.(),tick:proofTick,register(backend){
    if(registered.has(backend)||backend.cache.hostId!=='local'||!backend.threadAssignments)return;
    registered.add(backend);
    const nativeWrite=backend.writeThreadAssignment,nativeObserve=backend.observeThreads;
    if(typeof nativeWrite!=='function'||typeof nativeObserve!=='function')return;
    const writing=new Map(),versions=new Map(),pending=new Map(),reading=new Set(),unimported=new Set();
    let draining=false;
    const local=a=>a?.projectKind==='local'&&backend.cache.getProjects()?.[a.projectId]!=null;
    function preserveUnknownNull(id){
      const state=backend.globalState,current=state.getStored(assignmentKey)?.[id];
      const migration=state.getStored(migrationKey)?.[backend.migrationIdentity];
      const legacy=current?.projectKind==='local'&&(current.projectOrigin!=='chatgpt'||local(current));
      // Read migration can finish while individual legacy writes remain pending.
      return legacy&&(migration?.pendingThreadAssignmentIds?.includes(id)||
        (!proven.has(id)&&migration?.threadAssignmentsReadMigrated!==true));
    }
    const nativeAdopt=backend.threadAssignments.adopt;
    backend.threadAssignments.adopt=function(id,projectId){
      if(projectId!=null)confirm(id);
      // A mode switch can observe a NULL before legacy membership has ever
      // reached native storage. It is not evidence of a user removing a task.
      if(projectId===null&&preserveUnknownNull(id)){
        unimported.add(id);return;
      }
      unimported.delete(id);
      return Reflect.apply(nativeAdopt,this,arguments);
    };
    const connection=backend.connection;
    if(!connections.has(connection)){
      connections.add(connection);const request=connection.sendAppServerRequest;
      connection.sendAppServerRequest=async function(method,params){
        const result=await Reflect.apply(request,this,arguments);
        // Success, including an explicit empty projectId, proves a native write.
        // Observations, mode changes and failed RPCs never prove a removal.
        if(method==='thread/metadata/update'&&typeof params?.projectId==='string')confirm(params.threadId);
        return result;
      };
    }
    function committed(id){
      const state=backend.globalState,host=backend.migrationIdentity,migrations=state.getStored(migrationKey)||{};
      const previous=migrations[host];
      if(previous?.pendingThreadAssignmentIds?.includes(id))state.set(migrationKey,{...migrations,[host]:{
        ...previous,pendingThreadAssignmentIds:previous.pendingThreadAssignmentIds.filter(t=>t!==id)}});
    }
    backend.writeThreadAssignment=async function(id,assignment,commit,createdProject,sourceHost){
      const current=backend.globalState.getStored(assignmentKey)?.[id];
      // New/prewarmed tasks use the desktop's creation path. Remote/ChatGPT
      // projects also retain their own native write path.
      if(backend.threadAssignmentsEnabled||!uuid.test(id)||createdProject!==undefined||
          !(local(assignment)||(assignment==null&&(local(current)||sourceHost==='local'))))
        return Reflect.apply(nativeWrite,this,arguments);
      const previous=writing.get(id)||Promise.resolve();
      const operation=previous.catch(()=>{}).then(async()=>{
        await backend.ensureProjectsReady();
        if(backend.disposed||backend.projectSupport!=='supported')throw Error('Local project storage is unavailable.');
        const projectId=assignment==null?'':backend.serverProjectsByLegacyId.get(assignment.projectId)?.id;
        if(projectId===undefined)throw Error('Cannot move a task to an unavailable local project.');
        versions.set(id,(versions.get(id)||0)+1);
        await backend.connection.sendAppServerRequest('thread/metadata/update',{threadId:id,projectId});
        await Reflect.apply(nativeWrite,this,[id,assignment,commit,createdProject,sourceHost]);
        committed(id);
      });
      writing.set(id,operation);
      try{return await operation;}
      finally{if(writing.get(id)===operation){writing.delete(id);pending.delete(id);}}
    };
    async function drain(){
      if(draining||backend.disposed||!pending.size)return;draining=true;
      try{
        await backend.ensureProjectsReady();
        if(backend.disposed||backend.projectSupport!=='supported')return;
        for(const id of [...pending.keys()].slice(0,16)){
          if(writing.has(id)||reading.has(id))continue;
          const failures=pending.get(id)||0;
          pending.delete(id);reading.add(id);const version=versions.get(id)||0;
          try{
            // Read after invalidation instead of adopting a possibly stale
            // thread/list response that raced with a drag/drop operation.
            const {thread}=await backend.connection.sendAppServerRequest('thread/read',{threadId:id,includeTurns:false});
            if(backend.disposed||writing.has(id)||(versions.get(id)||0)!==version)continue;
            if(thread.projectId!==undefined){
              if(thread.projectId!=null&&!backend.legacyProjectIdsByServerId.has(thread.projectId)){
                if(failures<3)pending.set(id,failures+1);continue;
              }
              backend.threadAssignments.adopt(id,thread.projectId);
              if(!unimported.has(id))committed(id);
            }
          }catch{if(failures<2)pending.set(id,failures+1);}
          finally{reading.delete(id);}
        }
      }catch{/* Retry an interrupted connection on a later project refresh. */}
      finally{draining=false;}
    }
    backend.observeThreads=function(threads){
      for(const thread of threads)if(thread.projectId!=null)confirm(thread.id);
      const result=Reflect.apply(nativeObserve,this,arguments);
      if(backend.threadAssignmentsEnabled||backend.disposed)return result;
      for(const thread of threads){
        if(!uuid.test(thread.id)||thread.projectId===undefined||writing.has(thread.id)||reading.has(thread.id))continue;
        if(thread.projectId===null&&unimported.has(thread.id)&&preserveUnknownNull(thread.id))continue;
        if(backend.threadAssignments.matches(thread.id,thread.projectId))continue;
        pending.set(thread.id,0);
      }
      while(pending.size>1024)pending.delete(pending.keys().next().value);
      if(pending.size)void drain();
      return result;
    };
    drainers.set(backend,drain);
    wakeups.add(()=>{
      if(backend.disposed)return false;
      for(const id of unimported)if(!preserveUnknownNull(id)){unimported.delete(id);pending.set(id,0);}
      if(pending.size)void drain();
      return true;
    });
    void proofTick();
  }};
  setInterval(proofTick,700).unref();
})();
