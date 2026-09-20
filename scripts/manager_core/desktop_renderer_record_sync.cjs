// The renderer owns the visible transcript. The main-process catalog is a
// separate store: refreshing it alone never invalidates this store's readers.
(() => {
  const managers = new Set(), local = new WeakMap(), refreshing = new WeakMap();
  const pending = new Map(), uuid = /^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i;
  const deletedKeys=new Set();
  const archivedKeys=new Set();
  const counters = {refreshes:0, failures:0, unavailable:0, deferred:0, draftDeferred:0,
    ipcMessages:0,ipcCoalesced:0};
  const signals=new Set(['thread/started','thread/name/updated','thread/settings/updated','thread/project/updated','thread/archived','thread/unarchived','thread/deleted','turn/started','turn/completed','item/started','item/completed','item/agentMessage/delta']);
  let running = false;
  let composing = false;
  let inputBusyUntil = 0;
  // Passive, bounded timing samples only. No keys, input text, DOM snapshots,
  // task IDs or URLs leave the renderer through this diagnostic interface.
  const inputSamples=[], longTasks=[], inputObservers=[];
  let eventTiming=false;
  const trimSamples=(samples,now)=>{
    while(samples.length && (samples.length>256 || samples[0].at<now-30000))samples.shift();
  };
  const inputHealth=()=>{
    const now=globalThis.performance?.now?.() ?? 0;
    trimSamples(inputSamples,now);trimSamples(longTasks,now);
    const durations=inputSamples.map(s=>s.duration).sort((a,b)=>a-b);
    return {event_timing_supported:eventTiming,window_ms:30000,
      slow_input_samples:inputSamples.length,
      input_delay_max_ms:Math.round(Math.max(0,...inputSamples.map(s=>s.delay))),
      input_duration_p95_ms:Math.round(durations[Math.max(0,Math.ceil(durations.length*.95)-1)]||0),
      input_duration_max_ms:Math.round(Math.max(0,...durations)),
      long_task_samples:longTasks.length,long_task_max_ms:Math.round(Math.max(0,...longTasks.map(s=>s.duration)))};
  };
  if(typeof PerformanceObserver==='function'){
    const observe=(type,receive,options={})=>{
      if(!PerformanceObserver.supportedEntryTypes?.includes(type))return false;
      try{const observer=new PerformanceObserver(list=>receive(list.getEntries()));
        observer.observe({type,...options});inputObservers.push(observer);return true;
      }catch{return false;}
    };
    eventTiming=observe('event',entries=>{
      for(const e of entries){
        if(document.visibilityState==='hidden' || !['keydown','keyup','beforeinput','input','compositionstart','compositionupdate','compositionend'].includes(e.name) ||
          !e.target?.closest?.('[data-codex-composer]'))continue;
        const delay=e.processingStart-e.startTime;
        if(!Number.isFinite(delay)||!Number.isFinite(e.duration)||!Number.isFinite(e.startTime))continue;
        inputSamples.push({at:e.startTime,delay:Math.max(0,delay),duration:Math.max(0,e.duration)});
      }
      trimSamples(inputSamples,performance.now());
    },{durationThreshold:16});
    observe('longtask',entries=>{
      if(document.visibilityState==='hidden')return;
      for(const e of entries)if(Number.isFinite(e.startTime)&&Number.isFinite(e.duration))longTasks.push({at:e.startTime,duration:e.duration});
      trimSamples(longTasks,performance.now());
    });
    window.addEventListener('pagehide',()=>{for(const observer of inputObservers)observer.disconnect();},{once:true});
  }
  let refreshTimer, deltaTimer, cleanupTimer;
  const deltaPending=new Map(), lastPublished=new Map(), DELTA_INTERVAL=200;
  const post = (id,host,kind='changed') => {
    window.electronBridge?.sendMessageFromView?.({type:'manager-record-changed',threadId:id,hostId:host,kind});
    counters.ipcMessages++;
    const key=host+'\0'+id;
    lastPublished.delete(key);lastPublished.set(key,Date.now());
    while(lastPublished.size>512)lastPublished.delete(lastPublished.keys().next().value);
  };
  function scheduleDeltas(){
    if(deltaTimer!==undefined||!deltaPending.size)return;
    deltaTimer=setTimeout(()=>{
      deltaTimer=undefined;
      for(const [key,{id,host}] of deltaPending){
        if(deletedKeys.has(key)||archivedKeys.has(key)){deltaPending.delete(key);continue;}
        if(Date.now()-(lastPublished.get(key)??-Infinity)<DELTA_INTERVAL)continue;
        deltaPending.delete(key);post(id,host);
      }
      scheduleDeltas();
    },DELTA_INTERVAL);
  }
  function publish(m,id,method){
    const host=m.hostId,key=host+'\0'+id;
    if(method==='item/agentMessage/delta'){
      if(deletedKeys.has(key)||archivedKeys.has(key))return;
      if(Date.now()-(lastPublished.get(key)??-Infinity)<DELTA_INTERVAL){
        deltaPending.set(key,{id,host});counters.ipcCoalesced++;
        if(deltaPending.size>512){
          const oldest=deltaPending.keys().next().value,entry=deltaPending.get(oldest);
          deltaPending.delete(oldest);post(entry.id,entry.host);
        }
        scheduleDeltas();return;
      }
    }
    deltaPending.delete(key);
    if(!deltaPending.size&&deltaTimer!==undefined){clearTimeout(deltaTimer);deltaTimer=undefined;}
    post(id,host,({'thread/deleted':'deleted','thread/archived':'archived','thread/unarchived':'unarchived'})[method]||'changed');
  }
  function scheduleRefresh(){
    if(!pending.size||document.visibilityState==='hidden'){
      if(refreshTimer!==undefined){clearTimeout(refreshTimer);refreshTimer=undefined;}
      return;
    }
    if(refreshTimer!==undefined||running)return;
    const due=Math.min(...[...pending.values()].map(state=>state.due));
    refreshTimer=setTimeout(()=>{refreshTimer=undefined;void tick();},Math.max(400,due-Date.now()));
  }
  const liveManagers = () => {
    for(const m of managers)if(m.disposed)managers.delete(m);
    return managers;
  };
  function scheduleCleanup(){
    if(cleanupTimer!==undefined||!managers.size)return;
    // Some native owners mark themselves disposed without calling dispose().
    // Keep a cheap, coarse fallback so idle transcript graphs are released too.
    cleanupTimer=setTimeout(()=>{cleanupTimer=undefined;liveManagers();scheduleCleanup();},30000);
  }
  const hasDraft = id => {
    // Native transcript hydration can recreate the composer when its latest
    // turn changes. Defer that task's merge while the user owns an unsent draft;
    // do not copy/restore DOM text, which loses mentions, selections and IME.
    if(composing || Date.now()<inputBusyUntil)return true;
    const editor=document.querySelector('[data-codex-composer]');
    return !!editor?.textContent?.trim();
  };
  const active = (m,id) => local.get(m)?.turns.has(id) || local.get(m)?.requests.has(id) || m.hasInFlightConversationResume(id);
  async function tick() {
    liveManagers();
    // Hidden profiles keep invalidations, not repeated transcript hydration.
    // One visibility transition drains the latest state without dropping tasks.
    if(running || !pending.size || document.visibilityState==='hidden'){scheduleRefresh();return;}
    running = true;
    try {
      for(const [key,state] of [...pending].filter(([,s])=>s.due<=Date.now()).slice(0,8)) {
        const {id,host,deleted}=state;
        if(deleted){for(const m of liveManagers())if(m.hostId===host)m.handleThreadDeletion([id]);pending.delete(key);continue;}
        let deferred=false, failed=false, applied=false;
        if(hasDraft(id)){state.due=Date.now()+400;counters.draftDeferred++;continue;}
        for(const m of liveManagers()) {
          if(m.disposed || m.hostId!==host || !m.threadStore) continue;
          if(active(m,id)){deferred=true;counters.deferred++;continue;}
          const store=m.threadStore;
          refreshing.set(store,{m,id});
          try {
            store.backgroundThreadLookups?.delete(id);
            store.threadReadStates?.delete(id);
            await store.hydrateThreads([id],{addToRecentConversations:true,includeTurns:true,maxTurns:8,
              retainHistoryPagination:true,notifyAnyCallbacks:true,throwOnReadError:true});
            if(active(m,id)||hasDraft(id)){deferred=true;continue;}
            applied=true;counters.refreshes++;
          } catch(error) { failed=true;if(/thread not loaded|thread.*not found/i.test(String(error?.message||error)))counters.unavailable++;else counters.failures++; }
          finally {refreshing.delete(store);}
        }
        if(pending.get(key)!==state)continue;
        state.due=Date.now()+(failed?2500:700);
        if(failed){if(++state.failures>=3)pending.delete(key);}
        else if(applied&&!deferred)pending.delete(key);
      }
    } finally {running=false;scheduleRefresh();}
  }
  globalThis.__codexRendererRecordSync = {
    register(m) {
      liveManagers();
      if(m.disposed||managers.has(m))return;
      managers.add(m);local.set(m,{turns:new Map(),requests:new Map()});
      scheduleCleanup();
      const dispose=m.dispose;
      if(typeof dispose==='function')m.dispose=function(...args){
        try{return Reflect.apply(dispose,this,args);}
        finally{managers.delete(m);local.delete(m);}
      };
      const client=m.requestClient,send=client.sendRequest;
      client.sendRequest=async function(method,params,...rest){
        if(globalThis.__codexProfileResume)params=await globalThis.__codexProfileResume(m,send,this,method,params);
        const id=params?.threadId,track=uuid.test(id||'')&&(method==='turn/start'||method==='turn/steer');
        const state=local.get(m);
        if(track)state.requests.set(id,(state.requests.get(id)||0)+1);
        try{return await Reflect.apply(send,this,[method,params,...rest]);}
        finally{if(track){const count=state.requests.get(id)-1;if(count)state.requests.set(id,count);else state.requests.delete(id);}}
      };
    },
    observe(m,method,params) {
      const state=local.get(m),id=params?.thread?.id||params?.threadId,turn=params?.turn?.id||params?.turnId;
      if(!state||!uuid.test(id||''))return;
      if(method==='thread/deleted'){deletedKeys.add(m.hostId+'\0'+id);pending.delete(m.hostId+'\0'+id);}
      if(method==='thread/archived'){archivedKeys.add(m.hostId+'\0'+id);pending.delete(m.hostId+'\0'+id);}
      if(method==='thread/unarchived')archivedKeys.delete(m.hostId+'\0'+id);
      if(signals.has(method))publish(m,id,method);
      if(['turn/started','item/started','item/agentMessage/delta'].includes(method))state.turns.set(id,turn||state.turns.get(id)||null);
      if(method==='turn/completed'&&(!turn||state.turns.get(id)===turn))state.turns.delete(id);
      if(['thread/closed','thread/deleted'].includes(method)||(method==='thread/status/changed'&&params?.status?.type==='idle'))state.turns.delete(id);
    },
    canApply(store){const r=refreshing.get(store);return !r||(!deletedKeys.has(r.m.hostId+'\0'+r.id)&&!archivedKeys.has(r.m.hostId+'\0'+r.id)&&!active(r.m,r.id)&&!hasDraft(r.id));},
    status(){return {...counters,managers:liveManagers().size,pending:pending.size,
      pendingNotifications:deltaPending.size,refreshScheduled:refreshTimer!==undefined,
      archived:archivedKeys.size,deleted:deletedKeys.size};},
    inputHealth,tick
  };
  window.addEventListener('compositionstart',event=>{if(event.target?.closest?.('[data-codex-composer]'))composing=true;});
  window.addEventListener('compositionend',()=>{if(composing)inputBusyUntil=Date.now()+250;composing=false;});
  // Reserve the first keystroke and deletion-to-empty too, before the editor's
  // DOM has a draft to inspect. Never intercept keys or inspect input contents.
  window.addEventListener('beforeinput',event=>{
    if(event.target?.closest?.('[data-codex-composer]'))inputBusyUntil=Date.now()+250;
  },{capture:true,passive:true});
  document.addEventListener?.('visibilitychange',()=>{scheduleRefresh();if(document.visibilityState!=='hidden')void tick();});
  window.addEventListener('message',event=>{
    const data=event.data;
    if(data?.type!=='manager-record-invalidated'||!Array.isArray(data.threadIds))return;
    // The main-process adapter already retries durable reads three times. Each
    // successful delivery needs one renderer merge, not another three reads.
    // A new state object preserves invalidations arriving during an in-flight read.
    const host=data.hostId||'local';
    if(typeof host!=='string'||host.length>256)return;
    for(const id of (data.archivedThreadIds||[]).slice(0,256))if(uuid.test(id)){
      archivedKeys.add(host+'\0'+id);pending.delete(host+'\0'+id);
      for(const m of liveManagers())if(m.hostId===host)m.handleThreadArchived(id);
    }
    for(const id of (data.unarchivedThreadIds||[]).slice(0,256))if(uuid.test(id)&&!deletedKeys.has(host+'\0'+id)){
      archivedKeys.delete(host+'\0'+id);
      for(const m of liveManagers())if(m.hostId===host)m.handleThreadUnarchived(id);
    }
    for(const id of data.threadIds.slice(0,32))if(uuid.test(id)&&!deletedKeys.has(host+'\0'+id)&&!archivedKeys.has(host+'\0'+id))pending.set(host+'\0'+id,{id,host,failures:0,due:Date.now()});
    for(const id of (data.deletedThreadIds||[]).slice(0,256))if(uuid.test(id)){deletedKeys.add(host+'\0'+id);pending.set(host+'\0'+id,{id,host,deleted:true,due:Date.now()});}
    while(deletedKeys.size>4096)deletedKeys.delete(deletedKeys.values().next().value);
    while(archivedKeys.size>4096)archivedKeys.delete(archivedKeys.values().next().value);
    while(pending.size>1024)pending.delete(pending.keys().next().value);
    void tick();
  });
})();
