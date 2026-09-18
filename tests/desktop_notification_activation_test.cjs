const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../scripts/manager_core/desktop_notification_activation.cjs'), 'utf8');
const token = 'a'.repeat(32), pipe = 'codex-workspace-notify-99-' + 'b'.repeat(32);
let marker = {version: 1, appPid: 42, hwnd:'123', shellPid:99, token, notificationPipe:pipe};
let living = true, sent = [], delay = 0;
let win = {isDestroyed:()=>false, getNativeWindowHandle(){const b=Buffer.alloc(8);b.writeBigUInt64LE(123n);return b}};
const context = {process:{platform:'win32',pid:42,env:{CODEX_MANAGER_ROOT:'fixture'},kill(pid,signal){assert.equal(signal,0);if(!living)throw Error('closed')}}, Date, require(name){
  if(name==='electron')return {BrowserWindow:{fromWebContents:()=>win}};
  if(name==='node:fs/promises')return {async stat(){return {size:100}},async readFile(){if(delay)await new Promise(r=>setTimeout(r,delay));return JSON.stringify(marker)}};
  if(name==='node:net')return {createConnection(name){assert.equal(name,'\\\\.\\pipe\\'+pipe);return {
    setTimeout(){},on(){},once(event,cb){assert.equal(event,'connect');cb()},end(data){sent.push(JSON.parse(data))},unref(){},destroy(){}
  }}};
  return require(name);
}};
context.setInterval=()=>({unref(){}});
vm.runInNewContext(source,context);
(async()=>{
  assert.equal(sent.length,0); // Loading/showing a notification never activates anything.
  await context.__codexManagerNotificationClick({conversationId:'remote-task'}, {});
  assert.equal(sent.length,1); assert.equal(sent[0].appPid,42); assert.equal(sent[0].hwnd,'123');
  assert.equal(sent[0].token,token); assert.equal(sent[0].version,1);
  assert.equal(Object.hasOwn(sent[0],'conversationId'),false); // Native route stays authoritative, including SSH.
  marker.hwnd='999'; await context.__codexManagerNotificationClick({},{});assert.equal(sent.length,1);
  marker.hwnd='123';marker.notificationPipe='codex-ipc';await context.__codexManagerNotificationClick({},{});assert.equal(sent.length,1);
  marker.notificationPipe=pipe;living=false;await context.__codexManagerNotificationClick({},{});assert.equal(sent.length,1);
  living=true;delay=1550;await context.__codexManagerNotificationClick({},{});assert.equal(sent.length,1);
  marker=null;delay=0;await context.__codexManagerNotificationClick({},{});assert.equal(sent.length,1);
  win=null;await context.__codexManagerNotificationClick({},{});assert.equal(sent.length,1);
  console.log('PASS: exact window lease, click-only routing, no task/host replacement, detached/dead/slow/mismatched drops');
})().catch(e=>{console.error(e);process.exitCode=1});
