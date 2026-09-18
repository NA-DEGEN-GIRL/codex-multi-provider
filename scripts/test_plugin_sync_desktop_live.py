"""Shared plugin revisions must refresh a running hidden desktop.

The fixture plays the manager side: it installs a synthetic local plugin in the
disposable desktop home through manager_core.plugin_sync, publishes the shared
record-signals revision, and the running desktop has to reload its own
app-server catalog (skills/list forceReload), invalidate the renderer queries
and keep the unsent draft, route and window alive without a restart, focus or
navigation. Only disposable homes and loopback fixture responses are used.
"""
import json
import os
import shutil
import time
import test_desktop_record_sync_live as fixture

PLUGIN='fixture-plugin'
SKILL='fixture-skill'
VERSION='1.0.0'
NATIVE_MARKETPLACE='openai-curated-remote'

RENDERER_PROBE = rb'''
(() => {
 globalThis.__fixtureRendererLoadId=globalThis.__fixtureRendererLoadId??(globalThis.crypto?.randomUUID?.()??String(Date.now())+Math.random());
 globalThis.__fixtureComposerText=function(){
  try{
   const node=document.querySelector('[data-codex-composer]');
   for(let current=node;current;current=current.parentElement){
    const key=Object.keys(current).find(name=>name.startsWith('__reactFiber'));
    if(!key)continue;
    for(let fiber=current[key];fiber;fiber=fiber.return){
     const controller=fiber.memoizedProps?.composerController;
     const view=controller?.view;
     if(!view)continue;
     if(typeof view.getPromptText==='function'){const text=view.getPromptText();if(typeof text==='string')return text;}
     if(typeof view.text==='string')return view.text;
    }
   }
  }catch{}
  return null;
 };
})();
'''

