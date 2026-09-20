const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('scripts/manager_core/desktop_renderer_record_sync.cjs','utf8');
const id='11111111-1111-4111-8111-111111111111';
let now=0,nextTimer=0,draft='';
const timers=new Map(),events={};
const ctx={Date:{now:()=>now},document:{visibilityState:'visible',querySelector:()=>({textContent:draft}),
  addEventListener:(name,fn)=>events[name]=fn},window:{addEventListener:(name,fn)=>events[name]=fn},
  setTimeout(fn,delay){const handle=++nextTimer;timers.set(handle,{fn,due:now+delay});return handle;},
  clearTimeout:handle=>timers.delete(handle)};
vm.createContext(ctx);vm.runInContext(source,ctx);const sync=ctx.__codexRendererRecordSync;
const settle=()=>new Promise(resolve=>setImmediate(resolve));
async function advance(ms){now+=ms;for(const [handle,timer] of [...timers])if(timer.due<=now){timers.delete(handle);timer.fn();}await settle();}
function manager(hostId='local'){
  const interests=new Map(),reads=[],cache=new Map(),versions=new Map();
  let unblock,nextBlocked=false,parses=0;
  const store={
    isConversationActive:id=>(interests.get(id)||0)>0,
    retainActiveConversation(id,changed){
      const count=interests.get(id)||0;interests.set(id,count+1);if(!count)changed(true);
      return()=>{const count=interests.get(id)-1;interests.set(id,count);if(!count)changed(false);return 'native-release';};
    },
    async hydrateThreads(ids,options){
      const threadId=ids[0],version=versions.get(threadId)||0;
      reads.push({id:threadId,includeTurns:options.includeTurns});
      assert.equal(options.retainHistoryPagination,true);
      if(options.includeTurns)assert.equal(options.maxTurns,8);
      else assert.equal(options.maxTurns,undefined,'inactive summaries never request a turn window');
      if(nextBlocked){nextBlocked=false;await new Promise(resolve=>unblock=resolve);}
      if(sync.canApply(store)&&options.includeTurns){parses++;cache.set(threadId,version);}
    }
  };
  const m={hostId,threadStore:store,requestClient:{sendRequest:async()=>({})},hasInFlightConversationResume:()=>false,
    dispose(){this.disposed=true;return 'native-dispose';},handleThreadDeletion(){},handleThreadArchived(){},handleThreadUnarchived(){}};
  return{m,store,reads,cache,versions,parses:()=>parses,block:()=>nextBlocked=true,unblock:()=>unblock()};
}
function invalidate(hostId='local',ids=[id],extra={}){events.message({data:{type:'manager-record-invalidated',hostId,threadIds:ids,...extra}});}
(async()=>{
  const local=manager();sync.register(local.m);local.cache.set(id,-1);local.versions.set(id,1);
  invalidate();await settle();
  assert.deepEqual(local.reads,[{id,includeTurns:false}]);assert.equal(local.parses(),0);
  assert.equal(local.cache.get(id),-1,'a cached inactive transcript is left unparsed');
  local.versions.set(id,2);invalidate();await settle();
  const changed=[];let release=local.store.retainActiveConversation(id,value=>changed.push(value));
  assert.deepEqual(changed,[true]);assert.equal(local.reads.length,2,'retention must not hydrate inside its caller');
  await advance(399);assert.equal(local.parses(),0);
  await advance(1);assert.equal(local.parses(),1);assert.equal(local.cache.get(id),2,'activation reads the newest transcript once');
  const second=local.store.retainActiveConversation(id,()=>assert.fail('already active'));
  await advance(1000);assert.equal(local.parses(),1,'another interest does not repeat a clean activation');
  assert.equal(second(),'native-release');assert.equal(release(),'native-release');assert.deepEqual(changed,[true,false]);

  local.versions.set(id,3);invalidate();await settle();
  draft='unsent draft';release=local.store.retainActiveConversation(id,()=>{});
  const beforeDraft=local.reads.length;await advance(400);assert.equal(local.reads.length,beforeDraft);
  draft='';await advance(400);assert.equal(local.cache.get(id),3);release();
  local.versions.set(id,4);invalidate();await settle();
  release=local.store.retainActiveConversation(id,()=>{});release();
  const beforeAbandon=local.reads.length;await advance(400);
  assert.equal(local.reads.length,beforeAbandon,'an abandoned activation does not read even a summary');
  release=local.store.retainActiveConversation(id,()=>{});await advance(400);
  assert.equal(local.cache.get(id),4,'abandoned activation leaves dirty history available for the next activation');release();

  local.versions.set(id,5);local.block();invalidate();await settle();
  release=local.store.retainActiveConversation(id,()=>{});
  local.versions.set(id,6);invalidate();local.unblock();await settle();await advance(700);
  assert.equal(local.cache.get(id),6,'activation during an inactive read preserves the newer in-flight invalidation');
  local.versions.set(id,7);local.block();invalidate();await settle();
  local.versions.set(id,8);invalidate();local.unblock();await settle();await advance(700);
  assert.equal(local.cache.get(id),8,'a completed transcript read cannot clear a newer dirty generation');release();

  // A disposed owner and an activation that loses interest mid-read cannot
  // install transcript graphs after the native view has gone away.
  local.versions.set(id,9);invalidate();await settle();local.block();
  release=local.store.retainActiveConversation(id,()=>{});await advance(400);release();local.unblock();await settle();
  assert.equal(local.cache.get(id),8,'losing interest during the read prevents transcript application');
  await advance(700);release=local.store.retainActiveConversation(id,()=>{});await advance(400);
  assert.equal(local.cache.get(id),9);release();
  const remote=manager('remote-ssh-test');sync.register(remote.m);remote.versions.set(id,20);
  invalidate(remote.m.hostId);await settle();remote.block();remote.store.retainActiveConversation(id,()=>{});await advance(400);
  assert.equal(remote.m.dispose(),'native-dispose');remote.unblock();await settle();
  assert.equal(remote.parses(),0,'disposed manager never applies an in-flight transcript');
  assert.equal(local.cache.get(id),9,'same task ID on SSH never changes local history');

  // Delete/archive invalidations remain authoritative over queued activations.
  local.versions.set(id,10);invalidate();await settle();
  release=local.store.retainActiveConversation(id,()=>{});
  invalidate('local',[],{archivedThreadIds:[id]});const beforeArchive=local.reads.length;await advance(700);
  assert.equal(local.reads.length,beforeArchive);release();
  invalidate('local',[id],{unarchivedThreadIds:[id]});await settle();
  release=local.store.retainActiveConversation(id,()=>{});await advance(400);assert.equal(local.cache.get(id),10);release();
  invalidate();await settle();release=local.store.retainActiveConversation(id,()=>{});
  invalidate('local',[],{deletedThreadIds:[id]});const beforeDelete=local.reads.length;await advance(700);
  assert.equal(local.reads.length,beforeDelete);release();

  const delayed=manager('delayed'),delayedStore=delayed.store,nativeRetain=delayedStore.retainActiveConversation;
  delete delayed.m.threadStore;sync.register(delayed.m);delayed.m.threadStore=delayedStore;
  delayed.versions.set(id,30);invalidate('delayed');await settle();
  assert.equal(delayed.reads[0].includeTurns,false,'constructor registration before store assignment installs the hook lazily');
  assert.notEqual(delayedStore.retainActiveConversation,nativeRetain);
  const delayedRelease=delayedStore.retainActiveConversation(id,()=>{});await advance(400);
  assert.equal(delayed.cache.get(id),30);delayedRelease();
  delayed.m.dispose();assert.equal(delayedStore.retainActiveConversation,nativeRetain,'dispose restores the exact native hook');
  const wrappedOwner=manager('wrapped-owner');sync.register(wrappedOwner.m);
  const ourRetain=wrappedOwner.store.retainActiveConversation;
  const externalRetain=function(...args){return Reflect.apply(ourRetain,this,args);};
  wrappedOwner.store.retainActiveConversation=externalRetain;wrappedOwner.m.dispose();
  assert.equal(wrappedOwner.store.retainActiveConversation,externalRetain,'dispose must preserve another integration wrapper');
  const readonly=manager('readonly');
  Object.defineProperty(readonly.store,'retainActiveConversation',{value:readonly.store.retainActiveConversation,writable:false});
  sync.register(readonly.m);invalidate('readonly');await settle();
  assert.equal(readonly.reads[0].includeTurns,true,'an unwrappable native hook keeps the compatibility history path');
  const throwing=manager('throwing'),nativeFailure=Error('native retain failure');
  throwing.store.retainActiveConversation=()=>{throw nativeFailure;};sync.register(throwing.m);
  assert.throws(()=>throwing.store.retainActiveConversation(id,()=>{}),error=>error===nativeFailure);

  const orphans=Array.from({length:9},(_,i)=>(2000+i).toString(16).padStart(8,'0')+'-1111-4111-8111-111111111111');
  invalidate('unregistered-host',orphans);await settle();
  assert.equal(sync.status().refreshScheduled,false,'unregistered host invalidations must not create a refresh poll');
  const served=manager('served');sync.register(served.m);invalidate('served');await settle();
  assert.equal(served.reads.length,1,'older unmatched hosts cannot occupy the matching host read budget');
  assert.equal(sync.status().refreshScheduled,false);
  const reconnecting=manager('reconnect');sync.register(reconnecting.m);
  ctx.document.visibilityState='hidden';invalidate('reconnect');await settle();
  const preserved=sync.status().pending;reconnecting.m.dispose();
  ctx.document.visibilityState='visible';events.visibilitychange();await settle();
  assert.equal(sync.status().pending,preserved,'ordinary invalidations survive the last matching manager disposal');
  assert.equal(sync.status().refreshScheduled,false,'the preserved queue sleeps while the native host is absent');
  const replacement=manager('reconnect'),replacementStore=replacement.store;
  delete replacement.m.threadStore;sync.register(replacement.m);
  assert.equal(sync.status().refreshScheduled,true,'registration wakes the preserved host queue before store assignment');
  replacement.m.threadStore=replacementStore;await advance(400);
  assert.equal(replacement.reads.length,1);assert.equal(sync.status().pending,preserved-1);
  assert.equal(sync.status().refreshScheduled,false);

  // An unrelated failed task must retain its own backoff without delaying a
  // newly activated cached transcript behind that later scheduler deadline.
  const retrying=manager('retry-deadline'),failedId='aaaaaaaa-1111-4111-8111-111111111111';
  const retryHydrate=retrying.store.hydrateThreads,attempts=[];let failOnce=true;
  retrying.store.hydrateThreads=async function(ids,options){
    attempts.push({id:ids[0],at:now});
    if(ids[0]===failedId&&failOnce){failOnce=false;throw Error('retry deadline fixture');}
    return Reflect.apply(retryHydrate,this,[ids,options]);
  };
  sync.register(retrying.m);retrying.versions.set(id,51);invalidate('retry-deadline');await settle();
  const failedAt=now;invalidate('retry-deadline',[failedId]);await settle();
  assert.equal(attempts.filter(attempt=>attempt.id===failedId).length,1);
  retrying.store.retainActiveConversation(id,()=>{});await advance(399);
  assert.equal(retrying.parses(),0);await advance(1);
  assert.equal(retrying.cache.get(id),51,'activation advances an existing later timer to its own 400 ms deadline');
  assert.equal(attempts.filter(attempt=>attempt.id===failedId).length,1,'earlier activation must not shorten the other task backoff');
  await advance(2099);assert.equal(attempts.filter(attempt=>attempt.id===failedId).length,1);
  await advance(1);assert.deepEqual(attempts.filter(attempt=>attempt.id===failedId).map(attempt=>attempt.at),[failedAt,failedAt+2500]);

  // More dirty tasks than the bound must not silently validate an evicted,
  // previously loaded transcript. Its later activation conservatively reads.
  const bounded=manager('bounded');sync.register(bounded.m);
  const ids=Array.from({length:1025},(_,i)=>i.toString(16).padStart(8,'0')+'-1111-4111-8111-111111111111');
  bounded.cache.set(ids[0],-1);bounded.versions.set(ids[0],99);
  for(const threadId of ids){invalidate('bounded',[threadId]);await settle();}
  const beforeBounded=bounded.reads.length;bounded.store.retainActiveConversation(ids[0],()=>{});await advance(400);
  assert.equal(bounded.reads.length,beforeBounded+1);assert.equal(bounded.cache.get(ids[0]),99);
  assert(sync.status().summaryRefreshes>=1025);assert(sync.status().historyRefreshes>0);
  console.log('PASS: offscreen summaries, deferred native-interest catch-up, drafts, lifetime/invalidation races, host isolation and bounded dirty history');
})().catch(error=>{console.error(error);process.exitCode=1});
