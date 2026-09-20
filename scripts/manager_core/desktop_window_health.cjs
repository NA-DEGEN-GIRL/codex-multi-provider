// Metadata-only liveness probes. A renderer IPC round trip distinguishes a
// responsive native HWND from a busy Chromium/React thread. Never reads the DOM,
// focuses a window, reloads a route, or sends user input.
(() => {
  const root=process.env.CODEX_MANAGER_ROOT;
  if(process.platform!=='win32'||!root)return;
  const fs=require('node:fs').promises, path=require('node:path'),{app}=require('electron');
  const directory=path.join(root,'work','control-center','window-hosts');
  const marker=path.join(directory,`${process.pid}.json`),file=path.join(directory,`${process.pid}.health.json`);
  const windows=new Map();let ticking=false,lastTick=Date.now();
  app.on('browser-window-created',(_,win)=>{
    const hwnd=win.getNativeWindowHandle().readBigUInt64LE().toString();
    const state={win,ready:false,pending:0,started:0,finished:0,latency:0,failures:0,input:null};
    windows.set(hwnd,state);
    win.webContents.once('did-finish-load',()=>{state.ready=true;});
    win.once('closed',()=>{windows.delete(hwnd);});
  });
  const timer=setInterval(async()=>{
    const now=Date.now(),nodeLag=Math.max(0,now-lastTick-1000);lastTick=now;
    if(ticking)return;ticking=true;
    try{
      const stat=await fs.stat(marker);if(stat.size>32768)return;
      const lease=JSON.parse(await fs.readFile(marker,'utf8'));
      if(lease.version!==1||lease.appPid!==process.pid||lease.mode!=='viewport'||!lease.visible)return;
      const state=windows.get(lease.hwnd);if(!state||state.win.isDestroyed())return;
      if(state.ready&&!state.pending&&now-state.started>=2000){
        state.pending=state.started=now;
        Promise.resolve().then(()=>state.win.webContents.executeJavaScript('globalThis.__codexRendererRecordSync?.inputHealth?.() ?? null',false)).then(sample=>{
          state.finished=Date.now();state.latency=state.finished-state.started;
          // Whitelist scalars even though the producer is our own adapter.
          state.input=null;
          if(sample && typeof sample==='object'){
            const input={event_timing_supported:sample.event_timing_supported===true};
            for(const key of ['window_ms','slow_input_samples','input_delay_max_ms','input_duration_p95_ms',
              'input_duration_max_ms','long_task_samples','long_task_max_ms'])
              if(Number.isFinite(sample[key])&&sample[key]>=0)input[key]=Math.round(sample[key]);
            state.input=input;
          }
        }).catch(()=>{state.failures++;}).finally(()=>{state.pending=0;});
      }
      const data={version:1,appPid:process.pid,hwnd:lease.hwnd,token:lease.token,at:now,
        rendererPid:state.win.webContents.getOSProcessId?.(),
        ready:state.ready,node_lag_ms:nodeLag,renderer_pending_ms:state.pending?now-state.pending:0,
        renderer_roundtrip_ms:state.latency,renderer_completed_at:state.finished,failures:state.failures,input:state.input};
      const tmp=file+'.tmp';await fs.writeFile(tmp,JSON.stringify(data));await fs.rename(tmp,file);
    }catch{/* Missing lease and filesystem races are normal during attach/detach. */}
    finally{ticking=false;}
  },1000);
  timer.unref();
})();
