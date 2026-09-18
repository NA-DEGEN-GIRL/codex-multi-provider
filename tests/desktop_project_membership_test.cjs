const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const id='11111111-1111-4111-8111-111111111111',project='22222222-2222-4222-8222-222222222222';
const state=new Map([['thread-project-assignments',{}],['projectless-thread-ids',[id]],['selected-project','KEEP']]);
const database=new Map([[id,null]]),requests=[];let reject=false,commits=0;
const source=fs.readFileSync('scripts/manager_core/desktop_project_membership.cjs','utf8');
const context={process:{env:{CODEX_RECORD_SIGNALS:'fixture'}}};vm.createContext(context);vm.runInContext(source,context);
const backend={cache:{hostId:'local',getProjects:()=>({[project]:{id:project}})},disposed:false,
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
 reject=true;await assert.rejects(()=>backend.writeThreadAssignment(id,assignment,commit,undefined,'local'));
 assert.equal(commits,0);assert.equal(database.get(id),null);
 reject=false;await backend.writeThreadAssignment(id,assignment,commit,undefined,'local');
 assert.equal(database.get(id),project);assert.equal(commits,1);
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
 console.log('PASS: common move persistence, failed-write atomicity, peer removal projection, selection and native remote/creation paths');
})().catch(e=>{console.error(e);process.exitCode=1});
