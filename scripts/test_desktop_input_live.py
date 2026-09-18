"""Three real desktop processes, empty homes, production host, physical input.

Temporarily shows a labelled fixture window. Sends no chat/model/network request.
Only foreground-checked fixture controls receive input. Prior foreground is restored.
"""
import json
import argparse
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch
from uuid import uuid4

from desktop_launch import find_app
from manager_core import desktop_bundle

ROOT = Path(__file__).resolve().parents[1]
DIAGNOSTIC = r'''
(() => {
 const {app}=require('electron'),fs=require('node:fs'),path=require('node:path');
 const root=process.env.CODEX_MANAGER_ROOT;let first;
 app.on('browser-window-created',(_,win)=>{
  if(first)return;first=win;
  fs.writeFileSync(path.join(root,'status.json'),JSON.stringify({hwnd:win.getNativeWindowHandle().readBigUInt64LE().toString(),hook:typeof win.hookWindowMessage,shape:typeof win.setShape,dipRect:typeof require('electron').screen?.screenToDipRect,bounds:win.getBounds?.()}));
  win.webContents.once('did-finish-load',()=>setTimeout(async()=>{
   try {
    await win.webContents.executeJavaScript(`(() => {
      const box=document.createElement('div');box.style='position:fixed;inset:0;background:#203244;color:white;z-index:2147483647;pointer-events:auto';
      const tag='${process.env.FIXTURE_EDITOR || 'input'}';
      const editor=tag==='contenteditable'?'div':tag;
      box.innerHTML='<div style="padding:16px">Disposable keyboard fixture — no chat will be sent</div><'+editor+' '+(tag==='contenteditable'?'contenteditable="true"':'')+' id="native-input-fixture" style="position:absolute;left:30px;top:60px;width:440px;height:160px;font-size:30px;color:black;background:white"></'+editor+'>';
      document.body.append(box);const input=box.querySelector('#native-input-fixture');
      window.__inputFixture={value:'',clicks:0,keys:0,focusEvents:0,compositions:[],keyEvents:[],releases:[]};
      input.addEventListener('pointerdown',()=>window.__inputFixture.clicks++);
      input.addEventListener('pointerup',e=>window.__inputFixture.releases.push([e.isTrusted,e.button,document.hasFocus(),document.activeElement===input]));
      input.addEventListener('keydown',e=>{window.__inputFixture.keys++;window.__inputFixture.keyEvents.push([e.key,e.code,e.keyCode,e.altKey,e.ctrlKey]);});
      input.addEventListener('focus',()=>window.__inputFixture.focusEvents++);
      input.addEventListener('input',()=>window.__inputFixture.value=input.isContentEditable?input.textContent.replace(/\u00a0/g,' '):input.value);
      for(const type of ['compositionstart','compositionupdate','compositionend'])input.addEventListener(type,e=>window.__inputFixture.compositions.push([type,e.data]));
    })()`);
    let busy=false;
    setInterval(async()=>{if(busy)return;busy=true;try{
      const state=await win.webContents.executeJavaScript(`JSON.stringify({...window.__inputFixture,focused:document.hasFocus(),active:document.activeElement?.id,embedded:window.__codexHostEmbedded,installed:window.__codexHostInputInstalled})`);
      const capture=path.join(root,'capture-preedit');
      if(fs.existsSync(capture)){fs.unlinkSync(capture);try{const image=await win.webContents.capturePage();fs.writeFileSync(path.join(root,'inline-preedit.png'),image.toPNG());}catch{}}
      const file=path.join(root,'input-state.json');fs.writeFileSync(file+'.tmp',state);fs.renameSync(file+'.tmp',file);
    }catch{}finally{busy=false}},150).unref();
   }catch(error){fs.writeFileSync(path.join(root,'fixture-error.txt'),String(error));}
  },700));
 });
 setTimeout(()=>app.exit(),120000).unref();
})();
'''.encode()


def run():
    parser=argparse.ArgumentParser(add_help=False)
    parser.add_argument('--desktop-source',type=Path)
    options, host_args=parser.parse_known_args()
    fixture_host = ROOT / 'tests/DesktopRenderHost/bin/Release/net10.0-windows10.0.19041.0/DesktopRenderHost.exe'
    if options.desktop_source:
        source=options.desktop_source.resolve()
        manifest=json.loads((source.parent/'original-sync.json').read_text(encoding='utf-8'))
        installed={'executable':str(source),'Version':manifest['source']['version']}
    else: installed = find_app()
    assets = Path(desktop_bundle.prepare(ROOT, installed)['executable']).parent
    root = ROOT / 'artifacts/results' / ('desktop-input-' + uuid4().hex[:8])
    root.mkdir(parents=True)
    children, logs, entries = [], [], []
    startup = subprocess.STARTUPINFO()
    startup.dwFlags = subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    print(root, flush=True)
    try:
        for i in range(3):
            evidence=root/str(i);program=evidence/'app';program.mkdir(parents=True)
            for directory, _, names in os.walk(assets):
                relative=Path(directory).relative_to(assets);target=program/relative;target.mkdir(exist_ok=True)
                for name in names:
                    if relative==Path('resources') and name=='app.asar':continue
                    os.link(desktop_bundle._long_path(Path(directory)/name),desktop_bundle._long_path(target/name))
            original_read=Path.read_bytes
            def read(path):
                data=original_read(path)
                return DIAGNOSTIC+b'\n'+data if path.name=='desktop_notification_activation.cjs' else data
            with patch.object(Path,'read_bytes',read):
                desktop_bundle.patch_archive(Path(installed['executable']).parent/'resources/app.asar',program/'resources/app.asar')
            env={k:v for k,v in os.environ.items() if k.upper() in {'SYSTEMROOT','WINDIR','PATH','TEMP','TMP','USERPROFILE','LOCALAPPDATA','APPDATA'}}
            env.update(CODEX_HOME=str(evidence/'home'),CODEX_ELECTRON_USER_DATA_PATH=str(evidence/'ui'),
                CODEX_MANAGER_ROOT=str(evidence),CODEX_MANAGER_DESKTOP_PIPE='codex-manager-'+str(uuid4()),
                FIXTURE_EDITOR=('input','textarea','contenteditable')[i])
            (evidence/'home').mkdir()
            log=(evidence/'desktop.log').open('w',encoding='utf-8');logs.append(log)
            child=subprocess.Popen([str(program/'ChatGPT.exe'),'--user-data-dir='+str(evidence/'ui'),'codex://threads/new?mode=codex'],
                env=env,startupinfo=startup,creationflags=subprocess.CREATE_NO_WINDOW,stdout=log,stderr=log)
            children.append(child);entries.append(dict(Run=str(evidence),Pid=child.pid))
        (root/'input-fixtures.json').write_text(json.dumps(entries),encoding='utf-8')
        host=subprocess.Popen([str(fixture_host),str(root),'--input',*host_args],startupinfo=startup,creationflags=subprocess.CREATE_NO_WINDOW)
        children.append(host);host.wait(timeout=110)
        report=json.loads((root/'input-report.json').read_text(encoding='utf-8'))
        print(json.dumps(report,ensure_ascii=False))
        return 0 if report['passed'] else 1
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate();child.wait(timeout=5)
        for log in logs:log.close()


if __name__=='__main__':
    sys.exit(run())
