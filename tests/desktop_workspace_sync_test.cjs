const fs=require('node:fs'),os=require('node:os'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const root=fs.mkdtempSync(path.join(os.tmpdir(),'workspace-share-'));
const source=fs.readFileSync('scripts/manager_core/desktop_workspace_sync.cjs','utf8');
const project={id:'11111111-1111-4111-8111-111111111111',hostId:'remote-ssh-discovered:remote-dev',remotePath:'/home/test/example-game',label:'example-game'};
function app(writer,projects=[]){
  const state=new Map([['remote-projects',projects],['project-order',['local-project']],['selected-project',{id:'private-selection'}],['auth','PRIVATE']]),listeners=new Map(),messages=[];
  const store={getStored:k=>state.get(k),set(k,v){state.set(k,v);listeners.get(k)?.();},onDidChange(k,fn){listeners.set(k,fn);return()=>listeners.delete(k)}};
  const context={process:{pid:writer,env:{CODEX_RECORD_SIGNALS:root,CODEX_MANAGER_PROFILE_ID:writer}},require,setInterval:()=>({unref(){}})};
  vm.createContext(context);vm.runInContext(source,context);
  const sync=context.__codexWorkspaceSync;sync.register(store,{sendMessageToAllWindows:m=>messages.push(m)});
  return {store,state,messages,sync};
}
async function drain(...apps){for(let n=0;n<8;n++){await new Promise(r=>setTimeout(r,8));for(const a of apps)await a.sync.tick();}}
(async()=>{
 const a=app('22222222-2222-4222-8222-222222222222',[project]);
 await drain(a);
 const b=app('33333333-3333-4333-8333-333333333333');await drain(a,b);
 assert.equal(b.state.get('remote-projects')[0].label,'example-game');
 b.store.set('project-order',['local-project']);await drain(a,b);
 assert(b.state.get('project-order').includes(project.id),'late native migration must not hide an unchanged SSH declaration');
 const replacement=app('55555555-5555-4555-8555-555555555555');
 b.sync.register(replacement.store,{sendMessageToAllWindows:m=>replacement.messages.push(m)});await drain(a,b);
 assert.equal(replacement.state.get('remote-projects')[0].label,'example-game','login replacement store must receive the shared declarations');
 b.sync.register(b.store,{sendMessageToAllWindows:m=>b.messages.push(m)});await drain(a,b);
 assert.equal(b.state.get('selected-project').id,'private-selection');
 assert.equal(b.state.get('auth'),'PRIVATE');
 a.store.set('remote-projects',[{...project,label:'renamed'}]);await drain(a,b);
 assert.equal(b.state.get('remote-projects')[0].label,'renamed');
 b.store.set('remote-projects',[]);await drain(a,b);
 assert.equal(a.state.get('remote-projects').length,0);
 const stale=app('44444444-4444-4444-8444-444444444444',[project]);await drain(a,b,stale);
 assert.equal(stale.state.get('remote-projects').length,0,'stale launch cannot resurrect a deleted project');
 assert(a.state.get('project-order').includes('local-project'));
 assert(!a.state.get('project-order').includes(project.id));
 assert(b.messages.some(m=>m.type==='workspace-root-options-updated'));
 const files=fs.readdirSync(path.join(root,'workspaces')).map(n=>fs.readFileSync(path.join(root,'workspaces',n),'utf8')).join('');
 assert(!files.includes('PRIVATE'));assert(!files.includes('private-selection'));
 console.log('PASS: cross-profile SSH project add/rename/remove, durable tombstones, selection/auth isolation');
})().catch(e=>{console.error(e);process.exitCode=1}).finally(()=>fs.rmSync(root,{recursive:true,force:true}));
