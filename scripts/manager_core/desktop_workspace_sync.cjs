// Shared SSH project declarations. Native state owns persistence and renderer
// notifications; selections, drafts, authentication and connection toggles stay local.
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
  let store,windows,unsubscribe,last=new Map(),clock=0,ready=false,applying=false,dirty=false,running=false,epoch=0;
  const valid=p=>p&&uuid.test(p.id)&&typeof p.hostId==='string'&&p.hostId.startsWith('remote-ssh-')&&
    p.hostId.length<=256&&typeof p.remotePath==='string'&&p.remotePath.startsWith('/')&&p.remotePath.length<=4096&&
    typeof p.label==='string'&&p.label.length<=512;
  const clean=p=>({id:p.id,hostId:p.hostId,remotePath:p.remotePath,label:p.label});
  const snapshot=()=>new Map((store.getStored(key)||[]).filter(valid).map(p=>[p.id,clean(p)]));
  const same=(a,b)=>JSON.stringify(a)===JSON.stringify(b);
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
  globalThis.__codexWorkspaceSync={register(s,w){
    if(store===s)return;
    unsubscribe?.();epoch++;store=s;windows=w;ready=false;last=snapshot();
    unsubscribe=s.onDidChange(key,change);void tick();
  },tick};
  const timer=setInterval(tick,700);timer.unref();
})();
