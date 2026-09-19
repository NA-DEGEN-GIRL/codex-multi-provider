const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('scripts/manager_core/desktop_renderer_record_sync.cjs','utf8');
const id='11111111-1111-4111-8111-111111111111';
let now=0,next=0,reads=0,failRead=false,nativeDisposes=0;
const timers=new Map(),events={},messages=[];
const document={visibilityState:'visible',querySelector:()=>({textContent:''}),addEventListener:(name,fn)=>events[name]=fn};
const ctx={Date:{now:()=>now},document,window:{addEventListener:(name,fn)=>events[name]=fn,
  electronBridge:{sendMessageFromView:message=>messages.push(message)}},
  setInterval(){throw Error('renderer must not install a perpetual refresh poll');},
  setTimeout(fn,delay){const handle=++next;timers.set(handle,{fn,due:now+delay});return handle;},
  clearTimeout:handle=>timers.delete(handle)};
vm.createContext(ctx);vm.runInContext(source,ctx);const sync=ctx.__codexRendererRecordSync;
const settle=()=>new Promise(resolve=>setImmediate(resolve));
async function advance(ms){now+=ms;for(const [handle,timer] of [...timers])if(timer.due<=now){timers.delete(handle);timer.fn();}await settle();}
function manager(hostId){return {hostId,hasInFlightConversationResume:()=>false,
  requestClient:{sendRequest:async()=>({})},
  dispose(){this.disposed=true;nativeDisposes++;return 'native-result';},
  handleThreadDeletion(){},handleThreadArchived(){},handleThreadUnarchived(){},
  threadStore:{async hydrateThreads(){reads++;if(failRead){failRead=false;throw Error('transient');}}}};}
const invalidate=hostId=>events.message({data:{type:'manager-record-invalidated',hostId,threadIds:[id]}});
(async()=>{
  const local=manager('local');sync.register(local);
  assert.equal(timers.size,1,'only the coarse disposal fallback runs at idle');
  assert.equal([...timers.values()][0].due,30000);
  sync.observe(local,'turn/started',{threadId:id,turn:{id:'turn'}});
  for(let n=0;n<1000;n++)sync.observe(local,'item/agentMessage/delta',{threadId:id,delta:'PRIVATE_TEXT'});
  assert.equal(messages.length,1);assert.equal(sync.status().pendingNotifications,1);
  await advance(199);assert.equal(messages.length,1);
  await advance(1);assert.equal(messages.length,2);
  assert.equal(sync.status().ipcCoalesced,1000);
  assert(!JSON.stringify(messages).includes('PRIVATE_TEXT'));
  console.log('MEASURE: 1,000 stream deltas + start: renderer IPC notifications 1,001 -> '+messages.length+' (simulated 200 ms burst)');
  sync.observe(local,'item/agentMessage/delta',{threadId:id});
  sync.observe(local,'turn/completed',{threadId:id,turn:{id:'turn'}});
  const completed=messages.length;assert.equal(sync.status().pendingNotifications,0);
  await advance(200);assert.equal(messages.length,completed,'completion sends immediately and cancels the trailing delta');
  sync.observe(local,'thread/project/updated',{threadId:id});assert.equal(messages.length,completed+1);
  const remote=manager('remote-ssh-fixture');sync.register(remote);
  sync.observe(remote,'turn/started',{threadId:id});
  sync.observe(remote,'item/agentMessage/delta',{threadId:id});
  sync.observe(remote,'thread/deleted',{threadId:id});
  const deleted=messages.length;assert.equal(messages.at(-1).kind,'deleted');
  assert.equal(messages.at(-1).hostId,remote.hostId);
  await advance(400);sync.observe(remote,'item/agentMessage/delta',{threadId:id});
  assert.equal(messages.length,deleted,'stale queued deltas cannot resurrect a deleted task');
  sync.observe(local,'thread/archived',{threadId:id});assert.equal(messages.at(-1).kind,'archived');
  sync.observe(local,'thread/unarchived',{threadId:id});assert.equal(messages.at(-1).kind,'unarchived');

  document.visibilityState='hidden';events.visibilitychange();
  for(let n=0;n<40;n++)invalidate('local');await settle();
  assert.equal(reads,0);assert.equal(sync.status().pending,1);assert.equal(sync.status().refreshScheduled,false);
  document.visibilityState='visible';events.visibilitychange();await settle();
  assert.equal(reads,1);assert.equal(sync.status().pending,0);assert.equal(sync.status().refreshScheduled,false);
  failRead=true;invalidate('local');await settle();assert.equal(sync.status().refreshScheduled,true);
  const failedReads=reads;await advance(2000);assert.equal(reads,failedReads);
  await advance(500);assert.equal(reads,failedReads+1);assert.equal(sync.status().refreshScheduled,false);
  assert.equal(local.dispose(),'native-result');assert.equal(nativeDisposes,1);
  const before=messages.length;sync.observe(local,'turn/started',{threadId:id});assert.equal(messages.length,before);
  remote.disposed=true;await advance(30000);
  assert.equal(sync.status().managers,0);assert.equal(timers.size,0,'disposed owners leave no refresh/cleanup timers');
  console.log('PASS: coalesced metadata IPC, immediate final/deletion/move events, hidden/idle scheduling, retries and disposal');
})().catch(error=>{console.error(error);process.exitCode=1});
