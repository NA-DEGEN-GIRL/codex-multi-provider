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
const end = source.indexOf('stageNotificationSoundIfNeeded(){',start);
assert(start>0&&end>start);
const bodyHelper = source.slice(start,end).match(/body:([\w$]+)\(e\.body\)/)?.[1];
assert(bodyHelper, 'Native notification body helper was not found; review this app version.');
const activations=[], navigations=[], events={};
const owner={isDestroyed:()=>false};
const methods = vm.runInNewContext('({'+source.slice(start,end)+'})',{
  R_:s=>s,Wm:s=>s,[bodyHelper]:s=>s,process:{platform:'win32'},
  __codexManagerNotificationClick:(notice,origin)=>activations.push({notice,origin})
});
const native={...methods,stageNotificationSoundIfNeeded(){},isSupported:()=>true,
  options:{platform:'win32'},logger:{info(){},warning(){}},deliveredNotificationIds:new Set(),notifications:new Map(),
  createNotification:()=>({on:(name,fn)=>events[name]=fn,show(){}}),emitCompletedThreadsChanged(){},removeNotification(){}
};
const notification={id:'native-test',kind:'turn-complete',title:'task',body:'finished',conversationId:'thread-id',navigationPath:'/remote/test/thread-id'};
native.showNotification(notification,owner,action=>{navigations.push(action);return owner});
assert.equal(activations.length,0);
assert.equal(navigations.length,0);
events.click();
assert.equal(navigations.length,1);assert.equal(navigations[0].actionType,'open');
assert.equal(activations.length,1);assert.equal(activations[0].origin,owner);
assert.equal(activations[0].notice.navigationPath,notification.navigationPath);
console.log('PASS: actual patched native toast click invokes original task navigation once, then notifies the enclosing manager; display alone does neither');
