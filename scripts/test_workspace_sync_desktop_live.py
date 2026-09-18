"""Exercise the shared project adapter in the real hidden desktop fixture."""
import json
from pathlib import Path
import test_desktop_record_sync_live as fixture

PROBE = rb'''
(() => {
 const fs=require('node:fs'),path=require('node:path');
 const root=process.env.FIXTURE_ROOT,dir=path.join(process.env.CODEX_RECORD_SIGNALS,'workspaces');
 fs.mkdirSync(dir,{recursive:true});
 const writer='99999999-9999-4999-8999-999999999999',id='88888888-8888-4888-8888-888888888888';
 const file=path.join(dir,writer+'.json');
 const project={id,hostId:'remote-ssh-discovered:fixture',remotePath:'/fixture/example-game',label:'Fixture SSH project'};
 const publish=(seq,value)=>fs.writeFileSync(file,JSON.stringify({version:1,projects:[[id,seq,writer,value]]}));
 publish(1,project);
 const sync=globalThis.__codexWorkspaceSync,register=sync.register;
 let state,phase=0;
 sync.register=function(s,w){register(s,w);state=s;};
 setInterval(()=>{
   if(!state)return;
   const projects=state.getStored('remote-projects')||[],present=projects.some(p=>p.id===id);
   if(phase===0&&present){phase=1;publish(2,null);}
   else if(phase===1&&!present){phase=2;fs.writeFileSync(path.join(root,'workspace-check.json'),JSON.stringify({native_state_added:true,native_state_removed:true}));}
 },300).unref();
})();
'''

LOCAL_PROBE = rb'''
(() => {
 const fs=require('node:fs'),path=require('node:path'),{app}=require('electron');
 const root=process.env.FIXTURE_ROOT,dir=path.join(process.env.CODEX_RECORD_SIGNALS,'local-workspaces');
 const writer='77777777-7777-4777-8777-777777777777',id='66666666-6666-4666-8666-666666666666';
 const sync=globalThis.__codexLocalWorkspaceSync,register=sync.register,registerBackend=sync.registerBackend;
 let state,backend,view,phase=0,busy=false,project,checks={};
 app.on('browser-window-created',(_,w)=>{view??=w.webContents;});
 sync.register=function(s,w){register(s,w);state=s;};
 sync.registerBackend=function(b){registerBackend(b);if(b.cache.hostId==='local')backend=b;};
 setInterval(async()=>{
  if(busy||!state||!backend?.projectsReady)return;busy=true;
  try{
   if(phase===0){
    const result=await backend.connection.sendAppServerRequest('project/create',{name:'SHARED_API_PROJECT',roots:[{path:root}],idempotencyKey:id});
    project={id,name:'SHARED_API_PROJECT',rootPaths:[root],createdAt:Date.now(),updatedAt:Date.now(),serverId:result.project.id};
    fs.mkdirSync(dir,{recursive:true});fs.writeFileSync(path.join(dir,writer+'.json'),JSON.stringify({version:1,projects:[[id,1,writer,project]]}));phase=1;
   }else if(phase===1&&state.getStored('local-projects')?.[id]&&backend.serverProjectsByLegacyId.has(id)){
    const visible=await view.executeJavaScript('document.body.innerText');
    if(!visible.includes('SHARED_API_PROJECT'))return;
    checks.peer_project_visible_in_sidebar=true;
    await backend.updateProject(id,{name:'RENAMED_API_PROJECT'});checks.peer_project_native_edit=true;phase=2;
   }else if(phase===2&&state.getStored('local-projects')?.[id]?.name==='RENAMED_API_PROJECT'){
    await backend.writeProject('project/delete',state.getStored('local-projects')[id]);phase=3;
   }else if(phase===3&&!state.getStored('local-projects')?.[id]){
    await sync.tick();
    const rows=fs.readdirSync(dir).flatMap(n=>n.endsWith('.json')?JSON.parse(fs.readFileSync(path.join(dir,n),'utf8')).projects:[]);
    if(!rows.some(r=>r[0]===id&&r[3]===null))return;
    checks.native_removal_publishes_tombstone=true;phase=4;
    fs.writeFileSync(path.join(root,'local-workspace-check.json'),JSON.stringify(checks));
   }
  }catch(e){fs.writeFileSync(path.join(root,'local-workspace-error.txt'),String(e));}
  finally{busy=false;}
 },300).unref();
})();
'''

if __name__=='__main__':
    before=set((fixture.ROOT/'artifacts/results').glob('desktop-sync-*'))
    fixture.DIAGNOSTIC=PROBE+LOCAL_PROBE+fixture.DIAGNOSTIC
    passed=fixture.run()
    outputs=set((fixture.ROOT/'artifacts/results').glob('desktop-sync-*'))-before
    if len(outputs)!=1:raise SystemExit(1)
    output=outputs.pop();check=output/'workspace-check.json'
    report=json.loads((output/'report.json').read_text())
    checks=json.loads(check.read_text()) if check.exists() else {'native_workspace_probe':False}
    local=output/'local-workspace-check.json'
    checks.update(json.loads(local.read_text()) if local.exists() else {'native_local_workspace_probe':False})
    report['checks'].update(checks);report['passed']=passed and all(checks.values())
    (output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({'passed':report['passed'],'workspace_checks':checks,'output':str(output)}))
    raise SystemExit(not report['passed'])
