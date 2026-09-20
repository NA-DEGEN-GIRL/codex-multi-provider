const fs=require('node:fs'),os=require('node:os'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const root=fs.mkdtempSync(path.join(os.tmpdir(),'ssh-declaration-recovery-'));
const user=path.join(root,'user'),donorHome=path.join(user,'.codex'),home=path.join(root,'profile');
const signals=path.join(root,'signals'),sshKey='codex-managed-remote-connections',autoKey='remote-connection-auto-connect-by-host-id';
const profile='22222222-2222-4222-8222-222222222222';
const connection=alias=>({hostId:'remote-ssh-discovered:'+alias,displayName:alias,source:'discovered',alias,hostname:null,sshPort:null,identity:null});
const declarations=['build','render','dev','disabled','removed'].map(connection);
const manual={hostId:'remote-ssh-managed:manual',displayName:'Private server',source:'codex-managed',alias:null,hostname:'private.example'};
const donor={
  [sshKey]:declarations.map(item=>({...item,connectionAnalyticsId:'DONOR-TRACKING'})),
  [autoKey]:Object.fromEntries(declarations.map(item=>[item.hostId,item.alias!=='disabled']))
};
const metadata={workspace:{[sshKey]:Object.fromEntries(declarations.map(item=>[item.hostId,item]))}};
fs.mkdirSync(donorHome,{recursive:true});fs.mkdirSync(home);
const donorPath=path.join(donorHome,'.codex-global-state.json');
fs.writeFileSync(donorPath,JSON.stringify(donor));
fs.writeFileSync(path.join(home,'.manager-app-preferences.json'),JSON.stringify(metadata));
const source=fs.readFileSync('scripts/manager_core/desktop_signal_files.cjs','utf8')+'\n'+fs.readFileSync('scripts/manager_core/desktop_workspace_sync.cjs','utf8');
const state=new Map([
  [sshKey,[...declarations,manual]],[autoKey,{...donor[autoKey]}],['remote-projects',[]],['project-order',[]],
  ['selected-remote-host-id',declarations[2].hostId],['private-draft','DO NOT TOUCH']
]);
const listeners=new Map(),messages=[];
let writes=0,refreshes=0,now=0;
let refreshBehavior=()=>Promise.resolve();
const store={
  getStored:key=>state.get(key),
  set(key,value){writes++;state.set(key,value);listeners.get(key)?.();},
  onDidChange(key,fn){listeners.set(key,fn);return()=>listeners.delete(key);}
};
const context={
  process:{pid:123,env:{CODEX_HOME:home,CODEX_RECORD_SIGNALS:signals,CODEX_MANAGER_PROFILE_ID:profile}},
  require:name=>name==='node:os'?{homedir:()=>user}:require(name),setInterval:()=>({unref(){}}),
  Date:class extends Date{static now(){return now;}}
};
vm.createContext(context);vm.runInContext(source,context);
const sync=context.__codexWorkspaceSync;
const windows={sendMessageToAllWindows:message=>messages.push(message)};
const native={refreshRemoteConnections(){refreshes++;return refreshBehavior();}};
sync.register(store,windows,native);
async function drain(){for(let i=0;i<8;i++){await new Promise(resolve=>setTimeout(resolve,8));await sync.tick();}}
(async()=>{
  await drain();
  assert.equal(writes,0,'unchanged startup must not rewrite declarations');
  assert.equal(refreshes,0);
  // Native save accepts a stale partial list and clears the omitted auto keys.
  // The donor has since removed one old import, which must remain removed.
  donor[sshKey]=donor[sshKey].filter(item=>item.alias!=='removed');
  delete donor[autoKey][declarations[4].hostId];
  fs.writeFileSync(donorPath,JSON.stringify(donor));
  const survivor={...declarations[2],displayName:'Dev (profile edit)',connectionAnalyticsId:'PROFILE-TRACKING'};
  store.set(sshKey,[survivor,manual]);
  store.set(autoKey,{[declarations[1].hostId]:false});
  await drain();
  const healed=state.get(sshKey),byAlias=new Map(healed.map(item=>[item.alias,item]));
  assert.deepEqual([...byAlias.keys()].sort((a,b)=>String(a).localeCompare(String(b))),['build','dev','disabled',null,'render'].sort((a,b)=>String(a).localeCompare(String(b))));
  assert.deepEqual(byAlias.get('dev'),survivor,'surviving profile edits and analytics stay local');
  assert.deepEqual(byAlias.get(null),manual,'manual hosts stay local');
  assert.equal(byAlias.get('build').connectionAnalyticsId,undefined,'donor analytics must never be copied');
  assert.equal(byAlias.has('removed'),false,'donor removals must not be restored from old import metadata');
  assert.equal(state.get(autoKey)[declarations[0].hostId],true);
  assert.equal(state.get(autoKey)[declarations[1].hostId],false,'explicit profile OFF must survive');
  assert.equal(state.get(autoKey)[declarations[2].hostId],true,'surviving alias regains missing auto preference');
  assert.equal(state.get(autoKey)[declarations[3].hostId],false,'donor OFF must be restored as OFF');
  assert.equal(state.get(autoKey)[declarations[4].hostId],undefined);
  assert.equal(state.get('private-draft'),'DO NOT TOUCH');
  assert.equal(state.get('selected-remote-host-id'),declarations[2].hostId);
  assert.equal(refreshes,1,'repair refreshes the native shared connection cache once');
  assert(messages.some(message=>message.type==='global-state-updated'&&message.keys.includes(sshKey)&&message.keys.includes(autoKey)));
  const stable=[writes,messages.length,refreshes];
  await drain();
  assert.deepEqual([writes,messages.length,refreshes],stable,'unchanged repair must not create an event loop');
  store.set(autoKey,{...state.get(autoKey),[declarations[0].hostId]:false});
  const afterEdit=[writes,messages.length,refreshes];
  await drain();
  assert.equal(state.get(autoKey)[declarations[0].hostId],false);
  assert.deepEqual([writes,messages.length,refreshes],afterEdit,'explicit OFF is not a repair request');
  // A rejected cache refresh remains pending even though all settings healed.
  now+=1000;
  let attempts=0;
  refreshBehavior=()=>++attempts===1?Promise.reject(Error('temporary refresh failure')):Promise.resolve();
  store.set(sshKey,state.get(sshKey).filter(item=>item.alias!=='build'));
  await drain();
  assert.equal(attempts,1);
  const afterFailure=[writes,messages.length];
  await drain();
  assert.equal(attempts,1,'failed refresh must respect the cooldown');
  now+=1000;
  await drain();
  assert.equal(attempts,2,'a later tick retries the native cache without another settings change');
  assert.deepEqual([writes,messages.length],afterFailure,'retry must not rewrite settings or repeat notifications');
  now+=1000;
  await drain();
  assert.equal(attempts,2,'successful refresh consumes pending work');
  // An unresolved native promise neither overlaps retries nor stalls projects.
  let completeRefresh;
  refreshBehavior=()=>new Promise(resolve=>{completeRefresh=resolve;});
  store.set(sshKey,state.get(sshKey).filter(item=>item.alias!=='build'));
  await drain();
  const duringRefresh=refreshes;
  const project={id:'11111111-1111-4111-8111-111111111111',hostId:declarations[2].hostId,remotePath:'/tmp/project',label:'Active project'};
  store.set('remote-projects',[project]);
  now+=5000;
  await drain();
  assert.equal(refreshes,duringRefresh,'pending native refreshes must not overlap');
  assert(state.get('project-order').includes(project.id),'shared project synchronization continues during cache refresh');
  completeRefresh();await drain();
  // Registration can precede availability of the native connection handler.
  const replacement={...store};
  sync.register(replacement,windows);
  store.set(sshKey,state.get(sshKey).filter(item=>item.alias!=='build'));
  await drain();
  const beforeHandler=[writes,messages.length,refreshes];
  refreshBehavior=()=>Promise.resolve();
  now+=1000;
  sync.register(replacement,windows,native);
  await drain();
  assert.equal(refreshes,beforeHandler[2]+1,'late handler must fulfill existing refresh intent');
  assert.deepEqual([writes,messages.length],beforeHandler.slice(0,2));
  assert.equal(fs.existsSync(path.join(home,'.codex-global-state.json')),false,'only the native store may persist live settings');
  console.log('PASS: SSH declaration recovery, native refresh retry/cooldown/no overlap/late handler, nonblocking project sync, explicit OFF, donor removal, no event loop');
})().catch(error=>{console.error(error);process.exitCode=1}).finally(()=>{
  assert.equal(path.dirname(path.resolve(root)),path.resolve(os.tmpdir()));
  fs.rmSync(root,{recursive:true,force:true});
});
