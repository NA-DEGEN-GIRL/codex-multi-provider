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
