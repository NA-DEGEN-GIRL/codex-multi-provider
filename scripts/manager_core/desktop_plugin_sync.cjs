// Shared plugin/skill refresh. The plugin worker publishes one monotonic
// revision after its local bundles and config commit; each local desktop then
// re-scans through its own app-server and lets the native skills/changed
// subscriber invalidate the renderer's skills/plugins queries. No navigation,
// focus, window reload, extra skill roots or plugin reconciliation.
(() => {
  const root=process.env.CODEX_RECORD_SIGNALS||process.env.CODEX_MANAGER_RECORD_SIGNALS;
  if(!root)return;
  const fs=require('node:fs').promises,path=require('node:path');
  const file=path.join(root,'plugins.json');
  const POLL=800,MAX_BYTES=4096,MAX_BACKOFF=30000,PER_TICK=4,REQUEST_TIMEOUT=15000,MAX_RETRIES=6;
  const managers=new Set(),applied=new WeakMap(),schedules=new Map(),running=new WeakSet();
  let revision=null,stamp=null,malformed=null,malformedTries=0,ticking=false,stopped=false,lastError=null;
  const counters={scans:0,revisions:0,reloads:0,invalidations:0,deferred:0,failures:0,skipped:0};
  const validRevision=value=>(typeof value==='string'&&value.length>0&&value.length<=512)||
    (typeof value==='number'&&Number.isFinite(value));
  const backoff=failures=>Math.min(MAX_BACKOFF,1000*2**Math.min(Math.max(failures,1)-1,5));
  const fail=error=>{counters.failures++;lastError=String(error?.message||error).slice(0,200);};
  const keep=(manager,entry)=>{const current=schedules.get(manager);if(current===entry||!current)schedules.set(manager,entry);};
  const drop=(manager,entry)=>{if(schedules.get(manager)===entry)schedules.delete(manager);};
  function bounded(promise,ms){
    let timer;
    const timeout=new Promise((resolve,reject)=>{
      timer=setTimeout(()=>reject(Error('skills/list timed out')),ms);
      if(typeof timer?.unref==='function')timer.unref();
    });
    return Promise.race([promise,timeout]).finally(()=>clearTimeout(timer)).catch(error=>{
      promise.catch(()=>{});throw error;
    });
  }
  async function scan(){
    counters.scans++;
    let info;
    try{info=await fs.stat(file);}
    catch(error){if(error.code==='ENOENT')return revision;throw error;}
    if(!info.isFile()||info.size>MAX_BYTES)return revision;
    const current=info.mtimeMs+':'+info.size;
    if(current===stamp)return revision;
    let data;
    try{data=JSON.parse(await fs.readFile(file,'utf8'));}
    catch{
      // A partially observed write is retried a bounded number of times, then
      // the same file version is ignored until it changes.
      malformedTries=malformed===current?malformedTries+1:1;
      malformed=current;
      if(malformedTries>=3)stamp=current;
      return revision;
    }
    malformed=null;malformedTries=0;stamp=current;
    if(data?.version!==1||!validRevision(data.revision))return revision;
    const value=String(data.revision);
    if(value!==revision){revision=value;counters.revisions++;}
    return revision;
  }
  function invalidate(){
    // React-query subscribers live in the renderer's own app-server manager,
    // so the main process hands the invalidation to each window instead of
    // synthesizing a runtime notification here. A windowless attempt is not a
    // failure: the revision stays pending until a window can receive it.
    let windows;
    try{windows=require('electron').BrowserWindow.getAllWindows();}
    catch(error){fail(error);return 'failed';}
    if(!windows.length)return 'no-window';
    let delivered=0;
    for(const win of windows){
      try{
        if(!win.isDestroyed()&&!win.webContents.isDestroyed()){
          win.webContents.send('codex_desktop:message-for-view',{type:'manager-plugins-invalidated',hostId:'local'});
          delivered++;
        }
      }catch(error){fail(error);}
    }
    return delivered?'delivered':'failed';
  }
  async function attempt(manager,entry){
    if(running.has(manager))return;running.add(manager);
    try{
      if(manager.disposed){managers.delete(manager);drop(manager,entry);return;}
      if(!entry.loaded){
        const client=manager.requestClient;
        if(typeof client?.sendRequest!=='function'){drop(manager,entry);counters.skipped++;return;}
        // Native `skills/list` with forceReload clears the plugin cache and the
        // skill cache, then re-reads the latest config for the session cwd.
        // Never skills/extraRoots/set or plugin/reconcile: those replace roots.
        // A bounded background request cannot strand the scheduler on a
        // disconnected or replaced runtime.
        await bounded(client.sendRequest('skills/list',{cwds:[],forceReload:true},
          {priority:'background',timeoutMs:REQUEST_TIMEOUT}),REQUEST_TIMEOUT+1000);
        if(manager.disposed){managers.delete(manager);drop(manager,entry);return;}
        entry.loaded=true;counters.reloads++;
      }
      if(!entry.delivered){
        const outcome=invalidate();
        if(outcome!=='delivered'){
          if(outcome==='no-window'){
            // Retry on the next poll without burning the failure budget and
            // without repeating the native reload.
            if(!entry.deferred){entry.deferred=true;counters.deferred++;}
            entry.due=Date.now();
          }else{
            fail(Error('renderer invalidation failed'));
            entry.failures+=1;
            if(entry.failures===MAX_RETRIES)counters.deferred++;
            entry.due=Date.now()+backoff(entry.failures);
          }
          keep(manager,entry);
          return;
        }
        entry.delivered=true;
        counters.invalidations++;
      }
      applied.set(manager,entry.revision);
      drop(manager,entry);
    }catch(error){
      fail(error);
      entry.failures=(entry.failures||0)+1;
      if(entry.failures===MAX_RETRIES)counters.deferred++;
      // Retries stay capped and scheduled: a disconnected or repeatedly
      // failing app is never burst-retried, and the revision stays pending.
      entry.due=Date.now()+backoff(entry.failures);
      if(!manager.disposed)keep(manager,entry);
    }finally{running.delete(manager);}
  }
  async function tick(){
    if(ticking||stopped)return;ticking=true;
    try{
      for(const manager of managers)if(manager.disposed){managers.delete(manager);schedules.delete(manager);}
      const current=await scan();
      if(current===null||!managers.size)return;
      for(const manager of managers){
        if(applied.get(manager)===current){schedules.delete(manager);continue;}
        const entry=schedules.get(manager);
        if(!entry||entry.revision!==current)
          schedules.set(manager,{revision:current,due:0,failures:0,loaded:false,delivered:false});
      }
      let started=0;
      for(const [manager,entry] of [...schedules]){
        if(started>=PER_TICK)break;
        if(entry.revision!==current||entry.due>Date.now())continue;
        started++;void attempt(manager,entry);
      }
    }catch(error){
      fail(error);
    }finally{ticking=false;}
  }
  globalThis.__codexPluginSync={
    register(manager){
      if(stopped||manager?.disposed||manager?.hostId!=='local'||managers.has(manager))return;
      managers.add(manager);
      schedules.set(manager,{revision:revision,due:0,failures:0,loaded:false,delivered:false});
      void tick();
    },
    tick,
    status(){return{...counters,managers:managers.size,pending:schedules.size,revision,error:lastError};},
    stop(){stopped=true;clearInterval(timer);}
  };
  const timer=setInterval(tick,POLL);timer.unref();
})();
