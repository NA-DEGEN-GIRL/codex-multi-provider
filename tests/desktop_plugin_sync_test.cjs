const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync('scripts/manager_core/desktop_plugin_sync.cjs','utf8');
const rendererSource=fs.readFileSync('scripts/manager_core/desktop_plugin_renderer_sync.cjs','utf8');
const directory=path.join('fixture','record-signals'),file=path.join(directory,'plugins.json');
const disk=new Map(),sent=[];
let modified=1,now=1000,sendFails=false;
const windows=[{isDestroyed:()=>false,webContents:{isDestroyed:()=>false,send:(channel,payload)=>{
  if(sendFails)throw Error('renderer unavailable');sent.push({channel,payload});}}}];
const io={async stat(target){if(!disk.has(target)){const error=Error('missing');error.code='ENOENT';throw error;}
    return{isFile:()=>true,size:Buffer.byteLength(disk.get(target)),mtimeMs:modified};},
  async readFile(target){if(!disk.has(target))throw Error('missing');return disk.get(target);}};
const context={process:{env:{CODEX_RECORD_SIGNALS:directory}},
  require:name=>name==='node:fs'?{promises:io}:name==='electron'?{BrowserWindow:{getAllWindows:()=>windows}}:require(name),
  setInterval:()=>({unref(){}}),clearInterval:()=>{},setTimeout:()=>({unref(){}}),clearTimeout:()=>{},Date:{now:()=>now},console};
