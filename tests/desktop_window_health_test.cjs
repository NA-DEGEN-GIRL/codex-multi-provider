const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
let now=10000,tick,created,ready,closed,finish,probes=0,health;
const lease={version:1,appPid:42,hwnd:'123',token:'fixture',mode:'viewport',visible:true};
const promises={async stat(){return{size:100}},async readFile(){return JSON.stringify(lease)},
  async writeFile(_,text){health=JSON.parse(text)},async rename(){}};
const win={getNativeWindowHandle(){const b=Buffer.alloc(8);b.writeBigUInt64LE(123n);return b},isDestroyed:()=>false,
  once(event,fn){assert.equal(event,'closed');closed=fn},webContents:{
    once(event,fn){assert.equal(event,'did-finish-load');ready=fn},getOSProcessId:()=>43,
    executeJavaScript(code,userGesture){assert.equal(code,'0');assert.equal(userGesture,false);probes++;return new Promise(r=>{finish=r})}}};
vm.runInNewContext(fs.readFileSync('scripts/manager_core/desktop_window_health.cjs','utf8'),{
  process:{platform:'win32',pid:42,env:{CODEX_MANAGER_ROOT:'fixture'}},Date:{now:()=>now},
  setInterval(fn,interval){assert.equal(interval,1000);tick=fn;return{unref(){}}},
  require(name){if(name==='node:fs')return{promises};if(name==='electron')return{app:{on(event,fn){assert.equal(event,'browser-window-created');created=fn}}};return require(name)}
});
(async()=>{
  created(null,win);await tick();assert.equal(probes,0,'no execution before load');
  ready();now+=1000;await tick();assert.equal(probes,1);assert.equal(health.rendererPid,43);
  now+=3000;await tick();assert.equal(probes,1,'hung renderer cannot accumulate probes');
  assert.equal(health.renderer_pending_ms,3000);assert.equal(health.node_lag_ms,2000);
  finish(0);await new Promise(r=>setImmediate(r));now+=1000;await tick();
  assert.equal(health.renderer_roundtrip_ms,3000);assert.equal(probes,2);
  lease.visible=false;now+=5000;await tick();assert.equal(probes,2,'hidden profiles are not probed');
  finish(0);await new Promise(r=>setImmediate(r));lease.visible=true;lease.hwnd='999';await tick();assert.equal(probes,2,'different HWND is untouched');
  lease.hwnd='123';closed();await tick();assert.equal(probes,2,'closed window released');
  console.log('PASS: metadata-only renderer liveness, bounded stalled probe, hidden/wrong/closed window ignored');
})().catch(error=>{console.error(error);process.exitCode=1});
