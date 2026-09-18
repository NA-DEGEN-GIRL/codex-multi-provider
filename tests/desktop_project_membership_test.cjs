const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const id='11111111-1111-4111-8111-111111111111',project='22222222-2222-4222-8222-222222222222';
const state=new Map([['thread-project-assignments',{}],['projectless-thread-ids',[id]],['selected-project','KEEP']]);
const database=new Map([[id,null]]),requests=[];let reject=false,commits=0;
const source=fs.readFileSync('scripts/manager_core/desktop_project_membership.cjs','utf8');
const disk=new Map();let stamp=0;
const io={async mkdir(){},async readdir(){return [...disk.keys()].filter(p=>p.endsWith('.json')).map(p=>require('node:path').basename(p));},
 async stat(p){return{isFile:()=>true,mtimeMs:stamp,size:Buffer.byteLength(disk.get(p))};},async readFile(p){if(!disk.has(p))throw Error('missing');return disk.get(p);},
 async writeFile(p,value){disk.set(p,value);stamp++;},async rename(a,b){disk.set(b,disk.get(a));disk.delete(a);stamp++;}};
const context={process:{pid:42,env:{CODEX_RECORD_SIGNALS:'fixture'}},require:n=>n==='node:fs'?{promises:io}:require(n),setInterval:()=>({unref(){}})};vm.createContext(context);vm.runInContext(source,context);
const backend={cache:{hostId:'local',getProjects:()=>({[project]:{id:project}})},disposed:false,
 migrationIdentity:'local:fixture',
 threadAssignmentsEnabled:false,projectSupport:'supported',serverProjectsByLegacyId:new Map([[project,{id:project}]]),
 legacyProjectIdsByServerId:new Map([[project,project]]),globalState:{getStored:k=>state.get(k),set:(k,v)=>state.set(k,v)},
 async ensureProjectsReady(){},async writeThreadAssignment(t,a,commit){await commit();},observeThreads(){},
 connection:{async sendAppServerRequest(method,params){requests.push(method);
  if(reject)throw Error('fixture write rejected');
  if(method==='thread/metadata/update')database.set(params.threadId,params.projectId||null);
  return{thread:{id:params.threadId,projectId:database.get(params.threadId)}};
 }},threadAssignments:{matches:(t,p)=>(state.get('thread-project-assignments')[t]?.projectId||null)===p,
 adopt(t,p){const a={...state.get('thread-project-assignments')};if(p)a[t]={projectKind:'local',projectId:p};else delete a[t];state.set('thread-project-assignments',a);}}};
const sync=context.__codexProjectMembership;sync.register(backend);
const assignment={projectKind:'local',projectId:project};
const commit=async()=>{commits++;state.set('thread-project-assignments',{[id]:assignment});};
(async()=>{
 state.set('thread-project-assignments',{[id]:assignment});
 backend.observeThreads([{id,projectId:null}]);
 for(let i=0;i<5;i++){await new Promise(r=>setImmediate(r));await sync.refresh(backend);}
 assert.equal(state.get('thread-project-assignments')[id]?.projectId,project,'a mode-triggered NULL observation cannot erase unimported legacy membership');
 assert.equal(database.get(id),null,'protecting legacy state does not manufacture a native move');
 const protectedReads=requests.length;
 for(let i=0;i<10;i++){backend.observeThreads([{id,projectId:null}]);await sync.refresh(backend);await sync.tick();}
 assert.equal(requests.length,protectedReads,'repeated blocked NULL listings do not cause more RPCs');
 state.set('app-server-projects-migration-by-host',{'local:fixture':{threadAssignmentsReadMigrated:true,pendingThreadAssignmentIds:[id]}});
 backend.threadAssignments.adopt(id,null);
 assert.equal(state.get('thread-project-assignments')[id]?.projectId,project,'completed read migration does not erase a still-pending legacy assignment');
 assert.deepEqual(state.get('app-server-projects-migration-by-host')['local:fixture'].pendingThreadAssignmentIds,[id]);
 backend.threadAssignmentsEnabled=true;backend.threadAssignments.adopt(id,null);
 assert.equal(state.get('thread-project-assignments')[id]?.projectId,project,'native enabled observer is guarded too');
 backend.threadAssignmentsEnabled=false;
 reject=true;await assert.rejects(()=>backend.writeThreadAssignment(id,assignment,commit,undefined,'local'));
 assert.equal(commits,0);assert.equal(database.get(id),null);
 backend.threadAssignments.adopt(id,null);
 assert.equal(state.get('thread-project-assignments')[id]?.projectId,project,'failed write cannot authorize a removal');
 reject=false;await backend.writeThreadAssignment(id,assignment,commit,undefined,'local');
 assert.equal(database.get(id),project);assert.equal(commits,1);
 await sync.tick();
 assert([...disk.values()].some(s=>JSON.parse(s).thread_ids?.includes(id)),'successful native assignment is durably confirmed');
 database.set(id,null);backend.observeThreads([{id,projectId:null}]);
 for(let i=0;i<5;i++){await new Promise(r=>setImmediate(r));await sync.refresh(backend);}
 assert.equal(state.get('thread-project-assignments')[id],undefined);
 assert.equal(state.get('selected-project'),'KEEP');
 database.set(id,project);reject=true;backend.observeThreads([{id,projectId:project}]);
 await new Promise(r=>setImmediate(r));reject=false;
 for(let i=0;i<5;i++){await new Promise(r=>setImmediate(r));await sync.refresh(backend);}
 assert.equal(state.get('thread-project-assignments')[id]?.projectId,project,'transient read failure retries');
 const count=requests.length;
 await backend.writeThreadAssignment(id,{projectKind:'remote',projectId:project},async()=>{},undefined,'remote-ssh-fixture');
 assert.equal(requests.length,count,'remote drag remains on native path');
 await backend.writeThreadAssignment(id,assignment,async()=>{},null,'local');
 assert.equal(requests.length,count,'prewarmed/new task remains on creation path');
 // A reopened peer reads confirmation before treating NULL as a real removal.
 const peerContext={...context,process:{pid:43,env:{CODEX_RECORD_SIGNALS:'fixture',CODEX_MANAGER_PROFILE_ID:'33333333-3333-4333-8333-333333333333'}}};
 vm.createContext(peerContext);vm.runInContext(source,peerContext);
 state.set('thread-project-assignments',{[id]:assignment});database.set(id,null);
 const peer={...backend,threadAssignments:{matches:backend.threadAssignments.matches,adopt(t,p){if(p===null)state.set('thread-project-assignments',{});}},
   connection:{async sendAppServerRequest(method,params){return{thread:{id:params.threadId,projectId:database.get(params.threadId)}};}}};
 peerContext.__codexProjectMembership.register(peer);
 for(let i=0;i<5;i++){await new Promise(r=>setImmediate(r));await peerContext.__codexProjectMembership.tick();}
 peer.threadAssignments.adopt(id,null);
 assert.equal(state.get('thread-project-assignments')[id],undefined,'a new peer accepts a confirmed native removal');
 console.log('PASS: mode-switch NULL protection, native/legacy observers, durable proof, reopened peer removal, explicit moves and failed-write atomicity');
})().catch(e=>{console.error(e);process.exitCode=1});
