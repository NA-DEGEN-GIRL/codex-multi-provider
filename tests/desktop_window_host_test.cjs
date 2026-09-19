const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../scripts/manager_core/desktop_window_host.cjs'), 'utf8');
let handler, marker, timer, closed, ready, loaded, exists=true, calls=[], visible=false, report;
let clock=2000, quits=0, watch, reads=0;
const process = {platform:'win32',pid:42,env:{CODEX_MANAGER_ROOT:'fixture'},kill(pid,signal){assert.equal(signal,0); if(!exists) throw Error('closed')}};
const Menu={setApplicationMenu(menu){calls.push(['applicationMenu',menu])}};
vm.runInNewContext(source, {process,Date:{now:()=>clock},setInterval(fn){timer=fn;return {unref(){}}},require(name){
  if(name==='electron') return {Menu,app:{quit(){quits++},on(event,fn){assert.equal(event,'browser-window-created');handler=fn}}};
  if(name==='node:fs') return {mkdirSync(){},watch(_,options,fn){watch=fn},statSync(){reads++;if(!marker)throw Error('missing');return {size:100}},readFileSync(){return JSON.stringify(marker)},writeFileSync(file,data){report=JSON.parse(data)},unlinkSync(file){if(!file.endsWith('.render.json'))marker=null}};
  return require(name);
}});
const win={once(event,fn){if(event==='closed')closed=fn;else if(event==='ready-to-show')ready=fn;else assert.fail(event)},
  webContents:{once(event,fn){assert.equal(event,'did-finish-load');loaded=fn},executeJavaScript(){return Promise.resolve()}},
  isVisible(){return visible},isDestroyed(){return false},
  getNativeWindowHandle(){const b=Buffer.alloc(8);b.writeBigUInt64LE(123n);return b}};
const setters=['setBounds','setTitleBarOverlay','setBackgroundMaterial','setMenu','setMenuBarVisibility','setAutoHideMenuBar','setMinimumSize','setMaximumSize','show','showInactive','setOpacity','setBackgroundColor','setTitle','focus','blur'];
for(const name of setters) win[name]=(...args)=>{calls.push([name,...args]);if(name==='showInactive')visible=true};
handler(null,win);
win.setBounds({x:1}); assert.equal(calls.length,1); // Detached window works normally.
marker={version:1,appPid:42,hwnd:'123',shellPid:99};
for(const name of setters) win[name]({x:9});
assert.equal(calls.length,1); // No frame reset, screen-coordinate move or activation while embedded.
marker.hwnd='124'; win.showInactive(); assert.equal(calls.length,2); // Other HWND is untouched.
marker.hwnd='123'; exists=false;win.show();assert.equal(calls.length,3); // Closed shell cannot lock out standalone mode.
exists=true;marker=null;win.setBounds({x:2});assert.equal(calls.length,4); // Explicit detach restores behavior.
win.setOpacity(0);assert.deepEqual(calls.at(-1),['setOpacity',1]); // Startup fade cannot leave a blank embedded surface.
marker={version:1,appPid:42,hwnd:'123',shellPid:99};calls=[];
Menu.setApplicationMenu('first');Menu.setApplicationMenu('latest');timer();
assert.deepEqual(calls,[]); // Global menu bypasses window setters in Owl.
marker=null;timer();assert.deepEqual(calls,[['applicationMenu','latest']]);
timer();assert.equal(calls.length,1); // Deferred menu is delivered once.
marker={version:1,appPid:42,hwnd:'123',shellPid:99};
Menu.setApplicationMenu('after-close');closed();timer();
assert.deepEqual(calls.at(-1),['applicationMenu','after-close']);

// A Win32-visible child can still be invisible to Owl. Renderer readiness must
// reach the real showInactive, without a foreground activation or repeated show.
for(const name of setters) win[name]=(...args)=>{calls.push([name,...args]);if(name==='showInactive')visible=true};
handler(null,win);calls=[];visible=false;
marker={version:1,appPid:42,hwnd:'123',shellPid:99,token:'first'};
timer();assert.deepEqual(calls,[]); // Do not show a renderer that is not ready.
ready();
assert.deepEqual(calls,[['setOpacity',1],['showInactive']]);
assert.equal(report.shown,true);assert.equal(report.token,'first');
loaded();timer();win.show();win.showInactive();
assert.equal(calls.length,2); // Reload/show storms cannot loop presentation.
marker=null;win.show();assert.equal(calls.at(-1)[0],'show'); // Detached behavior.
calls=[];visible=false;marker={version:1,appPid:42,hwnd:'124',shellPid:99,token:'wrong'};
timer();assert.deepEqual(calls,[]);
marker.hwnd='123';exists=false;timer();assert.deepEqual(calls,[]);
exists=true;marker.token='reattached';timer();
assert.deepEqual(calls,[['setOpacity',1],['showInactive']]);
calls=[];visible=false;closed();timer();assert.deepEqual(calls,[]);
console.log('PASS: geometry/menu guards, renderer presentation, no activation/repeat, late attach and detach');

