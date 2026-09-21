"""Real desktop history store + second native writer, disposable homes only.

The desktop stays hidden. Responses use a loopback fixture; no user data,
credentials, navigation or external model requests are involved.
"""
import json
import os
from pathlib import Path
import subprocess
import time
import threading
from unittest.mock import patch
from uuid import uuid4

from desktop_launch import find_app
from manager_core import original_sync_bundle, desktop_bundle
from manager_core.record_signals import RecordSignals
from test_shared_editing_headless import Client, Fixture, ROOT

RENDERER_DIAGNOSTIC = rb'''
(() => {
 globalThis.fixtureHistoryRequests={fullReads:0,turnPages:0,itemPages:0};
 const sync=globalThis.__codexRendererRecordSync,register=sync.register;
 sync.register=function(m){
   register(m);
   const client=m.requestClient,send=client.sendRequest;
   client.sendRequest=function(method,params,...rest){
     const counts=globalThis.fixtureHistoryRequests;
     if(method==='thread/read'&&params?.includeTurns===true)counts.fullReads++;
     if(method==='thread/turns/list')counts.turnPages++;
     if(method==='thread/items/list')counts.itemPages++;
     return Reflect.apply(send,this,[method,params,...rest]);
   };
 };
 const route=globalThis.__codexProfileResume;
 globalThis.__codexProfileResume=async function(...args){
   const result=await route(...args);
   if(args[3]==='thread/resume')globalThis.fixtureVisibleResume={provider:result.modelProvider,model:result.model};
   return result;
 };
})();
'''
DIAGNOSTIC = r'''
(() => {
 const fs=require('node:fs'),path=require('node:path'),{app}=require('electron');
 const root=process.env.FIXTURE_ROOT, id=process.env.FIXTURE_THREAD;
 let view;
 app.on('browser-window-created',(_,win)=>{
   if(view){win.show=()=>{};win.showInactive=()=>{};win.focus=()=>{};return;}
   view=win.webContents;
   view.setBackgroundThrottling(false);
   const showInactive=win.showInactive.bind(win),setBounds=win.setBounds.bind(win);
   const show=()=>{setBounds({x:-28000,y:-28000,width:1100,height:850});showInactive();};
   win.show=show;win.showInactive=show;win.focus=()=>{};
   win.setBounds=()=>{};win.center=()=>{};win.setPosition=()=>{};win.maximize=()=>{};

   view.once('did-finish-load',()=>setTimeout(()=>view.send('codex_desktop:message-for-view',
     {type:'navigate-to-route',path:'/local/'+id}),6500));
 });
 const sync=globalThis.__codexRecordSync, register=sync.register;let manager,busy=false,loaded=false,lastError;
 sync.register=function(m){register(m);if(m.hostId==='local')manager=m;};
 setInterval(async()=>{
   if(busy||!manager?.threadStore)return;busy=true;
   try {
     const store=manager.threadStore;
     if(!loaded){
       await store.hydrateThreads([id],{addToRecentConversations:true,includeTurns:false,retainHistoryPagination:true,notifyAnyCallbacks:true});
       loaded=true;
     }
     if(fs.existsSync(path.join(root,'resume-again')) && !globalThis.fixtureResumed){
       globalThis.fixtureResumed=await manager.requestClient.sendRequest('thread/resume',{threadId:id,excludeTurns:true});
     }
     if(fs.existsSync(path.join(root,'insert-draft'))&&!globalThis.fixtureDraft){
       globalThis.fixtureDraft=await view.executeJavaScript(`(()=>{const e=document.querySelector('[data-codex-composer]');for(let n=e;n;n=n.parentElement){let f=n[Object.keys(n).find(k=>k.startsWith('__reactFiber'))];for(;f;f=f.return){const c=f.memoizedProps?.composerController;if(c?.view){c.setPromptText('UNSENT_DRAFT_KEEP');return true}}}return false})()`);

     }
     if(fs.existsSync(path.join(root,'clear-draft'))&&!globalThis.fixtureDraftCleared){
       globalThis.fixtureDraftCleared=await view.executeJavaScript(`(()=>{const e=document.querySelector('[data-codex-composer]');for(let n=e;n;n=n.parentElement){let f=n[Object.keys(n).find(k=>k.startsWith('__reactFiber'))];for(;f;f=f.return){const c=f.memoizedProps?.composerController;if(c?.view){c.setPromptText('');return true}}}return false})()`);
     }
     const body=JSON.stringify(manager.getConversation(id));
     const rendered=view&&!view.isDestroyed()?await view.executeJavaScript('document.body.innerText'):'';
     fs.writeFileSync(path.join(root,'rendered.txt'),rendered);

     fs.writeFileSync(path.join(root,'snapshot.tmp'),JSON.stringify({loaded,
       seed:body?.includes('SYNC_SEED'), second:body?.includes('SYNC_SECOND'),
       third:body?.includes('SYNC_THIRD'), draft:rendered.includes('UNSENT_DRAFT_KEEP'), uiSeed:rendered.includes('SYNC_SEED'), uiSecond:rendered.includes('SYNC_SECOND'), uiThird:rendered.includes('SYNC_THIRD'), resumedProvider:globalThis.fixtureResumed?.modelProvider, visibleResume:await view.executeJavaScript('globalThis.fixtureVisibleResume'), historyRequests:await view.executeJavaScript('globalThis.fixtureHistoryRequests'), rendererStatus:await view.executeJavaScript('globalThis.__codexRendererRecordSync?.status()'), status:sync.status(),error:lastError}));
     fs.renameSync(path.join(root,'snapshot.tmp'),path.join(root,'snapshot.json'));
   }catch(e){lastError=String(e);fs.writeFileSync(path.join(root,'fixture-error.txt'),lastError);}
   finally{busy=false;}
 },300).unref();
 setTimeout(()=>app.exit(),90000).unref();
})();
'''.encode()


