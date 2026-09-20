// Shared SSH project declarations. Native state owns persistence and renderer
// notifications; selections, drafts, authentication and explicit connection toggles stay local.
(() => {
  const root=process.env.CODEX_RECORD_SIGNALS||process.env.CODEX_MANAGER_RECORD_SIGNALS;
  if(!root)return;
  const fs=require('node:fs').promises,path=require('node:path');
  const writer=process.env.CODEX_MANAGER_PROFILE_ID||'00000000-0000-4000-8000-000000000001';
  const directory=path.join(root,'workspaces'),file=path.join(directory,writer+'.json');
  const uuid=/^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i;
  const files=globalThis.__codexSignalFiles.create(directory,{
    accept:n=>n.endsWith('.json')&&uuid.test(n.slice(0,-5))});
  const key='remote-projects', records=new Map(), owned=new Map();
  const sshKey='codex-managed-remote-connections',autoKey='remote-connection-auto-connect-by-host-id';
  const managedHome=process.env.CODEX_MANAGER_PROFILE_ID&&process.env.CODEX_HOME;
  const sourceHome=path.join(require('node:os').homedir(),'.codex');
  let store,windows,remoteConnections,subscriptions=[],last=new Map(),clock=0,ready=false,applying=false,dirty=false,running=false,epoch=0;
  let sshDirty=true,healingSsh=false;
  let sshRefreshPending=false,sshRefreshRunning=false,sshRefreshAfter=0,sshRefreshRevision=0;
  const valid=p=>p&&uuid.test(p.id)&&typeof p.hostId==='string'&&p.hostId.startsWith('remote-ssh-')&&
    p.hostId.length<=256&&typeof p.remotePath==='string'&&p.remotePath.startsWith('/')&&p.remotePath.length<=4096&&
    typeof p.label==='string'&&p.label.length<=512;
  const clean=p=>({id:p.id,hostId:p.hostId,remotePath:p.remotePath,label:p.label});
  const snapshot=()=>new Map((store.getStored(key)||[]).filter(valid).map(p=>[p.id,clean(p)]));
  const same=(a,b)=>JSON.stringify(a)===JSON.stringify(b);
  const object=value=>value!==null&&typeof value==='object'&&!Array.isArray(value);
  async function readSettings(file){
    const info=await fs.lstat(file);
    if(!info.isFile()||info.size>16*1024*1024)throw Error('Invalid saved declarations');
    return JSON.parse(await fs.readFile(file,'utf8'));
  }
  async function healSshConnections(generation){
    if(!sshDirty||!managedHome||path.resolve(managedHome)===path.resolve(sourceHome))return;
    sshDirty=false;
    try{
      const [metadata,donor]=await Promise.all([
        readSettings(path.join(managedHome,'.manager-app-preferences.json')),
        readSettings(path.join(sourceHome,'.codex-global-state.json'))]);
      if(generation!==epoch)return;
      const previous=metadata?.workspace?.[sshKey],declared=donor?.[sshKey];
      if(!object(previous)||!Array.isArray(declared)||declared.length>4096)return;
      // Re-read the native state after file I/O; concurrent edits and explicit
      // False preferences belong to this profile. Never import donor analytics.
      const current=store.getStored(sshKey)??[],auto=store.getStored(autoKey)??{};
      if(!Array.isArray(current)||!object(auto))return;
      const present=new Set(current.map(item=>item?.hostId)),additions=[],autoAdditions={};
      for(const item of declared){
        if(!object(item)||typeof item.hostId!=='string'||!item.hostId.startsWith('remote-ssh-')||
          item.hostId.length>256||!Object.hasOwn(previous,item.hostId)||item.source!=='discovered'||
          typeof item.alias!=='string'||!item.alias.trim()||item.hostname!=null)continue;
        if(!present.has(item.hostId)){
          additions.push(Object.fromEntries(['hostId','displayName','source','alias','hostname','sshPort','identity']
            .map(field=>[field,item[field]??null])));
          present.add(item.hostId);
        }
        const enabled=donor[autoKey]?.[item.hostId];
        if(!Object.hasOwn(auto,item.hostId)&&typeof enabled==='boolean')autoAdditions[item.hostId]=enabled;
      }
      const keys=[];
      healingSsh=true;
      try{
        if(additions.length){store.set(sshKey,[...current,...additions]);keys.push(sshKey);}
        if(Object.keys(autoAdditions).length){store.set(autoKey,{...auto,...autoAdditions});keys.push(autoKey);}
      }finally{healingSsh=false;}
      if(keys.length){
        sshRefreshPending=true;sshRefreshRevision++;
        windows.sendMessageToAllWindows({type:'global-state-updated',keys});
      }
    }catch{if(generation===epoch)sshDirty=true;}
  }
  function refreshSshConnections(){
    // Native cache refresh can fail after the settings were successfully saved.
    // Retain that work independently of further edits, without blocking project
    // synchronization or overlapping retries. A late handler can fulfill it.
    if(!sshRefreshPending||sshRefreshRunning||Date.now()<sshRefreshAfter||
      typeof remoteConnections?.refreshRemoteConnections!=='function')return;
    const generation=epoch,revision=sshRefreshRevision,handler=remoteConnections;
    sshRefreshRunning=true;
    Promise.resolve().then(()=>handler.refreshRemoteConnections()).then(()=>{
      if(generation===epoch&&revision===sshRefreshRevision&&handler===remoteConnections)sshRefreshPending=false;
    },()=>{}).finally(()=>{sshRefreshRunning=false;sshRefreshAfter=Date.now()+1000;});
  }
  function change(){
    if(applying||!ready)return;
    const next=snapshot();
    for(const id of new Set([...last.keys(),...next.keys()]))if(!same(last.get(id),next.get(id))){
      const row=[id,++clock,writer,next.get(id)||null];owned.set(id,row);records.set(id,row);dirty=true;
    }
    last=next;
  }
  async function tick(){
    if(!store||running)return;running=true;
    const generation=epoch;
    try{
      await healSshConnections(generation);
      if(generation!==epoch)return;
      refreshSshConnections();
      await files.scan((name,data)=>{
        if(data?.version!==1||!Array.isArray(data.projects)||data.projects.length>4096)return false;
        for(const row of data.projects){
          if(!Array.isArray(row)||row.length!==4)continue;
          const [id,seq,author,value]=row;
          if(!uuid.test(id)||!uuid.test(author)||!Number.isSafeInteger(seq)||seq<1||
            (value!==null&&(!valid(value)||value.id!==id)))continue;
          clock=Math.max(clock,seq);
          const old=records.get(id);
          if(!old||seq>old[1]||(seq===old[1]&&author>old[2]))records.set(id,row);
          if(author===writer&&(!owned.has(id)||seq>owned.get(id)[1]))owned.set(id,row);
        }
      });
      if(generation!==epoch)return;
      if(!ready){
        // Seed pre-upgrade declarations only when no shared event/tombstone exists.
        for(const [id,value] of snapshot())if(!records.has(id)){
          const row=[id,++clock,writer,value];records.set(id,row);owned.set(id,row);dirty=true;
        }
        ready=true;
      }
      const merged=[...records.values()].filter(r=>r[3]!==null).map(r=>r[3]);
      const before=snapshot(),next=new Map(merged.map(p=>[p.id,p]));
      if(before.size!==next.size||[...next].some(([id,p])=>!same(before.get(id),p))){
        applying=true;
        try{
          store.set(key,merged);
          windows.sendMessageToAllWindows({type:'global-state-updated',keys:[key]});
          windows.sendMessageToAllWindows({type:'workspace-root-options-updated'});
        }finally{applying=false;}
      }
      // Native project migration can replace the order after the declarations
      // arrived. Repair the order independently of project content changes.
      const removed=new Set([...records.values()].filter(r=>r[3]===null).map(r=>r[0]));
      const previous=store.getStored('project-order')||[];
      const order=previous.filter(id=>!removed.has(id));
      for(const p of merged)if(!order.includes(p.id))order.push(p.id);
      if(!same(previous,order)){
        store.set('project-order',order);
        windows.sendMessageToAllWindows({type:'global-state-updated',keys:['project-order']});
      }
      last=snapshot();
      if(dirty){
        const serial=clock,temporary=file+'.'+process.pid+'.tmp';
        await fs.writeFile(temporary,JSON.stringify({version:1,projects:[...owned.values()]}),'utf8');
        await fs.rename(temporary,file);if(clock===serial)dirty=false;
      }
    }catch{ /* Retry transient sharing violations without blocking the editor. */ }
    finally{running=false;}
  }
  globalThis.__codexWorkspaceSync={register(s,w,connections){
    if(store===s){if(connections)remoteConnections=connections;return;}
    remoteConnections=connections;sshRefreshPending=false;sshRefreshRevision++;sshRefreshAfter=0;
    for(const stop of subscriptions)stop?.();epoch++;store=s;windows=w;ready=false;last=snapshot();sshDirty=true;
    const sshChanged=()=>{if(!healingSsh)sshDirty=true;};
    subscriptions=[s.onDidChange(key,change),s.onDidChange(sshKey,sshChanged),s.onDidChange(autoKey,sshChanged)];void tick();
  },tick};
  const timer=setInterval(tick,700);timer.unref();
})();
