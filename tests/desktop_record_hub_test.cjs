// Record hub client (stage 2b/3b): while the manager service's hub answers,
// peers' changes arrive over one pipe connection and no peer file is scanned.
// Every failure falls back to the files at once. Real named pipes, fake clock.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),net=require('node:net'),crypto=require('node:crypto'),assert=require('node:assert/strict');
const source=fs.readFileSync('scripts/manager_core/desktop_signal_files.cjs','utf8')+'\n'+fs.readFileSync('scripts/manager_core/desktop_record_sync.cjs','utf8');
const task=n=>n.toString(16).padStart(8,'0')+'-1111-4111-8111-111111111111';
const [me,peer]=['aaaaaaaa-0000-4000-8000-000000000001','bbbbbbbb-0000-4000-8000-000000000002'];
const peerFile=peer+'.desktop.json',ssh='remote-ssh-discovered:shared',dir='fixture',hubFile=path.join(dir,'status',me+'.hub.json');
const until=async(check,label,ms=5000)=>{const end=Date.now()+ms;while(!check()){if(Date.now()>end)throw Error('timed out: '+label);await new Promise(r=>setTimeout(r,5));}};
const ok=result=>({ok:true,result});
// The service's pipe-name format; anything else keeps the adapter on files.
const pipeName=()=>'CodexControlCenter.service.'+crypto.randomBytes(16).toString('hex').toUpperCase();
// A scripted hub. respond(request) returns a reply, or undefined to hold it.
async function hubServer(respond){
  const name=pipeName(),requests=[],held=[],sockets=new Set();
  const server=net.createServer(socket=>{sockets.add(socket);let buffer='';
    socket.on('data',data=>{buffer+=data;let end;
      while((end=buffer.indexOf('\n'))>=0){const request=JSON.parse(buffer.slice(0,end));buffer=buffer.slice(end+1);requests.push(request);
        const reply=respond(request,socket);
        if(reply===undefined)held.push({request,socket});else socket.write(JSON.stringify({id:request.id,...reply})+'\n');}});
    socket.on('error',()=>{});socket.on('close',()=>sockets.delete(socket));});
  await new Promise(r=>server.listen('\\\\.\\pipe\\'+name,r));
  const answer=reply=>{for(const entry of held.splice(0))if(!entry.socket.destroyed)entry.socket.write(JSON.stringify({id:entry.request.id,...(typeof reply==='function'?reply(entry.request):reply)})+'\n');};
  return {name,requests,held,sockets,answer,polls:()=>requests.filter(r=>r.command==='records.poll'),
    publishes:()=>requests.filter(r=>r.command==='records.publish'),
    close:()=>new Promise(r=>{for(const s of sockets)s.destroy();server.close(r);})};
}
// A journal like the service's: reset on an unknown epoch, long polls held.
function journal(epoch='E1',head=0){
  const state={epoch,head,events:[],publish:()=>null};
  state.respond=request=>{
    if(request.command==='records.publish')return state.publish(request)??ok({accepted:request.args.events.length});
    const {epoch:e,cursor,wait_ms}=request.args;
    if(e!==state.epoch||cursor>state.head)return ok({epoch:state.epoch,cursor:state.head,reset:true,events:[],more:false});
    const events=state.events.filter(x=>x.seq>cursor);
    return events.length||!wait_ms?ok({epoch:state.epoch,cursor:state.head,reset:false,events,more:false}):undefined;
  };
  return state;
}
const deliver=(s,state,...events)=>{for(const e of events){state.head=e.seq;state.events.push({origins:[peer],...e});}s.answer(state.respond);};
// Long polls the hub lets expire (20 s each) while the fake clock moves on.
const idle=async(x,s,state,ms)=>{for(let t=0;t<ms;t+=20000){await until(()=>s.held.length>=1,'held poll');x.advance(20000);
  s.answer(()=>ok({epoch:state.epoch,cursor:state.head,reset:false,events:[],more:false}));}await until(()=>s.held.length>=1,'held poll');};