PROBE = rb'''
(() => {
 const fs=require('node:fs'),path=require('node:path'),{app}=require('electron');
 const root=process.env.FIXTURE_ROOT,signals=process.env.CODEX_RECORD_SIGNALS;
 const signalFile=path.join(signals,'plugins.json');
 const skill='fixture-skill';
 const phases=[{name:'installed',expect:true},{name:'disabled',expect:false},{name:'removed',expect:false}];
 const calls=[];
 let manager,view,phase=0,busy=false,lastRevision=null,statusBefore=null,rendererBefore=null;
 let phaseStart=0,waitTicks=0,callCursor=0;
 function save(name,value){fs.writeFileSync(path.join(root,name),JSON.stringify(value));}
 function readSignal(){
   try{const data=JSON.parse(fs.readFileSync(signalFile,'utf8'));
     return data?.version===1&&typeof data.revision==='string'?data:null;}
   catch{return null;}
 }
 function describe(params){
   if(params==null||typeof params!=='object')return null;
   const out={};
   for(const key of ['cwds','forceReload'])if(key in params)out[key]=params[key];
   return out;
 }
 function summarize(result){
   const data=Array.isArray(result?.data)?result.data:[];
   return {entries:data.length,cwds:data.map(entry=>entry?.cwd??null),
     errors:data.reduce((sum,entry)=>sum+(entry?.errors?.length||0),0),
     skills:data.flatMap(entry=>entry?.skills||[]).map(value=>({name:value?.name??null,
       path:value?.path??null,enabled:value?.enabled??null}))};
 }
 function hasSkill(summary){
   return summary.skills.some(value=>String(value.name||'').includes(skill)||
     String(value.path||'').replace(/\\/g,'/').includes(skill));
 }
 function status(){try{return globalThis.__codexPluginSync?.status?.()??null;}catch{return null;}}
 function delta(before,after,key){return ((after&&after[key])||0)-((before&&before[key])||0);}
 app.on('browser-window-created',(_,window)=>{if(!view&&!window.isDestroyed())view=window.webContents;});
 async function renderer(){
   if(!view||view.isDestroyed())return null;
   try{
     return await view.executeJavaScript(`(async()=>({loadId:globalThis.__fixtureRendererLoadId??null,
       patchedInvalidations:globalThis.__codexPluginInvalidations??0,
       rendererSync:globalThis.__codexPluginRendererSync?.status?.()??null,
       route:location.pathname+location.search+location.hash,
       draft:(document.body.innerText||'').includes('UNSENT_DRAFT_KEEP'),
       composer:globalThis.__fixtureComposerText?globalThis.__fixtureComposerText():null}))()`,true);
   }catch{return null;}
 }
 const sync=globalThis.__codexRecordSync;
 if(!sync||typeof sync.register!=='function'){
   fs.writeFileSync(path.join(root,'plugin-sync-error.txt'),'record sync adapter is missing');return;
 }
 const register=sync.register;
 sync.register=function(candidate){
   const result=Reflect.apply(register,this,arguments);
   if(candidate&&candidate.hostId==='local'&&!manager){
     manager=candidate;
     const client=candidate.requestClient,send=client?.sendRequest;
     if(typeof send==='function')client.sendRequest=async function(method,params){
       const record={method,params:describe(params),at:Date.now()};
       calls.push(record);
       try{const value=await Reflect.apply(send,this,arguments);record.result=summarize(value);return value;}
       catch(error){record.error=String(error?.message||error);throw error;}
     };
   }
   return result;
 };
 setInterval(async()=>{
  if(busy||!manager)return;busy=true;
  try{
   if(phase===0){
     if(!fs.existsSync(path.join(root,'plugins-armed')))return;
     const snapshot=await renderer();
     if(!snapshot||!snapshot.loadId||!snapshot.draft)return;
     const signal=readSignal();
     statusBefore=status();rendererBefore=snapshot;lastRevision=signal?.revision??null;
     save('plugin-sync-baseline.json',{revision:lastRevision,renderer:snapshot,status:statusBefore,
       calls:calls.length,adapter:!!globalThis.__codexPluginSync,
       rendererAdapter:!!snapshot.rendererSync});
     phase=1;phaseStart=0;waitTicks=0;return;
   }
   const target=phases[phase-1];
   if(!target)return;
   const signal=readSignal();
   if(!signal||signal.revision===lastRevision){
     if(waitTicks++>100)throw Error('Plugin revision did not change for '+target.name);
     return;
   }
   if(!phaseStart){phaseStart=Date.now();callCursor=calls.length;}
   const current=status(),snapshot=await renderer();
   const reloads=delta(statusBefore,current,'reloads'),invalidations=delta(statusBefore,current,'invalidations');
   const forced=calls.slice(callCursor).filter(call=>call.method==='skills/list'&&call.params&&call.params.forceReload===true);
   const answered=forced.find(call=>call.result);
   const rendererInvalidations=snapshot?.rendererSync?.invalidations??0;
   const ready=!!answered&&reloads>=1&&invalidations>=1&&!!snapshot&&
     rendererInvalidations>(rendererBefore.rendererSync?.invalidations??0);
   if(!ready){
     if(waitTicks++>100)throw Error('Plugin refresh did not reach the renderer for '+target.name+': '+
       JSON.stringify({forced,reloads,invalidations,snapshot,status:current}));
     return;
   }
   const present=hasSkill(answered.result);
   if(present!==target.expect)throw Error('Runtime catalog mismatch for '+target.name+': '+JSON.stringify(answered.result));
   const stable=snapshot.loadId===rendererBefore.loadId&&snapshot.route===rendererBefore.route&&
     snapshot.draft===true&&rendererBefore.draft===true;
   if(!stable)throw Error('Renderer changed during '+target.name+': '+JSON.stringify({before:rendererBefore,after:snapshot}));
   save('plugin-sync-'+target.name+'.json',{phase:target.name,revision:signal.revision,expectSkill:target.expect,
     skillPresent:present,forceReloadCalls:forced.map(call=>({params:call.params,result:call.result??null,error:call.error??null})),
     rendererInvalidationsDelta:rendererInvalidations-(rendererBefore.rendererSync?.invalidations??0),
     reloads,invalidations,statusBefore,statusAfter:current,
     rendererBefore,rendererAfter:snapshot,elapsedMs:Date.now()-phaseStart});
   lastRevision=signal.revision;statusBefore=current;rendererBefore=snapshot;phaseStart=0;waitTicks=0;phase++;
  }catch(error){fs.writeFileSync(path.join(root,'plugin-sync-error.txt'),String(error&&error.stack||error));}
  finally{busy=false;}
 },200).unref();
})();
'''


