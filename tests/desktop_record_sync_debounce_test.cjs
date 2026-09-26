// Cross-profile record sync reads each real change once: after the writers are
// quiet (or every 5 s while a task keeps changing), never for a shared-host turn
// this profile streamed itself, with one bounded not-found retry, and never
// from retired writers. The catalog reuses the summary only on verified shapes.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('scripts/manager_core/desktop_signal_files.cjs','utf8')+'\n'+fs.readFileSync('scripts/manager_core/desktop_record_sync.cjs','utf8');
const task=n=>n.toString(16).padStart(8,'0')+'-1111-4111-8111-111111111111';
const [me,peer,peer2,gone]=['aaaaaaaa-0000-4000-8000-000000000001','bbbbbbbb-0000-4000-8000-000000000002','bbbbbbbb-0000-4000-8000-000000000003','cccccccc-0000-4000-8000-000000000004'];
const original='00000000-0000-4000-8000-000000000001',ssh='remote-ssh-discovered:shared',DAY=864e5;
// The 26.915/26.917 catalog coordinator methods, verbatim apart from the
// minified converter name. observeThread applies a Thread without I/O;
// handleImportedThreads queues refreshThread, a second thread/read per task.
const um=(t,hostId)=>t.source==null?null:{hostId,threadId:t.id};
class NativeCoordinator{
  constructor(hostId,log){this.store={hostId,applyObservedEntry:e=>({changedThreadIds:[e.threadId]}),applyAuthoritativeRemoval:id=>({removedThreadIds:[id]})};
    this.syncEnabled=true;this.disposed=false;this.log=log;}
  observeThreads(){}
  publishMutation(m){for(const id of m.changedThreadIds||[])this.log.observed.push(id);}
  handleImportedThreads(ids){this.log.imported.push(...ids);}
  observeThread(e){this.observeThreads([e]);let t=um(e,this.store.hostId);if(t==null){this.publishMutation(this.store.applyAuthoritativeRemoval(e.id),[]);return}this.publishMutation(this.store.applyObservedEntry(t),[t])}
  async refreshThread(e,t,n){let r=await this.client.readItem(e,n);r.onAccepted?.(this),this.publishMutation(this.store.applyObservedEntry(r.entry),[r.entry])}
}
function setup({profiles,initial=[],coordinator}={}){
  let clock=1e12,visible=false;
  const files=new Map();
  const write=(name,changes,mtime=clock)=>{const f=files.get(name)||{generation:'g-'+name,size:100,seq:0};
    f.changes=changes;f.mtime=mtime;f.size++;files.set(name,f);};
  // Like a writer: one entry per task holding its latest sequence and kind.
  const emit=(name,id,host='local',kind='changed')=>{const f=files.get(name)||{changes:[],seq:0};const seq=(f.seq||0)+1;
    write(name,[...(f.changes||[]).filter(c=>c[0]!==id||c[2]!==host),[id,seq,host,kind]]);files.get(name).seq=seq;};
  for(const [name,changes,mtime] of initial)write(name,changes,mtime);
  const io={readdir:async dir=>{if(path.basename(dir)!=='profiles')return [...files.keys()];
      if(!profiles)throw Object.assign(Error('no profiles'),{code:'ENOENT'});return profiles;},
    stat:async file=>{const f=files.get(path.basename(file));if(!f)throw Object.assign(Error('gone'),{code:'ENOENT'});
      return{mtimeMs:f.mtime,ctimeMs:f.mtime,size:f.size,ino:1,isFile:()=>true};},
    readFile:async file=>{const f=files.get(path.basename(file));return JSON.stringify({version:2,generation:f.generation,changes:f.changes});},
    mkdir:async()=>{},writeFile:async()=>{},rename:async()=>{}};
  const messages=[];
  const win={isDestroyed:()=>false,isVisible:()=>visible,webContents:{isDestroyed:()=>false,send:(_,m)=>messages.push({...m,at:clock})}};
  class FakeDate extends Date{static now(){return clock}}
  const ctx={process:{env:{CODEX_MANAGER_RECORD_SIGNALS:path.join('root','work','control-center','record-signals'),CODEX_MANAGER_PROFILE_ID:me},pid:9},
    Date:FakeDate,setInterval:()=>({unref(){}}),clearInterval(){},
    require:n=>n==='node:fs'?{promises:io}:n==='electron'?{BrowserWindow:{getAllWindows:()=>[win]}}:require(n)};
  vm.createContext(ctx);vm.runInContext(source,ctx);
  const sync=ctx.__codexRecordSync,reads=[],log={observed:[],imported:[]},restored=[],versions=new Map();
  const notFound=new Set(),frozen=new Set(),discard=new Set(),titles=new Map();
  const manager=hostId=>{const m={hostId,hasInFlightConversationResume:()=>false,requestClient:{sendRequest:async()=>({})},
    handleThreadDeletion(){},handleThreadArchived(){},handleThreadUnarchived:id=>restored.push(id),
    threadStore:{threadsById:new Map(),pendingThreadTitlesById:titles,isConversationActive:()=>false,applyThreadTitleUpdate(){},
      async hydrateThreads([id],options){reads.push({id,at:clock});assert.equal(options.includeTurns,false);
        if(notFound.has(id))throw Error('thread not found: '+id);
        // A discarded read (e.g. a newer hydration generation) leaves the entry.
        if(!sync.canApply(m.threadStore)||(discard.has(id)&&m.threadStore.threadsById.has(id)))return;
        if(!frozen.has(id)||!m.threadStore.threadsById.has(id))versions.set(id,(versions.get(id)||0)+1);
        m.threadStore.threadsById.set(id,{id,name:'task',updatedAt:versions.get(id),source:'vscode',turns:[]});}}};
    sync.register(m);return m;};
  const coordinators=new Map();
  sync.catalog({getCoordinator:host=>{if(!coordinators.has(host))coordinators.set(host,coordinator?coordinator(log):new NativeCoordinator(host,log));return coordinators.get(host);}});
  const run=async ms=>{for(let t=0;t<ms;t+=400){clock+=400;await sync.tick();}};
  const at=id=>reads.filter(r=>r.id===id).map(r=>r.at);
  const count=id=>at(id).length;
  const delivered=(id,field='threadIds')=>messages.filter(m=>m[field].includes(id));
  const spaced=(times,gap,label)=>{for(let i=1;i<times.length;i++)assert(times[i]-times[i-1]>=gap,label+': '+times.map(t=>t-times[0]));};
  return {sync,write,emit,run,messages,reads,at,count,delivered,spaced,log,restored,notFound,frozen,discard,titles,manager,
    visible:v=>{visible=v;},advance:ms=>{clock+=ms;},now:()=>clock};
}
const peerFile=peer+'.desktop.json',peer2File=peer2+'.desktop.json';
(async()=>{
  const f=setup({profiles:[me,peer,peer2]});
  const local=f.manager('local'),remote=f.manager(ssh);
  await f.run(400);

  // 1. A hidden profile reads a streaming peer task at most every ~5 s, then
  // exactly once more after the writer is quiet.
  const a=task(1),startA=f.now();
  for(let i=0;i<25;i++){f.emit(peerFile,a);await f.run(400);}
  const streamedA=f.count(a);
  assert(streamedA>=1&&streamedA<=2,'a 10 s stream is re-read every ~5 s, not every flush: '+streamedA);
  assert(f.at(a)[0]-startA<=5600,'the first read of a stream comes within the 5 s bound');
  f.spaced(f.at(a),5000,'stream reads are at least 5 s apart');
  await f.run(2000);assert.equal(f.count(a),streamedA+1,'exactly one more read once the writer stops');
  f.spaced(f.at(a),1500,'reads stay bounded');
  assert.equal(f.log.observed.filter(x=>x===a).length,streamedA+1,'the catalog receives each summary already read');
  assert.deepEqual(f.log.imported,[],'no second catalog thread/read through handleImportedThreads');
  assert.equal(f.delivered(a).length,streamedA+1,'renderers receive one invalidation per read');
  await f.run(10000);assert.equal(f.count(a),streamedA+1);assert.equal(f.sync.status().pending,0);

  // 2. Visible: live notices at least 2 s apart; main still reads only every ~5 s.
  const b=task(2);f.visible(true);const start=f.now();
  for(let i=0;i<25;i++){f.emit(peerFile,b);await f.run(400);}
  f.spaced(f.at(b),5000,'main reads of a stream are at least 5 s apart');
  const notices=f.delivered(b,'liveThreadIds').map(m=>m.at);
  assert(notices.length>=3,'live notices keep an open transcript current: '+notices.length);
  assert(notices[0]-start>=2000,'a single short change gets no live notice');
  f.spaced(notices,2000,'live notices are at least 2 s apart');
  await f.run(2000);assert.equal(f.delivered(b).length,f.count(b));
  const quick=task(3);f.emit(peerFile,quick);await f.run(2000);
  assert.equal(f.count(quick),1);assert.equal(f.delivered(quick,'liveThreadIds').length,0,'one change, one renderer invalidation');
  f.visible(false);

  // 3. Only a shared-host turn this profile streamed itself is not read back.
  const c=task(4);
  f.sync.observe(remote,'turn/completed',{threadId:c});
  f.emit(peerFile,c,ssh);f.emit(peer2File,c,ssh);await f.run(4000);
  assert.equal(f.count(c),0,'republished copies of a turn this profile streamed are not read');
  assert.equal(f.sync.status().selfSkipped,1);
  f.write(me+'.desktop.json',[[task(5),1,ssh,'changed']]);await f.run(4000);
  assert.equal(f.count(task(5)),0,'this profile never reads its own writer file');
  f.advance(10000);f.emit(peerFile,c,ssh);await f.run(2000);
  assert.equal(f.count(c),1,'a later change this profile did not observe is read once');
  const started=task(9);f.sync.observe(remote,'thread/started',{thread:{id:started}});
  for(let i=0;i<5;i++){f.emit(peerFile,started,ssh);await f.run(400);}
  await f.run(2000);assert.equal(f.count(started),1,'a peer first turn after only thread/started here is read');
  const mine=task(10);f.sync.observe(local,'turn/started',{threadId:mine,turn:{id:'own'}});
  f.sync.observe(local,'turn/completed',{threadId:mine,turn:{id:'own'}});
  f.emit(peerFile,mine);await f.run(2000);
  assert.equal(f.count(mine),1,'a peer change right after this profile\'s own local turn is read');
  assert.equal(f.sync.status().selfSkipped,1);

  // 4. Not-found: one bounded retry (a live signal may precede the durable
  // record), then nothing until a newer signal names the task.
  const d=task(6);f.notFound.add(d);f.emit(peerFile,d);await f.run(2000);
  assert.equal(f.count(d),1);await f.run(20000);assert.equal(f.count(d),2,'one retry, then no more');
  f.spaced(f.at(d),3000,'the not-found retry is delayed');assert.equal(f.delivered(d).length,0);
  f.emit(peerFile,d);await f.run(2000);assert.equal(f.count(d),3,'a newer signal reads the task again');
  f.notFound.delete(d);await f.run(4000);
  assert.equal(f.count(d),4);assert.equal(f.delivered(d).length,1,'the durable record arrives on the retry');
  await f.run(10000);assert.equal(f.count(d),4);

  // 5. A read returning the summary already held may precede the writer's
  // durable record: retry once. The retried result goes to a fresh native read.
  const e=task(7);f.emit(peerFile,e);await f.run(2000);assert.equal(f.count(e),1);
  f.frozen.add(e);f.emit(peerFile,e);await f.run(2000);
  assert.equal(f.count(e),2);assert.equal(f.delivered(e).length,1,'an unchanged read is not delivered yet');
  await f.run(2000);assert.equal(f.count(e),3);assert.equal(f.delivered(e).length,2);
  assert.deepEqual(f.log.imported,[e],'after a retry the catalog takes a fresh native read');
  await f.run(10000);assert.equal(f.count(e),3,'one retry at most');
  // A discarded read or a pending local title never feeds the catalog.
  const [p,q]=[task(11),task(12)];f.emit(peerFile,p);f.emit(peerFile,q);await f.run(2000);
  f.discard.add(p);f.titles.set(q,'renaming');f.emit(peerFile,p);f.emit(peerFile,q);await f.run(4000);
  assert(f.log.imported.includes(p),'a read the native store discarded is not imported');
  assert(f.log.imported.includes(q),'a pending local title takes the native path');

  // 6. Writers repeat "unarchived" on every later change of that task. The
  // restore applies once; later copies are ordinary debounced changes.
  const g=task(8);f.emit(peerFile,g,'local','unarchived');await f.run(400);
  assert.deepEqual(f.restored,[g]);assert.equal(f.count(g),0,'the restore itself reads nothing');
  await f.run(2000);assert.equal(f.count(g),1);
  for(let i=0;i<5;i++){f.emit(peerFile,g,'local','unarchived');await f.run(400);}
  await f.run(2000);assert.deepEqual(f.restored,[g]);assert.equal(f.count(g),2);
  assert.equal(f.sync.status().failures,0);f.sync.stop();

  // 7. An unverified coordinator shape always uses the native import path.
  const n=setup({profiles:[me,peer],coordinator:log=>({syncEnabled:true,disposed:false,store:{hostId:'local'},
    observeThread:t=>log.observed.push(t.id),handleImportedThreads:ids=>log.imported.push(...ids)})});
  n.manager('local');await n.run(400);n.emit(peerFile,a);await n.run(2000);
  assert.deepEqual(n.log.observed,[]);assert.deepEqual(n.log.imported,[a]);n.sync.stop();

  // 8. Startup backlog: current profiles and the original app replay once;
  // writers that are not profiles never do.
  const [s1,s2,s3,s4]=[task(20),task(21),task(22),task(23)];
  const h=setup({profiles:[me,peer],initial:[[peer+'.json',[[s1,1,'local','changed']]],[gone+'.json',[[s2,1,'local','changed']]],
    [original+'.json',[[s3,1,'local','changed']],1e12-2*DAY],[me+'.json',[[s4,1,'local','changed']]]]});
  h.manager('local');await h.run(4000);
  assert.equal(h.count(s1),1,'a current profile backlog is read once');
  assert.equal(h.count(s2),0,'a writer that is not a current profile is ignored');
  assert.equal(h.count(s3),1,'the original app writer is trusted whatever its age');
  assert.equal(h.count(s4),0,'this profile runtime file is its own');
  assert.equal(h.sync.status().staleWriters,1);assert.equal(h.sync.status().writers,2);
  h.write(gone+'.json',[[s2,2,'local','changed']]);await h.run(2000);
  assert.equal(h.count(s2),1,'a write seen during this process applies whoever wrote it');
  h.sync.stop();
  // Without a trusted profile list the file age decides for unknown writers.
  const [o1,o2]=[task(30),task(31)];
  const u=setup({profiles:undefined,initial:[[peer+'.json',[[o1,1,'local','changed']],1e12-DAY-1],[peer2+'.json',[[o2,1,'local','changed']],1e12-60000]]});
  u.manager('local');await u.run(4000);
  assert.equal(u.count(o1),0,'no profile list: a writer idle for over a day is ignored');
  assert.equal(u.count(o2),1);assert.equal(u.sync.status().writers,null);u.sync.stop();
  const w=setup({profiles:[peer,gone],initial:[[gone+'.json',[[o1,1,'local','changed']]]]});
  w.manager('local');await w.run(4000);
  assert.equal(w.count(o1),1,'a list that omits this profile is not trusted');w.sync.stop();
  console.log('PASS: one read per settled change (5 s bound while streaming), live notices only while visible, shared-host self-skip only, bounded not-found retry, stale writers skipped, guarded single catalog read');
})().catch(e=>{console.error(e);process.exitCode=1});
