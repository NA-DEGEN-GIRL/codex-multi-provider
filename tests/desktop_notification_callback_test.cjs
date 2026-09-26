// Execute the actual version-checked method from the patched desktop archive.
// No real toast, account, window or global deep-link handler is invoked.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const directory = process.argv[2];
const metadata = JSON.parse(fs.readFileSync(path.join(directory,'manager-desktop.json'),'utf8'));
const archive = fs.readFileSync(path.join(directory,'resources/app.asar'));
const header = JSON.parse(archive.subarray(16,16+archive.readUInt32LE(12)).toString());
let entry = header;
for(const part of metadata.patch.notification_module.split('/'))entry=entry.files[part];
const base = 8+archive.readUInt32LE(4)+Number(entry.offset);
const source = archive.subarray(base,base+entry.size).toString();
const start = source.indexOf('showNotification(e,t,n,r){');
// 26.915 ends at stageNotificationSoundIfNeeded(); 26.917 adds playBundledNotificationSound before it.
const end = source.indexOf('}stageNotificationSoundIfNeeded(',start)+1;
assert(start>0&&end>start);
const bodyHelper = source.slice(start,end).match(/body:([\w$]+)\(e\.body\)/)?.[1];
assert(bodyHelper, 'Native notification body helper was not found; review this app version.');
const soundNames = source.slice(start,end).match(/([\w$]+)\.classic\b/)?.[1];
const activations=[], navigations=[], events={}, shown=[], sounds=[];
const owner={isDestroyed:()=>false};
const context={R_:s=>s,Wm:s=>s,[bodyHelper]:s=>s,process:{platform:'win32'},
  ...(soundNames?{[soundNames]:{default:'codex-notification',classic:'codex-classic'}}:{}),
  __codexManagerNotificationClick:(notice,origin)=>activations.push({notice,origin})
};
const methods = vm.runInNewContext('(class{'+source.slice(start,end)+'})',context).prototype;
const native=Object.assign(Object.create(methods),{stageNotificationSoundIfNeeded(){},playBundledNotificationSound:kind=>sounds.push(kind),isSupported:()=>true,
  options:{platform:'win32'},logger:{info(){},warning(){}},deliveredNotificationIds:new Set(),notifications:new Map(),
  createNotification:()=>({on:(name,fn)=>events[name]=fn,show(){shown.push(1)}}),emitCompletedThreadsChanged(){},removeNotification(){}
});
const notification={id:'native-test',kind:'turn-complete',title:'task',body:'finished',conversationId:'thread-id',navigationPath:'/remote/test/thread-id'};
native.showNotification(notification,owner,action=>{navigations.push(action);return owner});
assert.equal(activations.length,0);
assert.equal(navigations.length,0);
events.click();
assert.equal(navigations.length,1);assert.equal(navigations[0].actionType,'open');
assert.equal(activations.length,1);assert.equal(activations[0].origin,owner);
assert.equal(activations[0].notice.navigationPath,notification.navigationPath);
// Without the manager the native toast (and 26.917's bundled Windows sound) runs once.
const nativeSounds=sounds.length;
assert.equal(shown.length,1);assert.equal(nativeSounds,soundNames?1:0);
// An accepted workspace toast replaces the whole native presentation; a declined one restores it.
const offered=[];
context.__codexManagerNotificationShow=(notice,contents,fallback)=>{offered.push({notice,contents,fallback})};
native.showNotification({...notification,id:'native-test-2'},owner,()=>owner);
assert.equal(offered.length,1);assert.equal(offered[0].notice.id,'native-test-2');assert.equal(offered[0].contents,owner);
assert.equal(shown.length,1);assert.equal(sounds.length,nativeSounds);
offered[0].fallback();
assert.equal(shown.length,2);assert.equal(sounds.length,2*nativeSounds);
console.log('PASS: actual patched native toast click invokes original task navigation once, then notifies the enclosing manager; display alone does neither; an accepted workspace toast replaces the native toast and sound, a declined one restores both');
