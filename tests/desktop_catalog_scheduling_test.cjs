const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const id='11111111-1111-4111-8111-111111111111',file='22222222-2222-4222-8222-222222222222.json';
let now=10000,visible=false,seq=1,kind='changed',reads=0,deleted=0,archived=0,restored=0;
const messages=[],observed=[],DateClock=class extends Date{static now(){return now;}};
const io={mkdir:async()=>{},readdir:async()=>[file],stat:async()=>({size:100,mtimeMs:seq,isFile:()=>true}),
  readFile:async()=>JSON.stringify({version:2,generation:'fixture',changes:[[id,seq,'local',kind]]}),
  writeFile:async()=>{},rename:async()=>{}};
const win={isDestroyed:()=>false,isVisible:()=>visible,webContents:{isDestroyed:()=>false,send:(_,message)=>messages.push(message)}};
const ctx={process:{env:{CODEX_RECORD_SIGNALS:'fixture'}},Date:DateClock,
  setInterval:()=>({unref(){}}),clearInterval(){},require:n=>n==='node:fs'?{promises:io}:
    n==='electron'?{BrowserWindow:{getAllWindows:()=>[win]}}:require(n)};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync('scripts/manager_core/desktop_signal_files.cjs','utf8')+'\n'+fs.readFileSync('scripts/manager_core/desktop_record_sync.cjs','utf8'),ctx);
const sync=ctx.__codexRecordSync;
sync.register({hostId:'local',hasInFlightConversationResume:()=>false,requestClient:{sendRequest:async()=>({})},
  handleThreadDeletion(){deleted++},handleThreadArchived(){archived++},handleThreadUnarchived(){restored++},
  threadStore:{threadsById:new Map([[id,{name:'fixture'}]]),isConversationActive:()=>true,
    applyThreadTitleUpdate(){},async hydrateThreads(_,options){assert.equal(options.includeTurns,false);reads++;observed.push(seq);}}});
(async()=>{
  await sync.tick();assert.equal(reads,1);
  // A hidden peer stream (a delta every 100 ms) is coalesced, yet re-read with
  // its latest revision within the 5 s settle bound.
  const streamStart=now+100;
  for(let n=0;n<50;n++){now+=100;seq++;await sync.tick();}
  assert.equal(reads,1,'hidden streaming updates are coalesced, not re-read on every tick');
  assert.equal(sync.status().pending,1);assert.equal(sync.status().failures,0);
  now+=100;seq++;await sync.tick();
  assert.equal(reads,2,'a hidden stream is still re-read within 5 s');assert.equal(observed.at(-1),seq);
  assert(now-streamStart<=5000);
  for(let n=0;n<10;n++){now+=100;seq++;await sync.tick();}
  assert.equal(reads,2);
  visible=true;now+=100;seq++;await sync.tick();
  assert.equal(reads,3,'selection mid-stream bypasses the timers and reads the latest revision immediately');
  assert.equal(observed.at(-1),seq);
  visible=false;now+=100;seq++;await sync.tick();now+=1500;await sync.tick();
  assert.equal(reads,4,'a single change is read once it is quiet');
  now+=100;seq++;await sync.tick();now+=1600;await sync.tick();
  assert.equal(reads,4,'a settled change still waits for the hidden refresh timer');
  now+=300;await sync.tick();assert.equal(reads,5);assert.equal(observed.at(-1),seq);
  now+=100;seq++;kind='archived';await sync.tick();assert.equal(archived,1);
  assert.equal(reads,5,'archive changes bypass batching without hydrating missing history');
  seq++;kind='unarchived';await sync.tick();assert.equal(restored,1);assert.equal(reads,5);
  now+=2000;await sync.tick();assert.equal(reads,6,'a restore reads its summary once it settles');
  seq++;kind='deleted';await sync.tick();assert.equal(deleted,1);assert.equal(reads,6);
  seq++;kind='changed';await sync.tick();now+=2400;await sync.tick();assert.equal(reads,6,'late delta cannot resurrect a deletion');
  assert(messages.some(m=>m.deletedThreadIds.includes(id)));
  sync.stop();console.log('PASS: hidden catalog coalescing, latest-state selection, immediate archive/restore/deletion and no stale resurrection');
})().catch(error=>{console.error(error);process.exitCode=1});
