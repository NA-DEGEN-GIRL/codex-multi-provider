// The renderer reads a transcript only for a task open in its visible window:
// at most every 2 s while a peer streams, once per settled change, and once when
// a dirty task is opened. Tasks that are not open are never read.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('scripts/manager_core/desktop_renderer_record_sync.cjs','utf8');
const open='11111111-1111-4111-8111-111111111111',closed='22222222-2222-4222-8222-222222222222';
let now=0,nextTimer=0;
const timers=new Map(),events={};
const ctx={Date:{now:()=>now},document:{visibilityState:'visible',querySelector:()=>({textContent:''}),
  addEventListener:(name,fn)=>events[name]=fn},window:{addEventListener:(name,fn)=>events[name]=fn},
  setTimeout(fn,delay){const handle=++nextTimer;timers.set(handle,{fn,due:now+delay});return handle;},
  clearTimeout:handle=>timers.delete(handle)};
vm.createContext(ctx);vm.runInContext(source,ctx);const sync=ctx.__codexRendererRecordSync;
const settle=()=>new Promise(resolve=>setImmediate(resolve));
async function advance(ms){for(let t=0;t<ms;t+=100){now+=100;for(const [handle,timer] of [...timers])if(timer.due<=now){timers.delete(handle);timer.fn();}await settle();}}
const interests=new Map(),reads=[];
const store={isConversationActive:id=>(interests.get(id)||0)>0,
  retainActiveConversation(id,changed){const n=interests.get(id)||0;interests.set(id,n+1);if(!n)changed(true);
    return()=>{const n=interests.get(id)-1;interests.set(id,n);if(!n)changed(false);};},
  async hydrateThreads([id],options){reads.push({id,at:now});assert.equal(options.includeTurns,true);assert.equal(options.maxTurns,8);}};
const m={hostId:'local',threadStore:store,requestClient:{sendRequest:async()=>({})},hasInFlightConversationResume:()=>false,
  handleThreadDeletion(){},handleThreadArchived(){},handleThreadUnarchived(){}};
const deliver=(threadIds,liveThreadIds=[])=>events.message({data:{type:'manager-record-invalidated',hostId:'local',threadIds,liveThreadIds}});
const count=id=>reads.filter(r=>r.id===id).length;
(async()=>{
  sync.register(m);store.retainActiveConversation(open,()=>{});await advance(400);
  assert.equal(reads.length,0,'opening a clean task reads nothing extra');
  // A peer streams into both tasks for 10 s. Notices arrive faster than the
  // main process sends them, to prove the renderer's own 2 s bound.
  // Main also delivers its capped ~5 s mid-stream reads; they keep the bound.
  for(let t=0,n=0;t<10000;t+=400,n++){if(n%14===13)deliver([open,closed]);else deliver([],[open,closed]);await advance(400);}
  const live=reads.filter(r=>r.id===open).map(r=>r.at);
  assert(live.length>=4&&live.length<=6,'about one refresh per 2 s: '+live.length);
  for(let i=1;i<live.length;i++)assert(live[i]-live[i-1]>=2000,'an open transcript refreshes at most every 2 s');
  assert.equal(count(closed),0,'a task that is not open is never read while it streams');
  await advance(4000);const streamed=count(open);
  deliver([open,closed]);await settle();
  assert.equal(count(open),streamed+1,'the settled change reads the open task once');
  assert.equal(count(closed),0,'the settled change does not read a task that is not open');
  await advance(6000);assert.equal(count(open),streamed+1);assert.equal(sync.status().pending,0);
  store.retainActiveConversation(closed,()=>{});await advance(400);
  assert.equal(count(closed),1,'opening a dirty task reads it once');
  await advance(6000);assert.equal(count(closed),1);
  // Hidden windows keep invalidations; showing the window reads each open task once.
  ctx.document.visibilityState='hidden';
  for(let t=0;t<4000;t+=400){deliver([],[open]);await advance(400);}
  deliver([open]);await advance(1000);assert.equal(count(open),streamed+1,'a hidden window reads nothing');
  ctx.document.visibilityState='visible';events.visibilitychange();await settle();
  assert.equal(count(open),streamed+2,'becoming visible reads the latest state once');
  await advance(6000);assert.equal(count(open),streamed+2);
  assert.equal(sync.status().failures,0);assert(sync.status().liveNotices>0);
  console.log('PASS: open visible transcript refreshes at most every 2 s while streaming, once per settled change; closed tasks read only when opened');
})().catch(error=>{console.error(error);process.exitCode=1});