def wait_file(path,timeout=30):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        if path.exists():return json.loads(path.read_text(encoding='utf8'))
        error=path.parent/'plugin-sync-error.txt'
        if error.exists():raise RuntimeError(error.read_text(encoding='utf8'))
        time.sleep(.1)
    raise TimeoutError('Plugin fixture timed out: '+path.name)


def write_native_install(home):
    bundle=home/'plugins/cache'/NATIVE_MARKETPLACE/PLUGIN/VERSION
    (bundle/'.codex-plugin').mkdir(parents=True,exist_ok=True)
    (bundle/'skills'/SKILL).mkdir(parents=True,exist_ok=True)
    (bundle/'.codex-plugin/plugin.json').write_text(json.dumps(dict(name=PLUGIN,version=VERSION,
        description='Shared plugin live fixture',skills='./skills/')),encoding='utf8')
    (bundle/'skills'/SKILL/'SKILL.md').write_text('---\nname: '+SKILL+
        '\ndescription: Synthetic skill for the shared plugin live fixture.\n---\n\nFixture body.\n',encoding='utf8')


def publish(output,revision):
    target=output/'signals/plugins.json'
    target.parent.mkdir(parents=True,exist_ok=True)
    temporary=target.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(dict(version=1,revision=str(revision))),encoding='utf8')
    os.replace(temporary,target)


def module_revision(store):
    path=store.directory/'record-signals'/'plugins.json'
    if not path.is_file():return None
    try:
        value=json.loads(path.read_text(encoding='utf8'))
    except (OSError,ValueError):
        return None
    return str(value['revision']) if value.get('version')==1 and 'revision' in value else None


def next_revision(store,published,label):
    """Prefer the manager revision; a preserved edit needs its own bump."""
    current=module_revision(store)
    return current if current and current not in published else str(time.time_ns())+'-'+label


def disable_mirror(home,enabled):
    config=home/'config.toml'
    text=config.read_text(encoding='utf8')
    marker='[plugins."'+PLUGIN+'@codex-manager-shared"]'
    if marker not in text:
        raise RuntimeError('The manager did not declare the shared plugin in the desktop home.')
    head,_,body=text.partition(marker)
    body='\n'.join('enabled = '+('false' if not enabled else 'true') if line.strip().startswith('enabled =')
        else line for line in body.splitlines())
    config.write_text(head+marker+body,encoding='utf8')


