const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm'), path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../scripts/manager_core/desktop_task_context.cjs'), 'utf8');
const id='00000000-0000-4000-9000-000000000001', generation='00000000-0000-4000-9000-000000000002', thread='00000000-0000-4000-9000-000000000003';
let events=[], renamed=[], win={isDestroyed:()=>false,getNativeWindowHandle(){const b=Buffer.alloc(8);b.writeBigUInt64LE(123n);return b;}};
const context={URL,process:{platform:'win32',pid:42,env:{CODEX_MANAGER_ROOT:'isolated-fixture',CODEX_MANAGER_PROFILE_ID:id,CODEX_MANAGER_GENERATION:generation}},setTimeout,require(name){
  if(name==='electron')return {BrowserWindow:{fromWebContents:()=>win}};
  if(name==='node:fs')return {readFileSync:()=>JSON.stringify({version:1,appPid:42,mode:'viewport',hwnd:'123'})};
  if(name==='node:fs/promises')return {async mkdir(){},async writeFile(p,data){events.push(JSON.parse(data))},async rename(a,b){renamed.push(b)}};
  return require(name);
}};
vm.runInNewContext(source,context);
const settle=()=>new Promise(r=>setImmediate(r));
(async()=>{
  assert.equal(events.length,0);
  context.__codexManagerTaskContext({},'/local/'+thread+'?hostId=ssh-fixture','작업 제목');await settle();
  assert.equal(events.at(-1).thread_id,thread);assert.equal(events.at(-1).host_id,'ssh-fixture');assert.equal(events.at(-1).generation,generation);assert.equal(events.at(-1).app_pid,42);
  context.__codexManagerTaskContext({},'/new','새 작업');await settle();assert.equal(events.at(-1).thread_id,null);
  context.__codexManagerTaskContext({},'/local/not-a-real-task','pending');await settle();assert.equal(events.at(-1).thread_id,null);
  context.__codexManagerTaskContext({},'/local/'+thread,'A');context.__codexManagerTaskContext({},'/settings','settings');await settle();assert.equal(events.at(-1).thread_id,null);
  const count=events.length;
  win={isDestroyed:()=>false,getNativeWindowHandle(){const b=Buffer.alloc(8);b.writeBigUInt64LE(456n);return b;}};
  context.__codexManagerTaskContext({},'/','avatar');await settle();assert.equal(events.length,count,'auxiliary avatar route must not erase the hosted task');
  win=null;context.__codexManagerTaskContext({},'/local/'+thread,'B');await settle();assert.equal(events.length,count);
  assert.equal(events.length,renamed.length);
  assert.ok(renamed.every(p=>p.endsWith(path.join(id,'active-task.json'))));
  console.log('PASS: actual route/SSH host, new-task clear, rapid selection coalescing, detached view ignored; no RPC/navigation/focus');
})().catch(e=>{console.error(e);process.exitCode=1});
