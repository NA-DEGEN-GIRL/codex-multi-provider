// Renderer half of the shared plugin refresh. The main-process adapter cannot
// reach this window's react-query subscribers, so it hands over one bounded
// window message; replaying it through the renderer's own app-server manager
// runs the native skills/changed notification case, which invalidates the
// skills query and, with the verified patch, the plugins prefix too.
(() => {
  if(typeof window==='undefined')return;
  const managers=new Set();
  const pending=new Set();
  const counters={invalidations:0,managers:0,skipped:0,deferred:0};
  function deliver(manager){
    if(typeof manager?.events?.emitNotification!=='function'){counters.skipped++;return false;}
    try{
      manager.events.emitNotification({method:'skills/changed',params:{}});
      return true;
    }catch{counters.skipped++;}
    return false;
  }
  function invalidate(hostId){
    let delivered=0;
    for(const manager of managers){
      if(manager?.disposed){managers.delete(manager);continue;}
      if(manager.hostId!==hostId)continue;
      if(deliver(manager))delivered++;
    }
    counters.invalidations+=delivered;
    counters.managers=managers.size;
    // Keep a local invalidation pending until a matching manager can serve it
    // (for example a window that received the message before its app-server
    // manager was constructed).
    if(delivered)pending.delete(hostId);
    else{pending.add(hostId);counters.deferred++;}
    return delivered;
  }
  window.addEventListener('message',event=>{
    const data=event?.data;
    if(data?.type!=='manager-plugins-invalidated')return;
    invalidate(typeof data.hostId==='string'&&data.hostId.length<=256?data.hostId:'local');
  });
  globalThis.__codexPluginRendererSync={
    register(manager){
      if(manager?.disposed||managers.has(manager))return;
      managers.add(manager);
      counters.managers=managers.size;
      if(pending.has(manager.hostId)&&deliver(manager)){
        pending.delete(manager.hostId);
        counters.invalidations++;
      }
    },
    status(){return{...counters,pending:pending.size};}
  };
})();
