const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const id='11111111-1111-4111-8111-111111111111';
let receive,visibleChanged,now=0,requests=[],publications=[],duringRead,finish,draft='';
const ctx={setInterval(){},Date:{now:()=>now},document:{visibilityState:'visible',addEventListener(event,fn){if(event==='visibilitychange')visibleChanged=fn;},querySelector:()=>({textContent:draft})},window:{location:{pathname:'/local/'+id},addEventListener(event,fn){if(event==='message')receive=fn;},
  electronBridge:{sendMessageFromView:m=>publications.push(m)}}};
vm.createContext(ctx);vm.runInContext(fs.readFileSync('scripts/manager_core/desktop_renderer_record_sync.cjs','utf8'),ctx);
const sync=ctx.__codexRendererRecordSync;
const state={draft:'keep',model:'own',effort:'high'};
const manager={hostId:'local',hasInFlightConversationResume:()=>false,
  requestClient:{sendRequest:()=>new Promise(r=>finish=r)},threadStore:{
    backgroundThreadLookups:new Map([[id,'stale']]),threadReadStates:new Map([[id,new Error('old')]]),
    async hydrateThreads(ids,opts){
      assert.equal(opts.includeTurns,true);assert.equal(opts.retainHistoryPagination,true);
      assert(!this.backgroundThreadLookups.has(id));assert(!this.threadReadStates.has(id));
      if(duringRead)duringRead();
      if(sync.canApply(this))requests.push(ids[0]);
    }}};
sync.register(manager);
function invalidate(){receive({data:{type:'manager-record-invalidated',threadIds:[id]}});}
async function flush(){await new Promise(r=>setImmediate(r));now+=1000;await sync.tick();}
(async()=>{
  invalidate();await flush();assert(requests.includes(id),'visible renderer store must refresh');
  assert.equal(requests.length,1,'one main-process delivery must not multiply into three renderer reads');
  await flush();assert.equal(requests.length,1,'successful merge drains the invalidation');
  requests=[];ctx.document.visibilityState='hidden';
  for(let n=0;n<40;n++)invalidate();await flush();
  assert.equal(requests.length,0,'hidden profile must not rebuild its transcript');
  assert.equal(sync.status().pending,1,'hidden invalidations coalesce by task');
  ctx.document.visibilityState='visible';visibleChanged();await flush();
  assert.equal(requests.length,1,'restoring a profile hydrates its latest pending state once');
  requests=[];let raced=false;duringRead=()=>{if(!raced){raced=true;invalidate();}};
  invalidate();await flush();duringRead=null;
  assert.equal(requests.length,2,'invalidation arriving during read must survive completion');
  assert.deepEqual(state,{draft:'keep',model:'own',effort:'high'});
  requests=[];draft='unsent';invalidate();await flush();assert.equal(requests.length,0);assert(sync.status().draftDeferred>0);draft='';await flush();assert(requests.length>0);
  requests=[];sync.observe(manager,'turn/started',{threadId:id,turn:{id:'own'}});
  invalidate();await flush();assert.equal(requests.length,0,'own streaming response stays authoritative');
  sync.observe(manager,'turn/completed',{threadId:id,turn:{id:'own'}});
  const request=manager.requestClient.sendRequest('turn/start',{threadId:id});
  invalidate();await flush();assert.equal(requests.length,0,'pending submit is protected before notifications');
  finish({});await request;
  duringRead=()=>sync.observe(manager,'turn/started',{threadId:id,turn:{id:'race'}});
  invalidate();await flush();assert.equal(requests.length,0,'submit during read cancels merge');
  duringRead=null;sync.observe(manager,'turn/completed',{threadId:id,turn:{id:'race'}});
  invalidate();await flush();assert(requests.length>0);
  sync.observe(manager,'item/agentMessage/delta',{threadId:id,delta:'PRIVATE_TEXT'});
  assert(publications.length>0);assert(!JSON.stringify(publications).includes('PRIVATE_TEXT'));
  assert(publications.every(p=>p.type==='manager-record-changed'&&p.threadId===id));
  assert.equal(sync.status().failures,0);
  sync.observe(manager,'turn/completed',{threadId:id});
  const hydrate=manager.threadStore.hydrateThreads;let failOnce=true;
  manager.threadStore.hydrateThreads=async function(...args){if(failOnce){failOnce=false;throw new Error('temporary read failure');}return Reflect.apply(hydrate,this,args);};
  requests=[];invalidate();await flush();assert.equal(requests.length,0);assert.equal(sync.status().pending,1);
  now+=3000;await sync.tick();assert.equal(requests.length,1,'transient failure retains its bounded retry');
  assert.equal(sync.status().pending,0);
  const visibility=[];manager.handleThreadArchived=id=>visibility.push(['archived',id]);manager.handleThreadUnarchived=id=>visibility.push(['restored',id]);
  receive({data:{type:'manager-record-invalidated',threadIds:[],archivedThreadIds:[id]}});await flush();
  assert.deepEqual(visibility,[['archived',id]]);requests=[];invalidate();await flush();assert.equal(requests.length,0);
  receive({data:{type:'manager-record-invalidated',threadIds:[id],unarchivedThreadIds:[id]}});await flush();
  assert.deepEqual(visibility.at(-1),['restored',id]);assert(requests.length>0);
  const deleted=[];manager.handleThreadDeletion=ids=>deleted.push(['local',...ids]);
  const remote={...manager,hostId:'remote-ssh-discovered:remote-dev',requestClient:{sendRequest:async()=>({})},handleThreadDeletion:ids=>deleted.push(['ssh',...ids])};sync.register(remote);
  requests=[];draft='keep';receive({data:{type:'manager-record-invalidated',hostId:remote.hostId,threadIds:[],deletedThreadIds:[id]}});await flush();
  assert.deepEqual(deleted,[['ssh',id]],'same ID on another host must survive');assert.equal(draft,'keep');assert.equal(requests.length,0,'deletion never hydrates missing history');
  receive({data:{type:'manager-record-invalidated',hostId:remote.hostId,threadIds:[id]}});await flush();assert.equal(sync.status().pending,0,'late reads cannot resurrect a deletion');
  draft='';remote.disposed=true;
  manager.disposed=true;await sync.tick();assert.equal(sync.status().managers,0,'disposed transcript owners must be released');
  sync.register(manager);assert.equal(sync.status().managers,0,'disposed manager must not be re-registered');
  console.log('PASS: renderer invalidation, local streaming/submission races, metadata-only publication');
})().catch(e=>{console.error(e);process.exitCode=1});