def main():
    checks={}

    def drive(writer,tid,output):
        from manager_core.plugin_sync import PluginSync
        from manager_core.store import Store
        home=output/'original'          # the running desktop's CODEX_HOME
        owner=output/'plugin-owner'     # the home that installs the plugin
        store=Store(output/'plugin-store')
        profile=store.add_profile('fixture-desktop')
        store.mutate(lambda data:next(item for item in data['profiles']
            if item['id']==profile['id']).update(home=str(home)))
        write_native_install(owner)
        (output/'plugins-armed').touch()
        baseline=wait_file(output/'plugin-sync-baseline.json')
        status=baseline.get('status') or {}
        checks['plugin_sync_adapter_registered']=(baseline.get('adapter') is True
            and baseline.get('rendererAdapter') is True and status.get('managers',0)>=1)
        published=set()
        PluginSync(store,source=owner).reconcile(force=True)
        mirror=home/'plugins/cache/codex-manager-shared'/PLUGIN/VERSION
        config=(home/'config.toml').read_text(encoding='utf8')
        registry=json.loads((store.directory/'plugin-sync.json').read_text(encoding='utf8'))
        checks['plugin_sync_publishes_shared_mirror']=(mirror.is_dir()
            and '[marketplaces.codex-manager-shared]' in config
            and '[plugins."'+PLUGIN+'@codex-manager-shared"]' in config
            and set(registry.get('shared',{}))=={PLUGIN})
        revision=next_revision(store,published,'install');published.add(revision)
        publish(output,revision)
        installed=wait_file(output/'plugin-sync-installed.json')
        forced=[call for call in installed['forceReloadCalls'] if call.get('result')]
        checks['plugin_signal_reloads_runtime_catalog']=(installed['skillPresent'] is True
            and installed['reloads']==1 and installed['invalidations']==1
            and any(call['params'].get('forceReload') is True and not call['params'].get('cwds') for call in forced))
        checks['plugin_signal_invalidates_running_renderer']=(
            installed['rendererInvalidationsDelta']>=1
            and (installed['rendererAfter']['rendererSync'] or {}).get('invalidations',0)>=1)
        disable_mirror(home,False)
        PluginSync(store,source=owner).reconcile(force=True)
        config=(home/'config.toml').read_text(encoding='utf8')
        marker=config.partition('[plugins."'+PLUGIN+'@codex-manager-shared"]')[2]
        checks['plugin_disable_keeps_mirror_disabled']=any(line.strip()=='enabled = false'
            for line in marker.splitlines())
        revision=next_revision(store,published,'disable');published.add(revision)
        publish(output,revision)
        disabled=wait_file(output/'plugin-sync-disabled.json')
        checks['plugin_disable_hides_skill_without_restart']=(disabled['skillPresent'] is False
            and disabled['reloads']==1 and disabled['invalidations']==1
            and disabled['rendererInvalidationsDelta']>=1)
        shutil.rmtree(owner/'plugins/cache'/NATIVE_MARKETPLACE/PLUGIN)
        PluginSync(store,source=owner).reconcile(force=True)
        registry=json.loads((store.directory/'plugin-sync.json').read_text(encoding='utf8'))
        checks['plugin_removal_unpublishes_mirror']=(not mirror.exists()
            and PLUGIN in registry.get('removed',{})
            and not (store.directory/'shared-plugins/codex-manager-shared/plugins'/PLUGIN).exists())
        revision=next_revision(store,published,'remove');published.add(revision)
        publish(output,revision)
        removed=wait_file(output/'plugin-sync-removed.json')
        checks['plugin_removal_hides_skill_without_restart']=(removed['skillPresent'] is False
            and removed['reloads']==1 and removed['invalidations']==1)
        phases=[installed,disabled,removed]
        checks['plugin_refresh_preserves_draft_route_and_window']=all(
            phase['rendererBefore']['draft'] is True and phase['rendererAfter']['draft'] is True
            and phase['rendererBefore']['loadId']==phase['rendererAfter']['loadId']
            and phase['rendererBefore']['route']==phase['rendererAfter']['route'] for phase in phases)
        checks['plugin_refresh_reports_renderer_status']=all(
            (phase['rendererAfter']['rendererSync'] or {}).get('managers',0)>=1 for phase in phases)

    fixture.RENDERER_DIAGNOSTIC=RENDERER_PROBE+fixture.RENDERER_DIAGNOSTIC
    fixture.DIAGNOSTIC=PROBE+fixture.DIAGNOSTIC
    before=set((fixture.ROOT/'artifacts/results').glob('desktop-sync-*'))
    passed=fixture.run(before_writes=drive)
    output=(set((fixture.ROOT/'artifacts/results').glob('desktop-sync-*'))-before).pop()
    report=json.loads((output/'report.json').read_text(encoding='utf8'))
    expected={'plugin_sync_adapter_registered','plugin_signal_reloads_runtime_catalog',
        'plugin_signal_invalidates_running_renderer','plugin_refresh_preserves_draft_route_and_window',
        'plugin_refresh_reports_renderer_status','plugin_disable_keeps_mirror_disabled',
        'plugin_disable_hides_skill_without_restart','plugin_removal_hides_skill_without_restart',
        'plugin_sync_publishes_shared_mirror','plugin_removal_unpublishes_mirror'}
    report['checks'].update(checks)
    report['passed']=passed and expected<=set(checks) and all(checks.values())
    (output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    print(json.dumps({'passed':report['passed'],'plugin_checks':checks,'output':str(output)}))
    raise SystemExit(not report['passed'])


if __name__=='__main__':
    main()