// Independent viewport lifecycle: native bridge geometry, compositor wake-up,
// one show per selection, and explicit release rather than a late stray window.
for(const name of setters)win[name]=(...args)=>{calls.push([name,...args]);if(name==='showInactive')visible=true};
let actualBounds={x:0,y:0,width:400,height:300};
win.getBounds=()=>actualBounds;win.setBounds=(bounds)=>{actualBounds={...bounds};calls.push(['setBounds',bounds]);};
win.hide=()=>{calls.push(['hide']);visible=false};win.getMinimumSize=()=>[400,300];
handler(null,win);calls=[];visible=false;
marker={version:1,appPid:42,hwnd:'123',shellPid:99,token:'viewport',mode:'viewport',visible:false,
  bounds:{x:150,y:300,width:900,height:600,dpi:144}};
ready();
assert.equal(calls.filter(c=>c[0]==='showInactive').length,0);
assert.deepEqual(JSON.parse(JSON.stringify(calls.find(c=>c[0]==='setBounds')[1])),{x:100,y:200,width:600,height:400});
marker.visible=true;timer();
assert.equal(calls.filter(c=>c[0]==='showInactive').length,1);
win.hide();win.focus();win.show();timer();
assert.equal(calls.filter(c=>c[0]==='hide').length,1);
assert.equal(calls.filter(c=>c[0]==='focus').length,0);
assert.equal(calls.filter(c=>c[0]==='showInactive').length,1);
marker.visible=false;visible=false;timer();marker.visible=true;timer();
assert.equal(calls.filter(c=>c[0]==='showInactive').length,2);
assert.equal(calls.filter(c=>c[0]==='setBounds').length,5,'initial geometry plus one size transition per presentation');
for(let episode=0;episode<5;episode++){
 actualBounds={x:0,y:0,width:200,height:200};clock+=1100;timer();
 assert.equal(actualBounds.width,600);assert.equal(actualBounds.x,100);
 timer(); // Stable geometry resets the per-episode recovery budget.
}
assert.equal(calls.filter(c=>c[0]==='showInactive').length,2,'geometry repair must not activate/re-show on every event');
// Coalesced minimize/restore: shell marker goes false->true before JS polls.
marker.presentationEpoch=1;timer();
const beforeShows=calls.filter(c=>c[0]==='showInactive').length;
const beforeHides=calls.filter(c=>c[0]==='hide').length;
const beforeBounds=calls.filter(c=>c[0]==='setBounds').length;
marker.presentationEpoch=2;timer();timer();
assert.equal(report.presentationEpoch,2,'ack identifies the current selection, not an old successful presentation');
assert.equal(calls.filter(c=>c[0]==='hide').length,beforeHides+1);
assert.equal(calls.filter(c=>c[0]==='showInactive').length,beforeShows+1);
assert.equal(calls.filter(c=>c[0]==='focus').length,0);
assert.equal(calls.filter(c=>c[0]==='setBounds').length,beforeBounds+2,'one real native size transition per restore, even without invalidate');
assert.equal(actualBounds.width,600,'final bounds remain exact');
const beforeReads=reads;
for(let i=0;i<100;i++){
 watch('change','43.json');watch('rename','42.health.json');watch('change','42.json.render.json');
}
assert.equal(reads,beforeReads,'other profiles and diagnostics cannot wake the native layout loop');
watch('rename','42.json');assert(reads>beforeReads,'the actual lease change wakes presentation immediately');
marker.mode='released';marker.visible=false;timer();
assert.equal(visible,false);assert.equal(marker,null);
assert.deepEqual(calls.at(-2),['setMinimumSize',400,300]);assert.deepEqual(calls.at(-1),['hide']);
win.focus();assert.deepEqual(calls.at(-1),['focus']);
closed();
console.log('PASS: viewport geometry, repeated selection, single visibility authority, release and restored native controls');

handler(null,win); calls=[];
marker={version:1,appPid:42,hwnd:'124',shellPid:99,token:'quit',mode:'shutdown'};
timer();assert.equal(quits,0,'different window must not quit the app');
marker.hwnd='123';exists=false;timer();assert.equal(quits,0,'dead owner cannot issue a quit');
exists=true;timer();timer();assert.equal(quits,1,'normal application quit exactly once, before renderer ready');
assert.deepEqual(calls,[],'shutdown must not present or recover a hidden window');
closed();
console.log('PASS: scoped application shutdown before renderer readiness, no duplicate quit');

