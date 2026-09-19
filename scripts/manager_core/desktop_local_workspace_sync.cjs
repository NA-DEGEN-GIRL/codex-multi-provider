// Local project declarations share native project IDs, not a privileged donor
// profile. Tombstones prevent a stale/reopened profile from restoring removals.
(() => {
  const root=process.env.CODEX_RECORD_SIGNALS||process.env.CODEX_MANAGER_RECORD_SIGNALS;
  if(!root)return;
  const fs=require('node:fs').promises,path=require('node:path');
  const writer=process.env.CODEX_MANAGER_PROFILE_ID||'00000000-0000-4000-8000-000000000001';
  const directory=path.join(root,'local-workspaces'),file=path.join(directory,writer+'.json');
  const home=process.env.CODEX_HOME||path.join(require('node:os').homedir(),'.codex'),host='local:'+home;
  const key='local-projects',mapKey='app-server-project-id-by-legacy-project-id-by-host';
  const uuid=/^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i;
  const files=globalThis.__codexSignalFiles.create(directory,{
    accept:n=>n.endsWith('.json')&&uuid.test(n.slice(0,-5))});
  const identity=id=>typeof id==='string'&&(uuid.test(id)||/^local-[a-f0-9]{32}$/i.test(id));
  const valid=p=>p&&identity(p.id)&&typeof p.name==='string'&&p.name.length<=512&&
    Array.isArray(p.rootPaths)&&p.rootPaths.length<=128&&p.rootPaths.every(r=>typeof r==='string'&&r.length<=4096)&&
    (p.serverId==null||uuid.test(p.serverId));
  const clean=p=>({id:p.id,name:p.name,rootPaths:p.rootPaths,...Number.isFinite(p.createdAt)?{createdAt:p.createdAt}:{},
    ...Number.isFinite(p.updatedAt)?{updatedAt:p.updatedAt}:{},...p.serverId?{serverId:p.serverId}:{}});
  const same=(a,b)=>JSON.stringify(a)===JSON.stringify(b);
  const records=new Map(),owned=new Map(),backends=new Map();
  let store,windows,subscriptions=[],last=new Map(),clock=0,ready=false,applying=false,dirty=false,running=false,epoch=0,revision=0;
  function snapshot(){
    const mapping=store.getStored(mapKey)?.[host]||{};
    return new Map(Object.values(store.getStored(key)||{}).filter(valid).map(p=>[p.id,clean({...p,serverId:mapping[p.id]})]));
  }
  function change(){
    if(applying||!ready)return;
    const next=snapshot();
    for(const id of new Set([...last.keys(),...next.keys()]))if(!same(last.get(id),next.get(id))){
      const row=[id,++clock,writer,next.get(id)||null];owned.set(id,row);records.set(id,row);dirty=true;revision++;
    }
    last=next;
  }
  async function refreshBackends(){
    for(const [backend,applied] of backends){
      if(backend.disposed){backends.delete(backend);continue;}
      void globalThis.__codexProjectMembership?.refresh(backend);
      if(applied===revision||!backend.projectsReady||!backend.connected||backend.pendingProjectWrites.size)continue;
      const serial=revision,signal=backend.connectionLifetime.signal;
      // Read only: peer operations already wrote the common native database.
      // Re-running initializeProjects would import old deleted declarations.
      const projects=await backend.listProjects(signal);
      if(backend.disposed||backend.pendingProjectWrites.size||revision!==serial)continue;
      const mapping=store.getStored(mapKey)?.[backend.migrationIdentity]||{};
      backend.legacyProjectIdsByServerId.clear();
      for(const [legacy,native] of Object.entries(mapping))backend.legacyProjectIdsByServerId.set(native,legacy);
      backend.serverProjectsByLegacyId.clear();
      for(const p of projects)backend.storeProject(p);
      backends.set(backend,serial);
    }
  }
  async function tick(){
    if(!store||running)return;running=true;const generation=epoch;
    try{
      await files.scan((name,data)=>{
          if(data?.version!==1||!Array.isArray(data.projects)||data.projects.length>4096)return false;
          for(const row of data.projects){
            if(!Array.isArray(row)||row.length!==4)continue;
            const [id,seq,author,value]=row;
            if(!identity(id)||!uuid.test(author)||!Number.isSafeInteger(seq)||seq<1||
              (value!==null&&(!valid(value)||value.id!==id)))continue;
            clock=Math.max(clock,seq);const previous=records.get(id);
            if(!previous||seq>previous[1]||(seq===previous[1]&&author>previous[2])){records.set(id,row);revision++;}
            if(author===writer&&(!owned.has(id)||seq>owned.get(id)[1]))owned.set(id,row);
          }
      });
      if(generation!==epoch)return;
      if(!ready){
        const nativeIds=new Set([...records.values()].map(r=>r[3]?.serverId).filter(Boolean));
        for(const [id,p] of snapshot())if(!records.has(id)&&!nativeIds.has(p.serverId)){
          const row=[id,++clock,writer,p];records.set(id,row);owned.set(id,row);dirty=true;revision++;
        }
        ready=true;
      }
      applying=true;
      try{
        const current=store.getStored(key)||{},next={...current};
        const maps=store.getStored(mapKey)||{},mapping={...maps[host]};
        const removed=new Set(),canonical=new Map();
        for(const [id,,,p] of records.values())if(p?.serverId)canonical.set(p.serverId,id);
        for(const [id,p] of snapshot())if(p.serverId&&canonical.has(p.serverId)&&canonical.get(p.serverId)!==id){delete next[id];removed.add(id);}
        for(const [id,,,p] of records.values()){
          if(p===null){delete next[id];removed.add(id);continue;}
          const {serverId,...project}=p;next[id]=project;if(serverId)mapping[id]=serverId;
        }
        const keys=[];
        if(!same(maps[host]||{},mapping)){store.set(mapKey,{...maps,[host]:mapping});keys.push(mapKey);}
        if(!same(current,next)){store.set(key,next);keys.push(key);}
        const previous=store.getStored('project-order')||[],order=previous.filter(id=>!removed.has(id));
        for(const id of Object.keys(next))if(!order.includes(id))order.push(id);
        if(!same(previous,order)){store.set('project-order',order);keys.push('project-order');}
        if(keys.length){windows.sendMessageToAllWindows({type:'global-state-updated',keys});windows.sendMessageToAllWindows({type:'workspace-root-options-updated'});}
        last=snapshot();
      }finally{applying=false;}
      if(dirty){
        const serial=clock,temp=file+'.'+process.pid+'.tmp';
        await fs.writeFile(temp,JSON.stringify({version:1,projects:[...owned.values()]}),'utf8');
        await fs.rename(temp,file);if(serial===clock)dirty=false;
      }
      await refreshBackends();
    }catch{/* Retry sharing violations without blocking input. */}
    finally{running=false;}
  }
  globalThis.__codexLocalWorkspaceSync={register(s,w){
    if(store===s)return;for(const stop of subscriptions)stop?.();
    epoch++;store=s;windows=w;ready=false;last=snapshot();
    subscriptions=[s.onDidChange(key,change),s.onDidChange(mapKey,change)];void tick();
  },registerBackend(b){if(b.cache.hostId==='local'&&!b.disposed&&!backends.has(b)){
    backends.set(b,-1);globalThis.__codexProjectMembership?.register(b);
  }},tick};
  setInterval(tick,700).unref();
})();