// Lets pipe I/O land before asserting that something was NOT sent.
const settle=()=>new Promise(r=>setTimeout(r,40));
let stamp=0;
function setup({pipe,peers=[],saved,disk=new Map(),frozen=[],missing:gone=[]}={}){
  let clock=1e12;
  const calls={lists:0,peerReads:0},put=(file,text)=>disk.set(file,{text,stamp:++stamp});
  const writePeer=(name,changes)=>put(path.join(dir,name),JSON.stringify({version:2,generation:'g-'+name,changes}));
  for(const [name,changes] of peers)writePeer(name,changes);
  if(saved)put(hubFile,JSON.stringify(saved));
  const missing=()=>Object.assign(Error('gone'),{code:'ENOENT'});
  // calls.profiles: the manager's profile folders (a profile created later joins it).
  const io={readdir:async d=>{if(path.basename(d)==='profiles')return calls.profiles||[me,peer];calls.lists++;
      if(calls.fail>0&&calls.fail--)throw Object.assign(Error('sharing violation'),{code:'EBUSY'});
      return [...disk.keys()].filter(k=>path.dirname(k)===d).map(k=>path.basename(k));},
    stat:async f=>{const e=disk.get(f);if(!e)throw missing();return{mtimeMs:e.stamp,ctimeMs:e.stamp,size:e.text.length,ino:1,isFile:()=>true};},
    // gate: hold one file's read; readFail: peer reads refused (mid-replace or held by a scanner).
    readFile:async f=>{const e=disk.get(f);if(!e)throw missing();if(calls.gate&&f===calls.gate.file){calls.gate.hit=true;await calls.gate.promise;}
      if(path.dirname(f)===dir&&calls.readFail>0&&calls.readFail--)throw Object.assign(Error('denied'),{code:'EPERM'});
      if(path.dirname(f)===dir)calls.peerReads++;return e.text;},
    mkdir:async()=>{},writeFile:async(f,t)=>put(f,t),rename:async(a,b)=>{put(b,disk.get(a).text);disk.delete(a);}};
  const win={isDestroyed:()=>false,isVisible:()=>false,webContents:{isDestroyed:()=>false,send(){}}};
  class FakeDate extends Date{static now(){return clock}}
  const env={CODEX_MANAGER_RECORD_SIGNALS:dir,CODEX_MANAGER_PROFILE_ID:me};if(pipe)env.CODEX_MANAGER_RECORDS_PIPE=pipe;
  const ctx={process:{env,pid:9},Date:FakeDate,setInterval:()=>({unref(){}}),clearInterval(){},
    require:n=>n==='node:fs'?{promises:io}:n==='electron'?{BrowserWindow:{getAllWindows:()=>[win]}}:require(n)};
  vm.createContext(ctx);vm.runInContext(source,ctx);
  const sync=ctx.__codexRecordSync,reads=[],deleted=[];
  const manager=hostId=>{const m={hostId,hasInFlightConversationResume:()=>false,requestClient:{sendRequest:async()=>({})},
    handleThreadDeletion:ids=>deleted.push(...ids),handleThreadArchived(){},handleThreadUnarchived(){},
    // A frozen task's summary does not move (a read racing its writer).
    threadStore:{threadsById:new Map(frozen.map(id=>[id,{id,name:'task',updatedAt:0}])),isConversationActive:()=>false,applyThreadTitleUpdate(){},
      async hydrateThreads([id]){reads.push(id);if(gone.includes(id))throw Error('thread not found: '+id);if(!frozen.includes(id))m.threadStore.threadsById.set(id,{id,name:'task',updatedAt:reads.length});}}};
    sync.register(m);return m;};
  sync.catalog({getCoordinator:()=>({handleImportedThreads(){}})});
  const run=async ms=>{for(let t=0;t<ms;t+=400){clock+=400;await sync.tick();}};
  return {sync,run,reads,deleted,calls,disk,writePeer,manager,count:id=>reads.filter(r=>r===id).length,
    saved:()=>disk.has(hubFile)?JSON.parse(disk.get(hubFile).text):undefined,advance:ms=>{clock+=ms;},
    own:()=>JSON.parse(disk.get(path.join(dir,me+'.desktop.json'))?.text||'{"changes":[]}').changes};
}
// Review regressions (clientreview repro2 A/B, repro4, epochs, resume, reset races).
async function regressions(){
  // 9. A peer file refused during the reset catch-up (EPERM mid-replace, held
  // by a scanner, even past the reader's own 3-strike give-up) is retried until
  // one pass reads every file; no cursor is saved before that.
  {const st=journal('E1',4),srv=await hubServer(r=>st.respond(r));
    const f=setup({pipe:srv.name,peers:[[peerFile,[[task(100),1,'local','changed'],[task(101),2,'local','changed']]]]});
    f.manager('local');f.calls.readFail=4;
    await until(()=>f.sync.status().hub==='on','on');await f.run(400);
    assert.equal(f.count(task(100)),0);assert.equal(f.saved(),undefined,'no cursor is saved past a failed catch-up');
    await f.run(12000);
    assert.equal(f.count(task(100)),1);assert.equal(f.count(task(101)),1,'the refused file is read once it is readable');
    assert.equal(f.sync.status().hubCatchUps,1);assert.deepEqual(f.saved(),{version:1,epoch:'E1',cursor:4});
    f.sync.stop();await srv.close();}
  // 10. 1500 undelivered events on resume (512 per page): the pipeline never
  // evicts a hub report; the excess waits in the inbox and pins the cursor.
  {const N=1500,events=[];for(let i=1;i<=N;i++)events.push({host:'local',id:task(1000+i),kind:'changed',seq:i,origins:[peer]});
    const srv=await hubServer(r=>{if(r.command!=='records.poll')return ok({accepted:0});const c=r.args.cursor;
      const page=events.filter(e=>e.seq>c).slice(0,512),more=page.length===512&&page.at(-1).seq<N;
      if(!page.length&&r.args.wait_ms)return undefined;
      return ok({epoch:'E1',cursor:more?page.at(-1).seq:N,reset:false,events:page,more});});
    const f=setup({pipe:srv.name,saved:{version:1,epoch:'E1',cursor:0}});f.manager('local');
    await until(()=>f.sync.status().hubEvents>=N,'all delivered');
    let most=0,early;
    for(let i=0;i<20;i++){await f.run(20000);most=Math.max(most,f.sync.status().pending);if(i===2)early=f.saved()?.cursor??0;}
    const read=new Map();for(const id of f.reads)read.set(id,(read.get(id)||0)+1);
    assert.equal(read.size,N,'every delivered event is read');assert([...read.values()].every(n=>n===1),'once each (resumed changes are durable)');
    assert(most<=1024,'the pipeline stays bounded: '+most);assert(early<N,'the cursor waited for unread reports: '+early);
    assert.deepEqual(f.saved(),{version:1,epoch:'E1',cursor:N});f.sync.stop();await srv.close();}
  // 11. A reply landing while a snapshot is being built confirms only the
  // previous, lagged snapshot, never the files just read (repro4).
  {const other=peer+'.json',st=journal('E1',0),srv=await hubServer(r=>st.respond(r));
    const f=setup({pipe:srv.name,saved:{version:1,epoch:'E1',cursor:0},
      peers:[[peerFile,[[task(1),1,'local','changed']]],[other,[[task(2),1,'local','changed']]]]});
    f.manager('local');const empty=()=>ok({epoch:'E1',cursor:0,reset:false,events:[],more:false});
    await until(()=>f.sync.status().hub==='on','on');await f.run(400);
    await until(()=>srv.held.length>=1,'held');await f.run(10000);srv.answer(empty);
    await until(()=>srv.held.length>=1,'held2');await f.run(19600);
    f.writePeer(peerFile,[[task(1),1,'local','changed'],[task(70),2,'local','changed']]);
    f.writePeer(other,[[task(2),1,'local','changed'],[task(71),2,'local','changed']]);
    let open;f.calls.gate={file:path.join(dir,other),promise:new Promise(r=>open=r)};
    const pass=f.run(400);await until(()=>f.calls.gate.hit,'gated');
    srv.answer(empty);await settle();open();await pass;f.calls.gate=null;
    await srv.close();await until(()=>f.sync.status().hub==='fallback','fallback');await f.run(8000);
    assert.equal(f.count(task(70)),1);assert.equal(f.count(task(71)),1,'neither undelivered entry is hidden');f.sync.stop();}
  // 12. A second reset during a catch-up gets its own pass; a report queued
  // under an older epoch keeps pinning the cursor of the new one.
  {const other=peer+'.json',st=journal('E1',4),srv=await hubServer(r=>st.respond(r));
    const f=setup({pipe:srv.name,peers:[[peerFile,[[task(80),1,'local','changed']]],[other,[[task(81),1,'local','changed']]]]});f.manager('local');
    let open;f.calls.gate={file:path.join(dir,other),promise:new Promise(r=>open=r)};
    await until(()=>f.sync.status().hub==='on','on');
    const pass=f.run(400);await until(()=>f.calls.gate.hit,'first catch-up in progress');
    await until(()=>srv.held.length===1,'long poll');st.epoch='E2';st.head=0;srv.answer(st.respond);
    await until(()=>f.sync.status().hubResets===2,'second reset');open();await pass;f.calls.gate=null;
    assert.equal(f.saved(),undefined,'nothing is saved between the two resets');
    await f.run(4000);
    assert.equal(f.sync.status().hubCatchUps,2,'the second reset gets its own catch-up');
    assert.deepEqual(f.saved(),{version:1,epoch:'E2',cursor:0});assert.equal(f.count(task(80)),1);assert.equal(f.count(task(81)),1);
    deliver(srv,st,{host:'remote-ssh-discovered:other',id:task(82),kind:'changed',seq:7});
    await until(()=>srv.held.length===1&&srv.held[0].request.args.cursor===7,'poll');await f.run(2400);
    assert.deepEqual(f.saved(),{version:1,epoch:'E2',cursor:6});
    st.epoch='E3';st.head=2;st.events=[];srv.answer(st.respond);await until(()=>f.sync.status().hubResets===3,'third reset');
    await f.run(4000);assert.equal(f.saved().epoch,'E2','an old-epoch report still pins after a reset');
    f.sync.stop();await srv.close();}
  // 13. Changes redelivered on resume are durable: read once, like the
  // backlog. A live change whose summary has not moved yet is retried once.
  {const st=journal('E1',1),srv=await hubServer(r=>st.respond(r));st.events=[{host:'local',id:task(90),kind:'changed',seq:1,origins:[peer]}];
    const f=setup({pipe:srv.name,saved:{version:1,epoch:'E1',cursor:0},frozen:[task(90),task(91)]});f.manager('local');
    await until(()=>f.sync.status().hub==='on','on');await f.run(8000);
    assert.equal(f.count(task(90)),1,'a resumed change is read once');
    deliver(srv,st,{host:'local',id:task(91),kind:'changed',seq:2});await until(()=>srv.held.length===1&&srv.held[0].request.args.cursor===2,'poll');
    await f.run(8000);assert.equal(f.count(task(91)),2,'a live change keeps its one retry');f.sync.stop();await srv.close();}
  // Second review (finalreview client_probe*.cjs).
  // 14. A flood the pipeline cannot hold, then a peer's new file entries and a
  // fallback: nothing is evicted; files wait for room and resume where they
  // stopped; the cursor never passes an unread report.
  {const N=1500,events=[];for(let i=1;i<=N;i++)events.push({host:'local',id:task(1000+i),kind:'changed',seq:i,origins:[peer]});
    const srv=await hubServer(r=>{if(r.command!=='records.poll')return ok({accepted:0});const c=r.args.cursor;
      const page=events.filter(e=>e.seq>c).slice(0,512),more=page.length===512&&page.at(-1).seq<N;
      if(!page.length&&r.args.wait_ms)return undefined;
      return ok({epoch:'E1',cursor:more?page.at(-1).seq:N,reset:false,events:page,more});});
    const f=setup({pipe:srv.name,saved:{version:1,epoch:'E1',cursor:0}});f.manager('local');
    await until(()=>f.sync.status().hubEvents>=N,'all delivered');await f.run(4000);
    assert.equal(f.sync.status().pending,1024);
    const fresh=[];for(let i=1;i<=100;i++)fresh.push([task(5000+i),i,'local','changed']);
    f.writePeer(peerFile,fresh);await srv.close();await until(()=>f.sync.status().hub==='fallback','fallback');
    let most=0;for(let i=0;i<40;i++){await f.run(20000);most=Math.max(most,f.sync.status().pending);
      if(i===1)assert(f.saved().cursor<N,'the cursor waits for unread reports');}
    const read=new Map();for(const id of f.reads)read.set(id,(read.get(id)||0)+1);
    assert(events.every(e=>read.get(e.id)===1),'every hub report is read once');assert(fresh.every(([id])=>read.get(id)===1),'every file entry is read once');
    assert(most<=1024);assert.deepEqual(f.saved(),{version:1,epoch:'E1',cursor:N});f.sync.stop();}
  // 15. A catch-up re-lists the profiles: one created after startup is a
  // current writer, not a stale file.
  {const late='cccccccc-0000-4000-8000-000000000003',st=journal('E1',0),srv=await hubServer(r=>st.respond(r));
    const f=setup({pipe:srv.name,peers:[[peerFile,[[task(1),1,'local','changed']]]]});f.manager('local');
    // The startup reset lists the profiles; the late one is created after that.
    await until(()=>f.sync.status().hub==='on','on');await f.run(800);assert.equal(f.sync.status().hubCatchUps,1);
    f.calls.profiles=[me,peer,late];f.writePeer(late+'.json',[[task(900),1,'local','changed']]);
    await until(()=>srv.held.length>=1,'held');st.epoch='E2';st.head=0;srv.answer(st.respond);
    await until(()=>f.sync.status().hubResets>=2,'reset');await f.run(8000);
    assert.equal(f.count(task(900)),1,'a profile created after startup is read');assert.equal(f.sync.status().staleWriters,0);
    f.sync.stop();await srv.close();}
  // 16. Failed directory listings during a catch-up never age out a queued hub
  // report: it is read once its host's manager registers.
  {const st=journal('E1',0),srv=await hubServer(r=>st.respond(r));
    const f=setup({pipe:srv.name,saved:{version:1,epoch:'E1',cursor:0}}),local=f.manager('local');
    await until(()=>f.sync.status().hub==='on','on');await f.run(800);await until(()=>srv.held.length>=1,'held');
    deliver(srv,st,{host:'ssh-b',id:task(1),kind:'changed',seq:1});
    await until(()=>f.sync.status().hubEvents>=1,'event');await f.run(1200);await until(()=>srv.held.length>=1,'held2');
    st.epoch='E2';st.head=0;st.events=[];srv.answer(st.respond);await until(()=>f.sync.status().hubResets>=1,'reset');
    f.calls.fail=6;await f.run(4000);
    assert.equal(f.sync.status().pending,1,'six failed listings keep the report');assert.equal(f.saved().epoch,'E1');
    // Another report that keeps failing to apply is charged alone and dropped.
    local.handleThreadDeletion=()=>{throw Error('native failure');};
    await until(()=>srv.held.length>=1,'held3');deliver(srv,st,{host:'local',id:task(2),kind:'deleted',seq:1});
    await until(()=>f.sync.status().hubEvents>=2,'deletion');await f.run(20000);assert.equal(f.sync.status().pending,1,'only the failing report is dropped');
    f.manager('ssh-b');await f.run(4000);
    assert.equal(f.count(task(1)),1);assert.equal(f.saved().epoch,'E2');f.sync.stop();await srv.close();}
  // 17. A change redelivered on reconnect (it happened while away) never turns
  // a still-live entry for the same task durable: its not-found retry stays.
  {const st=journal('E1',0),srv=await hubServer(r=>st.respond(r)),x=task(95);
    const f=setup({pipe:srv.name,saved:{version:1,epoch:'E1',cursor:0},missing:[x]});f.manager('local');
    await until(()=>f.sync.status().hub==='on','on');await until(()=>srv.held.length>=1,'held');
    deliver(srv,st,{host:'ssh-c',id:x,kind:'changed',seq:1});await until(()=>f.sync.status().hubEvents>=1,'live event');await f.run(400);
    for(const socket of srv.sockets)socket.destroy();await until(()=>f.sync.status().hub==='fallback','fallback');
    st.head=2;st.events.push({host:'ssh-c',id:x,kind:'changed',seq:2,origins:[peer]});
    await f.run(30400);await until(()=>f.sync.status().hubEvents>=2,'redelivered');
    f.manager('ssh-c');await f.run(8000);
    assert.equal(f.count(x),2,'the live not-found retry survives a durable redelivery');f.sync.stop();await srv.close();}
}
(async()=>{
  // 1. First start (no saved cursor): the hub resets, which runs the file
  // backlog exactly once. After that no peer file is scanned.
  const state=journal('E1',4),s=await hubServer(r=>state.respond(r));
  const f=setup({pipe:s.name,peers:[[peerFile,[[task(1),1,'local','changed'],[task(2),2,'local','changed']]]]});
  f.manager('local');
  await until(()=>f.sync.status().hub==='on','first answer');
  assert.deepEqual(s.requests[0].args,{profile:me,wait_ms:0},'no epoch yet, no broker token');
  assert(!JSON.stringify(s.requests).includes('token'));
  await f.run(4000);
  assert.equal(f.sync.status().hubResets,1);assert.equal(f.sync.status().hubCatchUps,1,'one catch-up');
  assert.equal(f.count(task(1)),1);assert.equal(f.count(task(2)),1,'the backlog is read once');
  await until(()=>s.held.length===1,'long poll');
  assert.deepEqual(s.held[0].request.args,{profile:me,epoch:'E1',cursor:4,wait_ms:20000});
  assert.deepEqual(f.saved(),{version:1,epoch:'E1',cursor:4},'the cursor is saved once the catch-up is read');
  const lists=f.calls.lists,peerReads=f.calls.peerReads;
  f.writePeer(peerFile,[[task(1),1,'local','changed'],[task(2),2,'local','changed'],[task(4),3,'local','changed']]);
  await f.run(20000);
  assert.equal(f.calls.lists,lists,'hub mode: no peer directory scans');assert.equal(f.calls.peerReads,peerReads,'and no peer file reads');
  assert.equal(f.count(task(4)),0,'a file entry is not read while the hub serves');

  // 2. Hub events enter the same pipeline once: after a short quiet window,
  // deletions without a read, one read per event.
  deliver(s,state,{host:'local',id:task(3),kind:'changed',seq:5},{host:'local',id:task(5),kind:'deleted',seq:6});
  await until(()=>s.held.length===1&&s.held[0].request.args.cursor===6,'next poll');
  await f.run(400);assert.equal(f.count(task(3)),0,'hub events still wait a short quiet window');
  await f.run(1600);assert.equal(f.count(task(3)),1);assert.deepEqual(f.deleted,[task(5)]);assert.equal(f.count(task(5)),0);
  await f.run(10000);assert.equal(f.count(task(3)),1,'one read per event');
  // A queued report (no manager serves that host yet) pins the saved cursor.
  deliver(s,state,{host:'remote-ssh-discovered:other',id:task(6),kind:'changed',seq:7});
  await until(()=>s.held.length===1&&s.held[0].request.args.cursor===7,'next poll');
  await f.run(4000);assert.equal(f.saved().cursor,6,'the saved cursor never passes an unprocessed report');
  await idle(f,s,state,600000);await f.run(2400);assert.equal(f.saved().cursor,7,'a report parked for 10 min no longer pins it');
  assert.equal(f.sync.status().hub,'on');f.sync.stop();

  // 3. Restart with the saved cursor: no startup backlog replay at all.
  const mark=s.polls().length,g=setup({pipe:s.name,disk:f.disk});g.manager('local');
  await until(()=>g.sync.status().hub==='on','resume');
  assert.deepEqual(s.polls()[mark].args,{profile:me,epoch:'E1',cursor:7,wait_ms:0});
  await g.run(4000);assert.deepEqual(g.reads,[],'a valid cursor skips the backlog');assert.equal(g.sync.status().hubCatchUps,0);
  deliver(s,state,{host:'local',id:task(8),kind:'changed',seq:8});await until(()=>s.held.length===1&&s.held[0].request.args.cursor===8,'poll');
  await g.run(2000);assert.deepEqual(g.reads,[task(8)]);
  // The startup snapshot becomes the fallback baseline after a later complete
  // reply; a fallback then replays only file entries written after it.
  g.advance(15000);deliver(s,state,{host:'local',id:task(9),kind:'changed',seq:9});
  await until(()=>s.held.length===1&&s.held[0].request.args.cursor===9,'poll');await g.run(4000);
  g.writePeer(peerFile,[[task(1),1,'local','changed'],[task(2),2,'local','changed'],[task(4),3,'local','changed'],[task(10),4,'local','changed']]);
  for(const socket of s.sockets)socket.destroy();
  await until(()=>g.sync.status().hub==='fallback','connection loss');
  assert.match(g.sync.status().hubReason,/closed|error/);
  await g.run(400);assert.equal(g.count(task(10)),0,'after a confirmed baseline, newer file entries are live changes');
  await g.run(3600);
  assert.equal(g.count(task(10)),1,'the fallback reads what the hub may not have delivered');
  assert.equal(g.count(task(1))+g.count(task(4)),0,'but not the baseline the hub already covered');
  const retry=s.polls().length;await g.run(20000);await settle();assert.equal(s.polls().length,retry,'no retry before 30 s');
  await g.run(8000);await until(()=>g.sync.status().hub==='on','retried after 30 s');
  assert.equal(s.polls().at(-1).args.epoch,'E1');

  // 4. A reset mid-session runs exactly one catch-up, then polls the new epoch.
  state.epoch='E2';state.head=0;state.events=[];s.answer(state.respond);
  await until(()=>g.sync.status().hubResets===1,'reset');
  const before=g.calls.lists;g.writePeer(peerFile,[[task(1),1,'local','changed'],[task(4),3,'local','changed'],[task(10),4,'local','changed'],[task(11),5,'local','changed']]);
  // A failed catch-up is retried, and the new epoch is not saved before it ran.
  g.calls.fail=1;await g.run(400);assert.equal(g.saved().epoch,'E1');assert.equal(g.sync.status().hubCatchUps,0);
  await g.run(8000);
  assert.equal(g.calls.lists-before,2,'one catch-up scan (after one failed attempt)');assert.equal(g.count(task(11)),1);assert.equal(g.count(task(10)),1);
  await until(()=>s.held.length===1,'poll');assert.deepEqual(s.held[0].request.args,{profile:me,epoch:'E2',cursor:0,wait_ms:20000});
  await g.run(4000);assert.deepEqual(g.saved(),{version:1,epoch:'E2',cursor:0});

  // 5. {closing:true} falls back at once and the hub is retried later.
  s.answer({ok:true,result:{closing:true,epoch:'E2',cursor:0,reset:false,events:[],more:false}});
  await until(()=>g.sync.status().hub==='fallback','closing');assert.equal(g.sync.status().hubReason,'closing');
  const scans=g.calls.lists;await g.run(2000);assert(g.calls.lists>scans,'files are scanned again');
  await g.run(30000);await until(()=>g.sync.status().hub==='on','back after closing');g.sync.stop();

  // A snapshot is the fallback baseline only after REBASE_LAG_MS: an entry the
  // hub had not delivered yet is still read from the files after a fallback.
  const q=setup({pipe:s.name,disk:new Map([[hubFile,{text:JSON.stringify({version:1,epoch:'E2',cursor:0}),stamp:0}]]),
    peers:[[peerFile,[[task(60),1,'local','changed']]]]});q.manager('local');
  await until(()=>q.sync.status().hub==='on','q on');await q.run(400);
  q.advance(5000);deliver(s,state,{host:'local',id:task(61),kind:'changed',seq:1});
  await until(()=>s.held.some(h=>!h.socket.destroyed&&h.request.args.cursor===1),'q poll');await q.run(4000);
  for(const socket of s.sockets)socket.destroy();await until(()=>q.sync.status().hub==='fallback','q fallback');
  await q.run(2000);assert.equal(q.count(task(60)),1,'an unconfirmed snapshot never hides an undelivered entry');q.sync.stop();
  state.events=[];

  // 6. Own reports: batched at most every 400 ms, re-sent after records_busy
  // backoff, and stopped for the session on profile_mismatch.
  let mode='busy';state.publish=()=>{const code={busy:'records_busy',invalid:'invalid_request',mismatch:'profile_mismatch'}[mode];
    if(mode!=='mismatch')mode='ok';return code?{ok:false,error:{code,message:'x'}}:null;};
  const p=setup({pipe:s.name,disk:new Map([[hubFile,{text:JSON.stringify({version:1,epoch:'E2',cursor:0}),stamp:0}]])});p.manager(ssh);
  await until(()=>p.sync.status().hub==='on','publisher on');
  for(let i=1;i<=5;i++)p.sync.publish(task(20+i),ssh);
  // Hosts the hub would refuse (control characters, over 256 UTF-8 bytes) never join a batch.
  p.sync.publish(task(30),'local');p.sync.publish(task(31),'bad'+String.fromCharCode(10)+'host');p.sync.publish(task(32),'remote-ssh-discovered:'+'\uD55C'.repeat(80));
  await p.run(400);await until(()=>s.publishes().length===1,'one batch');
  assert.deepEqual(s.publishes()[0].args,{profile:me,events:[1,2,3,4,5].map(i=>({host:ssh,id:task(20+i),kind:'changed'}))},'one batch; local tasks are the proxy\'s');
  await until(()=>p.sync.status().hubPublishBusy===1,'busy');
  p.sync.publish(task(26),ssh);
  await p.run(800);await settle();assert.equal(s.publishes().length,1,'records_busy backs off');
  await p.run(400);await until(()=>s.publishes().length===2,'resent');
  assert.equal(s.publishes()[1].args.events.length,6,'the refused batch is re-sent with the new report');
  await until(()=>p.sync.status().hubPublished===6,'accepted');
  p.sync.publish(task(27),ssh);await p.sync.tick();await settle();assert.equal(s.publishes().length,2,'at most one batch per 400 ms');
  await p.run(400);await until(()=>s.publishes().length===3,'third');await until(()=>p.sync.status().hubPublished===7,'accepted');
  // invalid_request drops that batch only; publishing continues.
  mode='invalid';p.sync.publish(task(33),ssh);await p.run(400);await until(()=>p.sync.status().hubPublishDropped===1,'dropped');
  p.sync.publish(task(34),ssh);await p.run(400);await until(()=>p.sync.status().hubPublished===8,'publishing continues');
  assert.equal(p.sync.status().hubPublish,'on');
  mode='mismatch';p.sync.publish(task(28),ssh);await p.run(400);await until(()=>p.sync.status().hubPublish==='stopped','mismatch');
  p.sync.publish(task(29),ssh);await p.run(2000);await settle();assert.equal(s.publishes().length,6,'no publish after profile_mismatch');
  assert(p.own().some(c=>c[0]===task(29)),'the own file still carries every report');
  assert.equal(p.sync.status().hub,'on','polling continues');p.sync.stop();await s.close();

  // 7. unknown_command (an older service): files at once, including the
  // startup backlog, and a retry 30 s later.
  let known=false;const state2=journal('E9',0);
  const u=await hubServer(r=>known?state2.respond(r):{ok:false,error:{code:'unknown_command',message:'x'}});
  const h=setup({pipe:u.name,peers:[[peerFile,[[task(40),1,'local','changed']]]]});h.manager('local');
  await until(()=>h.sync.status().hub==='fallback','unknown_command');assert.equal(h.sync.status().hubReason,'unknown_command');
  await h.run(2000);assert.equal(h.count(task(40)),1,'the startup backlog comes from the files');
  known=true;await h.run(30000);await until(()=>h.sync.status().hub==='on','retry');
  await h.run(4000);assert.equal(h.count(task(40)),1,'the reset catch-up re-reads nothing the files delivered');
  // A request without an answer before its deadline also falls back.
  await until(()=>u.held.length===1,'long poll');h.advance(30001);await h.sync.tick();
  assert.equal(h.sync.status().hub,'fallback');assert.equal(h.sync.status().hubReason,'timeout');h.sync.stop();await u.close();

  // 8. A first answer that never comes, a service that is not running, and an
  // unbounded line all fall back without blocking anything.
  const silent=await hubServer(()=>undefined);
  const t=setup({pipe:silent.name,peers:[[peerFile,[[task(50),1,'local','changed']]]]});t.manager('local');
  await until(()=>silent.requests.length===1,'first poll');await t.run(2800);assert.equal(t.count(task(50)),0,'files wait for the first answer');
  await t.run(400);assert.equal(t.sync.status().hub,'fallback');await t.run(800);assert.equal(t.count(task(50)),1);t.sync.stop();await silent.close();
  const absent=setup({pipe:pipeName(),peers:[[peerFile,[[task(51),1,'local','changed']]]]});absent.manager('local');
  await until(()=>absent.sync.status().hub==='fallback','no service');await absent.run(800);assert.equal(absent.count(task(51)),1);absent.sync.stop();
  const flood=await hubServer((_,socket)=>{socket.write(Buffer.alloc(9*1024*1024,32));});
  const o=setup({pipe:flood.name});await until(()=>o.sync.status().hub==='fallback','oversized',15000);
  assert.equal(o.sync.status().hubReason,'oversized');o.sync.stop();await flood.close();
  // No pipe name, or anything but the exact service pipe-name format, keeps stage 1 unchanged.
  const valid=pipeName();
  for(const pipe of [undefined,'..\\x','a/b','codex-test-'+crypto.randomUUID(),valid.toLowerCase(),valid+'0','\\\\.\\pipe\\'+valid]){const n=setup({pipe});assert.equal(n.sync.status().hub,'off');n.sync.stop();}
  await regressions();
  console.log('PASS: hub replaces peer scans; events read once; reset -> one catch-up; saved cursor skips the backlog and never passes unprocessed reports; closing/unknown_command/loss/timeout/oversize fall back and retry; batched publish with busy backoff and mismatch stop');
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(()=>setTimeout(()=>process.exit(),50).unref());
