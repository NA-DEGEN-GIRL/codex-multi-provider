const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[2], 'utf8');
const id = '11111111-1111-4111-8111-111111111111';
const profile = '22222222-2222-4222-8222-222222222222.json';
let stamp = 0, seq = 0, calls = [], busy = false, whileReading = null;
const missing = '33333333-3333-4333-8333-333333333333';
let includeMissing = false, changeHost='local', deletion=false,kind='changed';
const data = () => JSON.stringify({version:1,generation:'first',changes:deletion?[[id,seq,changeHost,'deleted']]:includeMissing?[[missing,seq],[id,seq]]:[[id,seq,changeHost,kind]]});
const published=[]; const diagnostics=[];const deliveries=[];
const io = { readdir:async()=>[profile,'invalid.json'],stat:async()=>({mtimeMs:stamp,size:100,isFile:()=>true}),
 readFile:async()=>data(), mkdir:async()=>{},writeFile:async(file,body)=>(file.includes('status')?diagnostics:published).push(JSON.parse(body)),rename:async()=>{} };
const context = {process:{env:{CODEX_RECORD_SIGNALS:'fixture'}},require:n=>n==='electron'?{BrowserWindow:{getAllWindows:()=>[{isDestroyed:()=>false,webContents:{isDestroyed:()=>false,send:(channel,payload)=>deliveries.push({channel,payload})}}]}}:n==='node:fs'?{promises:io}:require(n),
 setInterval:()=>({unref(){}}),clearInterval(){},Date,Set,Map,WeakMap};
vm.createContext(context);vm.runInContext(fs.readFileSync('scripts/manager_core/desktop_profile_resume.cjs','utf8'),context);vm.runInContext(source,context);
 const sync=context.__codexRecordSync;
let state = {resumeState:'resumed',threadRuntimeStatus:{type:'idle'},draft:'keep my typing',model:'own-model'};
let finishRequest;
const manager={hostId:'local',getConversation:()=>state,hasInFlightConversationResume:()=>false,
 requestClient:{sendRequest:(method,params)=>method==='config/read'?Promise.resolve({config:{model:'gpt-native',model_provider:'openai',model_reasoning_effort:'high'}}):method==='thread/resume'?Promise.resolve(params):new Promise(resolve=>{finishRequest=resolve})},
 threadStore:{threadsById:new Map([[id,{name:'title'}]]),isConversationActive:()=>true,
  applyThreadTitleUpdate:(id,title)=>calls.push(['title',id,title]),
  hydrateThreads:async(ids,options)=>{
   calls.push(['read',...ids]);assert.equal(options.includeTurns,true);assert.equal(options.retainHistoryPagination,true);
   if(whileReading)whileReading();
   if(sync.canApply(manager.threadStore))calls.push(['applied',...ids]);
  }}};