def run(binary=None, before_writes=None):
    if binary is None:
        from manager_core.runtime_build import resolve
        binary=Path(resolve(ROOT)['runtime'])
    output=ROOT/'artifacts/results'/('desktop-sync-'+uuid4().hex[:8]);output.mkdir(parents=True)
    release_response=threading.Event()
    def before_response(body):
        if 'SYNC_THIRD' in json.dumps(body):release_response.wait(35)
    fixture=Fixture(before_response);clients=[];desktop=None;signals=None;checks={};report={'passed':False,'real_model_calls':0}
    print(output,flush=True)
    def wait_for(predicate):
        end=time.monotonic()+30
        while time.monotonic()<end:
            if desktop.poll() is not None:raise RuntimeError('Fixture desktop exited')
            try:
                snapshot=json.loads((output/'snapshot.json').read_text())
                if predicate(snapshot):return snapshot
            except (OSError,ValueError):pass
            time.sleep(.2)
        raise TimeoutError('Desktop did not refresh: '+(output/'fixture-error.txt').read_text() if (output/'fixture-error.txt').exists() else 'Desktop did not refresh')
    try:
        home=output/'original';writer_home=output/'writer';port=fixture.server.server_port
        # Create the task in the API profile first. Continuing a GPT-created
        # task through an API writer leaves its original provider metadata intact
        # and does not reproduce the missing-provider bug in a new account.
        seed_config=('model="api-test-model"\nmodel_provider="external_fixture"\n'
            'cli_auth_credentials_store="ephemeral"\napproval_policy="never"\n'
            '[features]\nshell_tool=false\n[model_providers.external_fixture]\n'
            'name="Loopback"\nbase_url="http://127.0.0.1:'+str(port)+'/v1"\n'
            'env_key="LOCAL_FIXTURE_TOKEN"\nwire_api="responses"\n')
        seed=Client(binary,home,None,'A',port,str(uuid4()),shared_append=True,config_text=seed_config);clients.append(seed)
        (home/'.codex-global-state.json').write_text(json.dumps({'electron-persisted-atom-state':{
            'last_completed_onboarding':9999999999999,'electron:onboarding-projectless-completed':True,
            'electron:onboarding-welcome-pending':False}}),encoding='utf8')
        tid=seed.rpc('thread/start',{'historyMode':'paginated','cwd':str(output)})['thread']['id']
        seed.turn(tid,'SYNC_SEED');seed.close();clients.clear()
        (home/'config.toml').write_text(seed_config.replace('external_fixture','fixture').replace('api-test-model','gpt-5.5'),encoding='utf8')
        with (home/'config.toml').open('a',encoding='utf8') as config: config.write('\n[windows]\nsandbox="unelevated"\n')
        writer_config=(home/'config.toml').read_text().replace('model_provider="fixture"','model_provider="external_fixture"').replace('[model_providers.fixture]','[model_providers.external_fixture]').replace('gpt-5.5','api-test-model')
        writer=Client(binary,writer_home,None,'B',port,str(uuid4()),canonical_home=home,config_text=writer_config);clients.append(writer)
        writer.rpc('thread/resume',{'threadId':tid,'excludeTurns':True,'modelProvider':'external_fixture','model':'api-test-model'})
        writer.turn(tid,'EXTERNAL_SEED')  # Persist the foreign provider before the visible desktop opens it.
        signals=RecordSignals(output/'signals',str(uuid4()))
        app=find_app();source=Path(app['executable']).parent
        assets=original_sync_bundle.prepare(ROOT,app).parent
        # Independent ASAR, immutable hardlinks for other program assets.
        program=output/'app';program.mkdir()
        for directory,_,names in os.walk(assets):
            relative=Path(directory).relative_to(assets);target=program/relative;target.mkdir(exist_ok=True)
            for name in names:
                if relative==Path('resources') and name=='app.asar':continue
                os.link(desktop_bundle._long_path(Path(directory)/name),desktop_bundle._long_path(target/name))
        original_read=Path.read_bytes
        def read(path):
            data=original_read(path)
            if path.name=='desktop_renderer_record_sync.cjs':return data+b'\n'+RENDERER_DIAGNOSTIC
            return data+b'\n'+DIAGNOSTIC if path.name=='desktop_record_sync.cjs' else data
        with patch.object(Path,'read_bytes',read):
            original_sync_bundle.patch_archive(source/'resources/app.asar',program/'resources/app.asar')
        env={k:v for k,v in os.environ.items() if k.upper() in {'SYSTEMROOT','WINDIR','PATH','TEMP','TMP','USERPROFILE','LOCALAPPDATA','APPDATA'}}
        env.update(CODEX_HOME=str(home),CODEX_CLI_PATH=str(binary),CODEX_RECORD_SHARED_APPEND='1',
            CODEX_RECORD_SIGNALS=str(output/'signals'),CODEX_ELECTRON_USER_DATA_PATH=str(output/'ui'),
            FIXTURE_ROOT=str(output),FIXTURE_THREAD=tid,LOCAL_FIXTURE_TOKEN='fixture-A')
        startup=subprocess.STARTUPINFO();startup.dwFlags=subprocess.STARTF_USESHOWWINDOW;startup.wShowWindow=subprocess.SW_HIDE
        with (output/'desktop.log').open('w',encoding='utf8') as log:
            desktop=subprocess.Popen([str(program/'ChatGPT.exe'),'--user-data-dir='+str(output/'ui')],
                env=env,startupinfo=startup,creationflags=subprocess.CREATE_NO_WINDOW,stdout=log,stderr=log)
            initial=wait_for(lambda s:s['loaded'] and s['uiSeed'] and s.get('visibleResume',{}).get('provider')=='fixture');checks['native_ui_loaded_seed']=True
            checks['visible_renderer_resumes_foreign_provider']=initial['visibleResume']['model']=='gpt-5.5'
            (output/'insert-draft').touch()
            wait_for(lambda s:s['draft'])
            original_next=writer.next
            def next_event(deadline):
                event=original_next(deadline)
                if 'method' in event:signals.observe(event)
                return event
            writer.next=next_event
            if before_writes:
                before_writes(writer,tid,output)
            for text,key in [('SYNC_SECOND','second'),('SYNC_THIRD','third')]:
                offset=len(writer.events)
                if key=='third':
                    errors=[]
                    def write_turn():
                        try:writer.turn(tid,text)
                        except Exception as error:errors.append(str(error))
                    worker=threading.Thread(target=write_turn,daemon=True);worker.start()
                    wait_for(lambda s:s['uiThird'])
                    checks['user_message_visible_before_writer_response_completes']=worker.is_alive()
                    release_response.set();worker.join(30)
                    if errors:raise RuntimeError(errors[0])
                    if worker.is_alive():raise TimeoutError('Fixture response did not finish')
                else:writer.turn(tid,text)
                signals.observe({'method':'thread/started','params':{'thread':{'id':'00000000-0000-4000-8000-000000000099'}}})
                for event in writer.events[offset:]:signals.observe(event)
                if key=='second':
                    snap=wait_for(lambda s:s['rendererStatus']['pending']>0 and s['rendererStatus']['draftDeferred']>0)
                    checks['unsent_draft_preserved']=snap['draft']
                    checks['merge_deferred_while_composing']=not snap['uiSecond']
                    (output/'clear-draft').touch()
                snap=wait_for(lambda s:s['ui'+key.title()]);checks[key+'_in_renderer_without_reopen']=True
            (output/'resume-again').touch()
            snap=wait_for(lambda s:s.get('resumedProvider')=='fixture')
            checks['native_app_resumes_api_history_with_own_provider']=True
            checks['desktop_alive']=desktop.poll() is None
            checks['main_catalog_refreshed']=snap['status']['summaryRefreshes']>=2
            checks['paginated_history_avoids_full_reads']=snap['historyRequests']['fullReads']==0
            checks['native_history_pages_loaded']=snap['historyRequests']['turnPages']>0 and snap['historyRequests']['itemPages']>0
            checks['no_sync_failures']=snap['status']['failures']==0
            checks['renderer_refreshed']=snap['rendererStatus']['refreshes']>=2
            checks['no_renderer_sync_failures']=snap['rendererStatus']['failures']==0
            writer.rpc('thread/archive',{'threadId':tid})
            writer.rpc('thread/list',{'limit':1})  # Drain the native notification following the mutation response.
            wait_for(lambda s:s['rendererStatus'].get('archived')==1)
            checks['api_archive_reaches_visible_renderer']=True
            writer.rpc('thread/unarchive',{'threadId':tid})
            writer.rpc('thread/list',{'limit':1})
            wait_for(lambda s:s['rendererStatus'].get('archived')==0)
            checks['api_restore_reaches_visible_renderer']=True
            report.update(passed=all(checks.values()),checks=checks,status=snap['status'],renderer=snap['rendererStatus'])
    except Exception as error:report.update(error=str(error),checks=checks)
    finally:
        release_response.set()
        if desktop and desktop.poll() is None:desktop.terminate();desktop.wait(timeout=5)
        if signals:signals.close()
        for client in clients:client.close()
        fixture.close()
        (output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf8')
        print(json.dumps(report),flush=True)
    return report['passed']


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--runtime',type=Path)
    args=parser.parse_args();raise SystemExit(0 if run(args.runtime) else 1)