vm.createContext(context);vm.runInContext(source,context);
const sync=context.__codexPluginSync;
const settle=async()=>{for(let i=0;i<12;i++)await new Promise(step=>setImmediate(step));};
function manager(hostId='local'){
  const state={hostId,disposed:false,fail:false,calls:[],
    requestClient:{async sendRequest(method,params,options){state.calls.push({method,params,options});
      if(state.fail)throw Error('runtime unavailable');
      return{data:[]};}}};
  return state;
}
const publish=revision=>{disk.set(file,JSON.stringify({version:1,revision}));modified++;};
(async()=>{
  const local=manager();
  sync.register(local);
  await settle();
  assert.equal(local.calls.length,0,'a missing revision file must not reload');

  publish('r1');
  await sync.tick();await settle();
  assert.equal(JSON.stringify(local.calls.map(call=>[call.method,call.params,call.options&&call.options.priority])),
    JSON.stringify([['skills/list',{cwds:[],forceReload:true},'background']]),'native reload uses a bounded background request');
  assert.equal(JSON.stringify(sent),JSON.stringify([{channel:'codex_desktop:message-for-view',
    payload:{type:'manager-plugins-invalidated',hostId:'local'}}]));

  // Re-scanning the same revision, and rewriting the same revision, stay once.
  await sync.tick();modified++;await sync.tick();await settle();
  assert.equal(local.calls.length,1,'one native reload per revision and client');
  assert.equal(sent.length,1);

  publish('r2');
  await sync.tick();await settle();
  assert.equal(local.calls.length,2);
  assert.equal(sent.length,2);

  // A client registered after the revision already applied reloads once.
  const late=manager();
  sync.register(late);
  await settle();
  assert.equal(late.calls.length,1);
  assert.equal(sent.length,3,'reopened clients refresh the committed revision');

  // A runtime failure retries with backoff and never invalidates early.
  const flaky=manager();flaky.fail=true;sync.register(flaky);
  await settle();
  assert.equal(flaky.calls.length,1,'a new client reloads the committed revision');
  assert.equal(flaky.calls[0].params.forceReload,true);
  assert.equal(sent.length,3,'failed reload must not invalidate the renderer');
  await sync.tick();await settle();
  assert.equal(flaky.calls.length,1,'backoff suppresses immediate retries');
  flaky.fail=false;now+=60000;
  await sync.tick();await settle();
  assert.equal(flaky.calls.length,2);
  assert.equal(sent.length,4);

  // A blocked window broadcast keeps the revision pending and retries the
  // invalidation only: the native cache reload is not repeated.
  publish('r3');
  sendFails=true;
  const blocked=manager();sync.register(blocked);
  await settle();
  const blockedReloads=blocked.calls.length;
  assert.equal(blockedReloads,1);
  assert.equal(blocked.calls[0].params.cwds.length,0);
  assert.equal(sent.filter(entry=>entry.payload&&entry.payload.type==='manager-plugins-invalidated').length,4);
  now+=60000;sendFails=false;
  await sync.tick();await settle();
  assert.equal(blocked.calls.length,blockedReloads,'a failed broadcast must not reload native caches again');
  assert(sent.some(entry=>entry.payload&&entry.payload.type==='manager-plugins-invalidated'));

  // A revision published before any window exists stays pending, is retried
  // without repeating the native reload, and is delivered once a window opens.
  const parked=windows.splice(0,windows.length);
  publish('r4');
  const windowless=manager();sync.register(windowless);
  await settle();
  assert.equal(windowless.calls.length,1,'the native reload still runs without windows');
  const beforeWindow=sent.length;
  await sync.tick();await settle();
  assert.equal(windowless.calls.length,1,'a windowless retry must not reload native caches again');
  assert.equal(sent.length,beforeWindow);
  assert(sync.status().pending>0,'a windowless revision stays pending');
  windows.push(parked[0]);
  await sync.tick();await settle();
  assert.equal(windowless.calls.length,1,'delivery after the window opens must not reload again');
  assert(sent.length>beforeWindow,'the pending revision is delivered when a window appears');

  // Repeated runtime failures keep the revision pending with a capped backoff
  // instead of dropping and immediately re-queueing it.
  const dead=manager();dead.fail=true;sync.register(dead);
  await settle();
  const deadCalls=dead.calls.length;
  for(let cycle=0;cycle<8;cycle++){now+=60000;await sync.tick();await settle();}
  const capped=dead.calls.length;
  assert(capped>deadCalls,'capped retries keep trying after the retry budget');
  assert(sync.status().pending>0,'a failing revision is retained, not dropped');
  await sync.tick();await settle();
  assert.equal(dead.calls.length,capped,'retries stay backed off while the runtime is disconnected');

  // Disposed and replaced clients are skipped, including mid-flight disposal.
  const disposed=manager();disposed.disposed=true;sync.register(disposed);
  const replaced=manager();
  replaced.requestClient.sendRequest=async function(method,params,options){
    replaced.calls.push({method,params,options});
    replaced.disposed=true;
    return{data:[]};
  };
  sync.register(replaced);
  publish('r5');
  await sync.tick();await settle();
  assert.equal(disposed.calls.length,0,'a disposed manager is never reloaded');
  assert.equal(replaced.calls.length,1);
  assert.equal(replaced.events,undefined);

  // Remote execution hosts keep their own runtimes: no request, no broadcast.
  const remote=manager('remote-ssh-fixture');sync.register(remote);
  publish('r6');
  await sync.tick();await settle();
  assert.equal(remote.calls.length,0,'remote managers keep their own runtime');
  assert(sent.every(entry=>entry.payload.hostId==='local'),'broadcasts stay local to the manager that reloaded');

  // Malformed payloads are ignored without touching the runtime.
  const total=local.calls.length;
  disk.set(file,JSON.stringify({version:2,revision:'r7'}));modified++;
  await sync.tick();await settle();
  disk.set(file,'x'.repeat(8192));modified++;
  await sync.tick();await settle();
  disk.set(file,'{not json');modified++;
  await sync.tick();await settle();
  assert.equal(local.calls.length,total,'invalid revisions are not reloaded');

  // Only the native skills/list reload is issued: never extra roots, plugin
  // reconciliation, navigation or any other runtime mutation.
  const methods=[...local.calls,...late.calls,...flaky.calls,...blocked.calls].map(call=>call.method);
  assert(methods.every(method=>method==='skills/list'),methods.join(','));
  const payloads=JSON.stringify([...local.calls,...late.calls,...flaky.calls,...blocked.calls]);
  assert(!payloads.includes('extraRoots')&&!payloads.includes('reconcile')&&!payloads.includes('annotations'));
  const status=sync.status();
  assert(status.reloads>=5&&status.invalidations>=3&&status.managers>=3,JSON.stringify(status));
  console.log('PASS: plugin revision scan, once-per-client reload, invalidation retry, backoff, dispose, remote skip and no extra roots');

  // Renderer half: the window message must replay through the renderer's own
  // app-server manager, which is where the react-query subscribers live.
  const listeners=[],localEvents=[],remoteEvents=[];
  const rendererWindow={addEventListener(type,listener){if(type==='message')listeners.push(listener);}};
  const rendererContext={window:rendererWindow,console};
  vm.createContext(rendererContext);vm.runInContext(rendererSource,rendererContext);
  const renderer=rendererContext.__codexPluginRendererSync;
  renderer.register({hostId:'local',disposed:false,events:{emitNotification:n=>localEvents.push(n)}});
  renderer.register({hostId:'remote-ssh-fixture',disposed:false,events:{emitNotification:n=>remoteEvents.push(n)}});
  renderer.register({hostId:'local',disposed:true,events:{emitNotification:()=>{throw Error('disposed manager used');}}});
  assert.equal(listeners.length,1);
  listeners[0]({data:{type:'other-message'}});
  listeners[0]({data:{type:'manager-plugins-invalidated',hostId:'local'}});
  assert.equal(JSON.stringify(localEvents),JSON.stringify([{method:'skills/changed',params:{}}]));
  assert.equal(remoteEvents.length,0,'remote hosts keep their own skill caches');
  assert.equal(renderer.status().invalidations,1);
  // A manager without events.emitNotification is never counted as delivered,
  // and the local invalidation stays pending until one can serve it.
  renderer.register({hostId:'local-nofn',disposed:false,events:{}});
  listeners[0]({data:{type:'manager-plugins-invalidated',hostId:'local-nofn'}});
  assert.equal(renderer.status().invalidations,1,'a missing emitter is not counted');
  assert(renderer.status().skipped>=1);
  assert.equal(renderer.status().pending,1,'the invalidation is retained locally');
  const lateEvents=[];
  renderer.register({hostId:'local-nofn',disposed:false,events:{emitNotification:n=>lateEvents.push(n)}});
  assert.equal(lateEvents.length,1,'a matching manager registered later drains the pending invalidation');
  assert.equal(renderer.status().pending,0);
  console.log('PASS: renderer replay uses the actual query subscriber process');

  // The verified renderer case must exist exactly once in the installed app
  // so the plugins-prefix invalidation is really applied by the bundlers.
  const extracted=()=>{
    try{
      const name=fs.readdirSync('work').filter(entry=>/^app-initial-[0-9a-f]+\.js$/.test(entry)).sort().pop();
      return name?fs.readFileSync(path.join('work',name),'utf8'):null;
    }catch{return null;}
  };
  const installed=(()=>{
    try{
      const root='C:\\Program Files\\WindowsApps';
      const version=fs.readdirSync(root).filter(name=>name.startsWith('OpenAI.Codex_'))
        .sort((a,b)=>a.localeCompare(b,undefined,{numeric:true})).pop();
      if(!version)return extracted();
      const archive=path.join(root,version,'app','resources','app.asar');
      const stream=fs.openSync(archive,'r');
      try{
        const prefix=Buffer.alloc(16);fs.readSync(stream,prefix,0,16,0);
        const length=prefix.readUInt32LE(12),base=8+prefix.readUInt32LE(8);
        const header=Buffer.alloc(length);fs.readSync(stream,header,0,length,16);
        const tree=JSON.parse(header.toString('utf8'));
        const entries=[];
        const walk=(node,prefixName)=>{
          for(const [name,item] of Object.entries(node.files))if(item.files)walk(item,prefixName+name+'/');
          else if(item.offset!=null&&!item.unpacked)entries.push([prefixName+name,item]);};
        walk(tree,'');
        const entry=entries.find(([name])=>name.startsWith('webview/assets/app-initial')&&name.endsWith('.js'));
        if(!entry)return null;
        const data=Buffer.alloc(entry[1].size);
        fs.readSync(stream,data,0,entry[1].size,base+Number(entry[1].offset));
        return data.toString('utf8');
      }finally{fs.closeSync(stream);}
    }catch{return extracted();}
  })();
  if(installed===null){console.log('SKIP: installed Codex renderer not readable for the verified case check');}
  else{
    const matches=installed.match(/case`skills\/changed`:([A-Za-z_$][A-Za-z0-9_$]*)\.queryClient\.invalidateQueries\(\{queryKey:\[`skills`\]\}\);break;/g)||[];
    assert.equal(matches.length,1,'installed renderer must contain exactly one verified skills/changed case');
    console.log('PASS: installed renderer carries the verified plugin invalidation case');
  }
})().catch(error=>{console.error(error);process.exitCode=1});
