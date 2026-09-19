const fs=require('node:fs'),os=require('node:os'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const root=fs.mkdtempSync(path.join(os.tmpdir(),'local-workspace-share-'));
const source=fs.readFileSync('scripts/manager_core/desktop_signal_files.cjs','utf8')+'\n'+fs.readFileSync('scripts/manager_core/desktop_local_workspace_sync.cjs','utf8');
const id='11111111-1111-4111-8111-111111111111',server='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const project={id,name:'API workspace',rootPaths:['C:/fixture/project'],createdAt:1,updatedAt:2};
function app(writer,projects={}){
  const host='local:C:/fixture/'+writer;
  const state=new Map([['local-projects',projects],['project-order',[]],['selected-project','private'],['auth','SECRET'],
    ['app-server-project-id-by-legacy-project-id-by-host',{[host]:Object.keys(projects).length?{[id]:server}:{}}]]),listeners=new Map(),messages=[];
  const store={getStored:k=>state.get(k),set(k,v){state.set(k,v);listeners.get(k)?.();},onDidChange(k,fn){listeners.set(k,fn);return()=>listeners.delete(k)}};
  const context={process:{pid:writer,env:{CODEX_HOME:host.slice(6),CODEX_RECORD_SIGNALS:root,CODEX_MANAGER_PROFILE_ID:writer}},require,setInterval:()=>({unref(){}})};
  vm.createContext(context);vm.runInContext(source,context);
  const sync=context.__codexLocalWorkspaceSync;sync.register(store,{sendMessageToAllWindows:m=>messages.push(m)});
  return {host,store,state,messages,sync};
}
async function drain(...apps){for(let n=0;n<7;n++){await new Promise(r=>setTimeout(r,8));for(const a of apps)await a.sync.tick();}}
(async()=>{
  const api=app('22222222-2222-4222-8222-222222222222',{[id]:project});await drain(api);
  const gpt=app('33333333-3333-4333-8333-333333333333');await drain(api,gpt);
  assert.equal(gpt.state.get('local-projects')[id].name,'API workspace');
  assert.equal(gpt.state.get('app-server-project-id-by-legacy-project-id-by-host')[gpt.host][id],server);
  const backend={cache:{hostId:'local'},projectsReady:true,connected:true,pendingProjectWrites:new Map(),connectionLifetime:{signal:{}},migrationIdentity:gpt.host,
    serverProjectsByLegacyId:new Map(),legacyProjectIdsByServerId:new Map(),async listProjects(){return[{id:server,name:'native'}]},
    storeProject(p){this.serverProjectsByLegacyId.set(this.legacyProjectIdsByServerId.get(p.id)||p.id,p)}};
  gpt.sync.registerBackend(backend);await drain(gpt);assert(backend.serverProjectsByLegacyId.has(id),'peer project can be edited via its native ID');
  api.store.set('local-projects',{[id]:{...project,name:'Renamed'}});await drain(api,gpt);
  assert.equal(gpt.state.get('local-projects')[id].name,'Renamed');
  api.store.set('local-projects',{});await drain(api,gpt);
  assert.equal(Object.keys(gpt.state.get('local-projects')).length,0);
  const stale=app('44444444-4444-4444-8444-444444444444',{[id]:project});await drain(api,gpt,stale);
  assert.equal(Object.keys(stale.state.get('local-projects')).length,0,'cold profile cannot resurrect a deletion');
  assert.equal(gpt.state.get('selected-project'),'private');assert.equal(gpt.state.get('auth'),'SECRET');
  api.store.set('local-projects',{[id]:project});await drain(api,gpt);assert(gpt.state.get('local-projects')[id],'explicit native undo is shared');
  const next=app('55555555-5555-4555-8555-555555555555');await drain(next);
  gpt.sync.register(next.store,{sendMessageToAllWindows:m=>next.messages.push(m)});await drain(api,gpt);
  api.store.set('local-projects',{});await drain(api,gpt);assert(!next.state.get('local-projects')[id],'login store replacement remains subscribed');
  const contents=fs.readdirSync(path.join(root,'local-workspaces')).map(n=>fs.readFileSync(path.join(root,'local-workspaces',n),'utf8')).join('');
  assert(!contents.includes('SECRET'));assert(!contents.includes('private'));
  console.log('PASS: API/GPT local project add/rename/delete/undo, native edit IDs, cold tombstones and login rebinding');
})().catch(e=>{console.error(e);process.exitCode=1}).finally(()=>fs.rmSync(root,{recursive:true,force:true}));
