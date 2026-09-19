"""Real installed desktop + production WPF host, with an empty disposable home.

No login is copied, no model request is sent, and no existing window is selected.
Run after building tests/DesktopRenderHost in Release. Evidence stays in results.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch
from uuid import uuid4

from desktop_launch import find_app
from manager_core import desktop_bundle

ROOT = Path(__file__).resolve().parents[1]


def run():
    fixture_host = ROOT / 'tests/DesktopRenderHost/bin/Release/net10.0-windows10.0.19041.0/DesktopRenderHost.exe'
    if not fixture_host.is_file():
        raise RuntimeError('Build tests/DesktopRenderHost/DesktopRenderHost.csproj -c Release first.')
    installed = find_app()
    assets = Path(desktop_bundle.prepare(ROOT, installed)['executable']).parent
    evidence = ROOT / 'artifacts/results' / ('desktop-render-' + uuid4().hex[:8])
    program = evidence / 'app'
    program.mkdir(parents=True)
    for directory, _, names in os.walk(assets):
        relative = Path(directory).relative_to(assets)
        target = program / relative
        target.mkdir(exist_ok=True)
        for name in names:
            if relative == Path('resources') and name == 'app.asar':
                continue
            # Program files only. No hardlinked program file is ever modified.
            os.link(desktop_bundle._long_path(Path(directory) / name), desktop_bundle._long_path(target / name))
    diagnostic = r'''
(() => {
 if(typeof process==='undefined'||process.type==='renderer')return;
 const {app,dialog}=require('electron'),fs=require('node:fs'),path=require('node:path');
 const root=process.env.CODEX_MANAGER_ROOT;let first;
 dialog.showErrorBox=(title,content)=>{
  fs.writeFileSync(path.join(root,'bootstrap-error.json'),JSON.stringify({title,content}));
  app.exit(1);
 };
 app.on('browser-window-created',(_,win)=>{
  if(first)return;first=win;
  const context=globalThis.__codexManagerTaskContext;
  globalThis.__codexManagerTaskContext=(sender,route,title)=>{
   fs.appendFileSync(path.join(root,'routes.jsonl'),JSON.stringify({route,view:sender.id,at:Date.now()})+'\n');
   return context(sender,route,title);
  };
  fs.writeFileSync(path.join(root,'status.json'),JSON.stringify({hwnd:win.getNativeWindowHandle().readBigUInt64LE().toString()}));
  win.webContents.once('did-finish-load',()=>setTimeout(async()=>{
   try {
    const screenshot=await win.webContents.capturePage();
    fs.writeFileSync(path.join(root,'renderer.png'),screenshot.toPNG());
    fs.writeFileSync(path.join(root,'renderer.json'),JSON.stringify({ready:true,visible:win.isVisible()}));
   }catch(error){fs.writeFileSync(path.join(root,'renderer.json'),JSON.stringify({error:String(error)}));}
  },2000));
  win.webContents.once('did-finish-load', async()=>{
   await win.webContents.executeJavaScript(`window.__viewportFixtureFrames=0;
    (function frame(){window.__viewportFixtureFrames++;requestAnimationFrame(frame)})();0`);
   let pending=false;
   const timer=setInterval(async()=>{
    if(pending||win.isDestroyed())return;pending=true;
    try {
     const frames=await win.webContents.executeJavaScript('window.__viewportFixtureFrames',false);
     fs.writeFileSync(path.join(root,'fixture-body.txt'),await win.webContents.executeJavaScript('document.body.innerText',false));
     const viewport=await win.webContents.executeJavaScript('({width:innerWidth,height:innerHeight,dpr:devicePixelRatio})',false);
     fs.writeFileSync(path.join(root,'frames.json'),JSON.stringify({frames,viewport,
      bounds:win.getBounds(),contentBounds:win.getContentBounds?.(),
      backgroundThrottling:win.webContents.getBackgroundThrottling?.(),
      canInvalidate:typeof win.webContents.invalidate==='function',
      canSetThrottling:typeof win.webContents.setBackgroundThrottling==='function'}));
    }catch{}finally{pending=false}
   },200);timer.unref();
  });
 });
 setTimeout(()=>app.exit(),32000).unref();
})();
'''.encode()
    original_read = Path.read_bytes

    def read(path):
        data = original_read(path)
        return diagnostic + b'\n' + data if path.name == 'desktop_profile_resume.cjs' else data

    with patch.object(Path, 'read_bytes', read):
        desktop_bundle.patch_archive(Path(installed['executable']).parent / 'resources/app.asar', program / 'resources/app.asar')
    environment = {k: v for k, v in os.environ.items() if k.upper() in {
        'SYSTEMROOT', 'WINDIR', 'PATH', 'TEMP', 'TMP', 'USERPROFILE', 'LOCALAPPDATA', 'APPDATA'}}
    environment.update(CODEX_HOME=str(evidence / 'home'), CODEX_ELECTRON_USER_DATA_PATH=str(evidence / 'ui'),
        CODEX_MANAGER_ROOT=str(evidence), CODEX_MANAGER_DESKTOP_PIPE='codex-manager-' + str(uuid4()),
        CODEX_MANAGER_PROFILE_ID=str(uuid4()), CODEX_MANAGER_GENERATION=str(uuid4()))
    # Match production's explicit CLI selection. New desktop versions otherwise
    # require MSIX package identity before the main window can even be created.
    from manager_core.runtime_build import resolve
    environment['CODEX_CLI_PATH'] = resolve(ROOT)['runtime']
    (evidence / 'home').mkdir()
    provider_fixture = None
    if '--notification-navigation' in sys.argv:
        from test_shared_editing_headless import Client, Fixture
        provider_fixture = Fixture()
        binary = ROOT / 'artifacts/manager-runtime/releases/20260917-031430-463dd9/codex.exe'
        environment.update(CODEX_CLI_PATH=str(binary), LOCAL_FIXTURE_TOKEN='fixture-A', CODEX_RECORD_SHARED_APPEND='1')
        seed = Client(binary, evidence / 'home', None, 'A', provider_fixture.server.server_port, str(uuid4()), shared_append=True)
        try:
            thread = seed.rpc('thread/start', {'historyMode':'paginated','cwd':str(evidence)})['thread']['id']
            seed.turn(thread, 'NOTIFICATION_ROUTE_SEED')
            (evidence / 'notification-task.txt').write_text(thread)
        finally: seed.close()
        with (evidence / 'home' / 'config.toml').open('a',encoding='utf8') as config:
            config.write('\n[windows]\nsandbox="unelevated"\n')
        (evidence / 'home' / '.codex-global-state.json').write_text(json.dumps({'electron-persisted-atom-state':{
            'last_completed_onboarding':9999999999999,'electron:onboarding-projectless-completed':True,
            'electron:onboarding-welcome-pending':False}}),encoding='utf8')
    startup = subprocess.STARTUPINFO()
    startup.dwFlags = subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    children = []
    print(evidence, flush=True)
    try:
        with (evidence / 'desktop.log').open('w', encoding='utf-8') as log:
            desktop = subprocess.Popen([str(program / 'ChatGPT.exe'), '--user-data-dir=' + str(evidence / 'ui'),
                'codex://threads/new?mode=codex'], env=environment, startupinfo=startup,
                creationflags=subprocess.CREATE_NO_WINDOW, stdout=log, stderr=log)
            children.append(desktop)
            host_args = [str(fixture_host), str(evidence), str(desktop.pid)]
            if '--hidden-start' in sys.argv: host_args.append('--hidden-start')
            if '--shutdown' in sys.argv: host_args.append('--shutdown')
            if '--frame-progress' in sys.argv: host_args.append('--frame-progress')
            if '--modal-popups' in sys.argv: host_args.append('--modal-popups')
            if '--notification-navigation' in sys.argv: host_args.append('--notification-navigation')
            host = subprocess.Popen(host_args, startupinfo=startup,
                creationflags=subprocess.CREATE_NO_WINDOW)
            children.append(host)
            host.wait(timeout=31)
            desktop.wait(timeout=36)
        if '--shutdown' in sys.argv:
            report = json.loads((evidence / 'shutdown.json').read_text(encoding='utf-8'))
            assert report['passed'], report
            print(json.dumps({'passed': True, 'shutdown': report, 'evidence': str(evidence)}, ensure_ascii=False))
            return
        report = json.loads((evidence / 'wpf-host.json').read_text(encoding='utf-8'))
        renderer = json.loads((evidence / 'renderer.json').read_text(encoding='utf-8'))
        assert report['passed'] and renderer.get('ready') and renderer.get('visible'), (report, renderer)
        print(json.dumps({'passed': True, 'checks': report['checks'], 'evidence': str(evidence)}, ensure_ascii=False))
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()  # Only handles started in this empty fixture.
                child.wait(timeout=5)
        if provider_fixture is not None: provider_fixture.close()


if __name__ == '__main__':
    run()