// Preloaded windows have no native lease until the user selects their profile.
// These fixtures execute startup callbacks without creating any native window.
function preloadFixture() {
  const fixture = {marker:null, ownerAlive:true, calls:[], report:null};
  vm.runInNewContext(source, {
    process:{platform:'win32', pid:42,
      env:{CODEX_MANAGER_ROOT:'fixture', CODEX_MANAGER_PRELOAD_HIDDEN:'1'},
      kill(pid,signal){assert.equal(signal,0);if(!fixture.ownerAlive)throw Object.assign(Error('gone'),{code:'ESRCH'})}},
    setInterval(fn){fixture.tick=fn;return {unref(){}}},
    require(name){
      if(name==='electron') return {app:{on(_,fn){fixture.created=fn},quit(){fixture.quit=true}}};
      if(name==='node:fs') return {
        mkdirSync(){},watch(){},
        statSync(){if(!fixture.marker)throw Error('missing');return {size:100}},
        readFileSync(){return JSON.stringify(fixture.marker)},
        writeFileSync(_,data){fixture.report=JSON.parse(data)},
        unlinkSync(file){if(!file.endsWith('.render.json'))fixture.marker=null},
      };
      return require(name);
    },
  });
  fixture.window = hwnd => {
    const events={}, viewEvents={};
    const window={visible:false, once(event,fn){events[event]=fn},
      webContents:{once(event,fn){viewEvents[event]=fn}},
      isVisible(){return this.visible},isDestroyed(){return false},
      getNativeWindowHandle(){const b=Buffer.alloc(8);b.writeBigUInt64LE(BigInt(hwnd));return b}};
    for(const name of ['show','showInactive','focus','moveTop','hide','maximize','minimize','restore',
      'unmaximize','setFullScreen','setAlwaysOnTop','setOpacity','setBounds']) {
      window[name]=(...args)=>{
        fixture.calls.push([hwnd,name,...args]);
        if(name==='show'||name==='showInactive')window.visible=true;
        if(name==='hide')window.visible=false;
      };
    }
    fixture.created(null,window);
    window.ready=()=>{events['ready-to-show']();viewEvents['did-finish-load']()};
    return window;
  };
  fixture.lease = (changes={}) => fixture.marker = {
    version:1,appPid:42,hwnd:'123',shellPid:99,token:'warmup',mode:'viewport',visible:false,...changes,
  };
  return fixture;
}
{
  const fixture=preloadFixture(), main=fixture.window(123), auxiliary=fixture.window(124);
  const startup=['show','showInactive','focus','moveTop','maximize','minimize','restore','unmaximize','setFullScreen','setAlwaysOnTop'];
  for(const name of startup)main[name](true);
  main.ready();auxiliary.ready();fixture.tick();fixture.tick();
  assert.deepEqual(fixture.calls,[],'ready callbacks and startup fallback must not display or activate preloads');
  main.setBounds({x:1});main.hide();main.setAlwaysOnTop(false);
  assert.deepEqual(fixture.calls.map(call=>call[1]),['setBounds','hide','setAlwaysOnTop'],
    'preloading permits native initialization and hide operations');
  fixture.calls=[];
  for(const invalid of [{appPid:43},{hwnd:'125'},{version:2},{shellPid:0}]){
    fixture.lease(invalid);main.show();main.focus();fixture.tick();
    assert.deepEqual(fixture.calls,[],'invalid lease cannot release startup visibility guard');
  }
  fixture.lease();fixture.ownerAlive=false;main.show();main.focus();fixture.tick();
  assert.deepEqual(fixture.calls,[],'dead lease owner cannot release startup visibility guard');
  fixture.ownerAlive=true;fixture.tick();
  assert.deepEqual(fixture.calls,[[123,'hide']],'valid hidden lease keeps main window hidden');
  main.show();main.focus();
  assert.equal(fixture.calls.length,1,'viewport lease owns main visibility after startup handoff');
  auxiliary.show();auxiliary.focus();
  const later=fixture.window(125);later.show();later.focus();
  assert.deepEqual(fixture.calls.slice(1),[[124,'show'],[124,'focus'],[125,'show'],[125,'focus']],
    'auxiliary windows are native after main attach, including ones created during preload');
  fixture.calls=[];fixture.marker.visible=true;fixture.tick();
  assert.deepEqual(fixture.calls,[[123,'setOpacity',1],[123,'showInactive']]);
  assert.equal(fixture.report.rendererReady,true,'renderer readiness proceeds during hidden startup');
  assert.equal(fixture.report.shown,true);fixture.tick();assert.equal(fixture.calls.length,2);
  fixture.marker.mode='released';fixture.tick();
  assert.equal(fixture.marker,null);main.show();main.focus();
  assert.deepEqual(fixture.calls.slice(-2),[[123,'show'],[123,'focus']],
    'explicit detach permanently restores display and activation');
}
{
  const fixture=preloadFixture(), main=fixture.window(123);
  fixture.lease({mode:'released',visible:false});main.ready();
  assert.equal(fixture.marker,null);main.show();main.focus();
  assert.deepEqual(fixture.calls,[[123,'hide'],[123,'show'],[123,'focus']],
    'release before the first viewport attachment restores normal behavior');
}
{
  const fixture=preloadFixture();fixture.window(123);
  fixture.lease({mode:'shutdown'});fixture.tick();
  assert.equal(fixture.quit,true);assert.deepEqual(fixture.calls,[],
    'shutdown does not need renderer readiness or a preceding viewport attach');
}
console.log('PASS: hidden preload before lease, startup no-focus, renderer ready, auxiliary windows and explicit detach');