sync.register(manager);
sync.catalog({getCoordinator:host=>{assert.equal(host,'local');return{handleImportedThreads:ids=>calls.push(['list',...ids])}}});
(async()=>{
 stamp++;seq++;await sync.tick();
 assert(calls.some(c=>c[0]==='list'));assert(calls.some(c=>c[0]==='applied'));
 assert(deliveries.some(x=>x.channel==='codex_desktop:message-for-view'&&x.payload.threadIds.includes(id)));
 assert.equal(state.draft,'keep my typing');assert.equal(state.model,'own-model');
 calls=[];state.threadRuntimeStatus.type='active';stamp++;seq++;await sync.tick();
 assert(calls.some(c=>c[0]==='applied'),'remote active snapshot must keep refreshing');
 sync.observe(manager,'turn/started',{threadId:id,turn:{id:'local'}});
 calls=[];stamp++;seq++;await sync.tick();
 assert(!calls.some(c=>c[0]==='read'),'local native stream must not be hydrated');
 sync.observe(manager,'turn/completed',{threadId:id,turn:{id:'local'}});
 const request=manager.requestClient.sendRequest('turn/start',{threadId:id});
 calls=[];stamp++;seq++;await sync.tick();
 assert(!calls.some(c=>c[0]==='read'),'local start request before notification must be protected');
 finishRequest({turn:{id:'next'}});await request;
 whileReading=()=>{sync.observe(manager,'turn/started',{threadId:id,turn:{id:'next'}})};
 calls=[];stamp++;seq++;await sync.tick();
 assert(calls.some(c=>c[0]==='read'));assert(!calls.some(c=>c[0]==='applied'),'original start during I/O must win');
 whileReading=null;sync.observe(manager,'turn/completed',{threadId:id,turn:{id:'next'}});
 published.length=0;
 sync.observe(manager,'item/agentMessage/delta',{threadId:id,delta:'private text stays local'});
 await sync.tick();assert.equal(published.length,1);
 assert.equal(published[0].changes[0][0],id);
 const firstSeq=published[0].changes[0][1];
 assert(!JSON.stringify(published).includes('private text'));
 sync.observe(manager,'thread/project/updated',{threadId:id,projectId:'native-audio'});
 await sync.tick();assert.equal(published.length,2);
 assert.deepEqual(published[1].changes,[[id,firstSeq+1,'local','changed']]);
 assert(!JSON.stringify(published).includes('native-audio'),'only invalidations cross windows, no stale assignment replay');
 sync.observe(manager,'turn/completed',{threadId:id});
  manager.threadStore.threadsById.set(id,{name:'title',modelProvider:'external-api'});
  const resumed=await manager.requestClient.sendRequest('thread/resume',{threadId:id,model:'api-test-model'});
  assert.equal(resumed.modelProvider,'openai');assert.equal(resumed.model,'gpt-native');
  assert.equal(resumed.config.model_reasoning_effort,'high');
  includeMissing=true;calls=[];stamp++;seq++;await sync.tick();
  assert(calls.some(c=>c[0]==='applied' && c[1]===id),'missing task cannot block readable task');
  assert(!calls.some(c=>c[0]==='list' && c.includes(missing)),'missing task cannot enter native infinite import retry');
  assert(!deliveries.some(x=>x.payload.threadIds.includes(missing)),'unpersisted ID never enters renderer retry loop');
  sync.publish(id);await sync.tick();assert(published.at(-1).changes.some(row=>row[0]===id));
  assert.equal(sync.status().failures,0);assert(sync.status().unavailable>0);
  includeMissing=false;const visibility=[];
  manager.handleThreadArchived=id=>visibility.push(['archived',id]);manager.handleThreadUnarchived=id=>visibility.push(['restored',id]);
  kind='archived';stamp++;seq++;calls=[];await sync.tick();assert.deepEqual(visibility,[['archived',id]]);
  assert(!calls.some(c=>c[0]==='read'));assert(deliveries.at(-1).payload.archivedThreadIds.includes(id));
  kind='changed';stamp++;seq++;calls=[];await sync.tick();assert(!calls.some(c=>c[0]==='read'),'late reads cannot re-add an archived task');
  kind='unarchived';stamp++;seq++;await sync.tick();assert.deepEqual(visibility.at(-1),['restored',id]);
  assert(deliveries.at(-1).payload.unarchivedThreadIds.includes(id));kind='changed';
  const deleted=[];manager.handleThreadDeletion=ids=>deleted.push(['local',...ids]);
  const remote={...manager,hostId:'remote-ssh-discovered:remote-dev',requestClient:{sendRequest:async()=>({})},handleThreadDeletion:ids=>deleted.push(['ssh',...ids])};sync.register(remote);
  includeMissing=false;changeHost=remote.hostId;deletion=true;calls=[];stamp++;seq++;await sync.tick();
  assert.deepEqual(deleted,[['ssh',id]]);assert(!calls.some(c=>c[0]==='read'&&c[1]===id));
  assert(deliveries.some(x=>x.payload.hostId===remote.hostId&&x.payload.deletedThreadIds.includes(id)));
  deletion=false;stamp++;seq++;calls=[];await sync.tick();assert(!calls.some(c=>c[0]==='read'),'late change cannot resurrect a deleted task');
  remote.disposed=true;
  manager.disposed=true;await sync.tick();assert.equal(sync.status().managers,0);
  sync.register(manager);assert.equal(sync.status().managers,0,'disposed manager must release its transcript graph');
  sync.stop();console.log('PASS: changed list/history refresh; original running turn, draft and model preserved; in-flight race guarded');
})().catch(e=>{console.error(e);process.exitCode=1});
