const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('scripts/manager_core/desktop_signal_files.cjs','utf8')+'\n'+fs.readFileSync('scripts/manager_core/desktop_record_sync.cjs','utf8');
const [a,b,flaky,missing]=['11111111-1111-4111-8111-111111111111','22222222-2222-4222-8222-222222222222','44444444-4444-4444-8444-444444444444','33333333-3333-4333-8333-333333333333'];
const peer='55555555-5555-4555-8555-555555555555.desktop.json',remoteHost='remote-ssh-discovered:fixture';
let clock=1e12,stamp=1,changes=[[a,1,'local','changed'],[b,2,'local','changed'],[flaky,3,'local','changed'],[missing,4,'local','changed'],[a,5,remoteHost,'changed']];
// No trusted profile list here, so the writer must be younger than a day.
const io={readdir:async()=>[peer],stat:async()=>({mtimeMs:1e12+stamp,ctimeMs:stamp,size:100,ino:1,isFile:()=>true}),
 readFile:async()=>JSON.stringify({version:2,generation:'before-start',changes}),mkdir:async()=>{},writeFile:async()=>{},rename:async()=>{}};
const deliveries=[],imports=[];
class FakeDate extends Date{static now(){return clock}}
const context={process:{env:{CODEX_RECORD_SIGNALS:'fixture'},pid:7},Date:FakeDate,setInterval:()=>({unref(){}}),clearInterval(){},
 require:n=>n==='electron'?{BrowserWindow:{getAllWindows:()=>[{isDestroyed:()=>false,webContents:{isDestroyed:()=>false,send:(c,p)=>deliveries.push(p)}}]}}:n==='node:fs'?{promises:io}:require(n)};
vm.createContext(context);vm.runInContext(source,context);
const sync=context.__codexRecordSync,reads=[];let flakyFailures=1,version=0;
const manager=hostId=>({hostId,hasInFlightConversationResume:()=>false,requestClient:{sendRequest:async()=>({})},
 threadStore:{threadsById:new Map([[a,{name:'a'}],[b,{name:'b'}],[flaky,{name:'f'}]]),isConversationActive:()=>false,applyThreadTitleUpdate(){},
  async hydrateThreads([id]){reads.push(hostId+' '+id);
   if(id===missing)throw Error('thread not found: '+id);
   if(id===flaky&&flakyFailures-->0)throw Error('connection reset');
   this.threadsById.set(id,{...this.threadsById.get(id),updatedAt:++version});}}});
const count=id=>reads.filter(r=>r==='local '+id).length;
const run=async n=>{for(let i=0;i<n;i++){clock+=1000;await sync.tick();}};
(async()=>{
 // The first scan runs before any window manager exists, as at desktop startup.
 await sync.tick();assert.equal(reads.length,0);
 sync.register(manager('local'));sync.register(manager(remoteHost));
 sync.catalog({getCoordinator:()=>({handleImportedThreads:ids=>imports.push(...ids)})});
 await run(20);
 assert.equal(count(a),1,'a change retained before startup is re-read once, not three times');
 assert.equal(count(b),1);assert.equal(reads.filter(r=>r===remoteHost+' '+a).length,1,'SSH backlog is bounded the same way');
 assert.equal(count(missing),1,'an unresolvable old change is not retried');
 assert.equal(count(flaky),2,'a transient failure of an old change still retries');
 assert.equal(deliveries.filter(p=>p.hostId==='local'&&p.threadIds.includes(a)).length,1,'renderers receive one invalidation per old change');
 assert.equal(sync.status().pending,0);
 const settled=reads.length;
 for(let i=0;i<10;i++){stamp++;await run(1);}
 assert.equal(reads.length,settled,'re-scanning the unchanged backlog issues no reads');
 // A real change after startup is read once after it settles. A live signal
 // can race the writer's durable record, so a live not-found read keeps one
 // bounded retry (the backlog above keeps none).
 changes=[...changes,[a,6,'local','changed'],[missing,7,'local','changed']];stamp++;
 await run(20);
 assert.equal(count(a),1+1,'a live change is read once after its writer is quiet');
 assert.equal(count(missing),1+2,'a live change to a not-yet-persisted task keeps one bounded retry');
 assert(imports.includes(a));assert.equal(sync.status().pending,0);
 sync.stop();console.log('PASS: startup backlog read once per change; missing old tasks dropped; transient and live not-found retries bounded; live changes read once after quiet');
})().catch(e=>{console.error(e);process.exitCode=1});
