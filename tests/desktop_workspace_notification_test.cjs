const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs'),{EventEmitter}=require('node:events');
const source=fs.readFileSync(require('node:path').join(__dirname,'../scripts/manager_core/desktop_notification_activation.cjs'),'utf8');
const token='a'.repeat(32),thread='00000000-0000-4000-9000-000000000004';
let lease={version:1,appPid:42,hwnd:'123',shellPid:99,token,mode:'viewport',notificationPipe:'codex-workspace-notify-99-'+'b'.repeat(32)};
let command,ack,tick,accepted=true,sent=[],fallback=0,routes=[];
const win={isDestroyed:()=>false,webContents:{id:1,isDestroyed:()=>false},getNativeWindowHandle(){const b=Buffer.alloc(8);b.writeBigUInt64LE(123n);return b}};
const context={URL,Date,process:{platform:'win32',pid:42,env:{CODEX_MANAGER_ROOT:'fixture'},kill(){}},
 setInterval(fn){tick=fn;return{unref(){}}},require(name){
  if(name==='electron')return{BrowserWindow:{fromWebContents:()=>win,getAllWindows:()=>[win]}};
  if(name==='node:fs')return{watch(){}};
  if(name==='node:fs/promises')return{async stat(){return{size:800}},async readFile(file){return JSON.stringify(file.endsWith('.navigate.json')?command:lease)},async writeFile(file,data){ack=JSON.parse(data)}};
  if(name==='node:net')return{createConnection(){const client=new EventEmitter();client.setTimeout=()=>{};client.unref=()=>{};client.destroy=()=>{};
    client.write=data=>{sent.push(JSON.parse(data));queueMicrotask(()=>client.emit('data',JSON.stringify({accepted})+'\n'))};
    queueMicrotask(()=>client.emit('connect'));return client}};
  return require(name);
 }};
vm.runInNewContext(source,context);
(async()=>{
 const notice={id:'question-1',kind:'question',title:'작업',body:'질문 <test>',navigationPath:'/local/'+thread+'?hostId=ssh-01'};
 await context.__codexManagerNotificationShow(notice,win.webContents,()=>fallback++);
 assert.equal(fallback,0);assert.equal(sent[0].notification.hostId,'ssh-01');assert.equal(sent[0].notification.threadId,thread);
 assert.equal(routes.length,0,'showing a toast must never navigate or focus');
 accepted=false;await context.__codexManagerNotificationShow(notice,win.webContents,()=>fallback++);assert.equal(fallback,1);
 lease.mode='released';await context.__codexManagerNotificationShow(notice,win.webContents,()=>fallback++);assert.equal(fallback,2);
 lease.mode='viewport';await context.__codexManagerNotificationShow({...notice,navigationPath:'/settings'},win.webContents,()=>fallback++);assert.equal(fallback,3);
 command={id:'c'.repeat(32),token,appPid:42,hwnd:'123',shellPid:99,createdAt:Date.now(),threadId:thread,hostId:'ssh with spaces'};
 await tick();assert.equal(routes.length,0,'cold navigation waits for native route dispatcher');
 const dispatcher={sendMessageToWebContents(contents,message){assert.equal(contents,win.webContents);routes.push(message)}};
 context.__codexManagerNavigation.register(dispatcher,win.webContents);await new Promise(r=>setImmediate(r));
 context.__codexManagerNavigation.register({sendMessageToWebContents(){assert.fail('auxiliary dispatcher must not receive a primary task')}},{id:2});
 assert.equal(routes.length,0,'native window ready must still wait for its actual task router');
 context.__codexManagerNavigation.ready(win.webContents);await new Promise(r=>setImmediate(r));
 assert.equal(routes.length,1);assert.equal(routes[0].path,'/local/'+thread+'?hostId=ssh%20with%20spaces');assert.equal(ack.id,command.id);
 await tick();assert.equal(routes.length,1,'duplicate command must not move the editor again');
 command={...command,id:'d'.repeat(32),token:'wrong'};await tick();assert.equal(routes.length,1);
 command={...command,token,createdAt:Date.now()-40000};await tick();assert.equal(routes.length,1);
 command={...command,createdAt:Date.now(),threadId:'../settings'};await tick();assert.equal(routes.length,1);
 console.log('PASS: question notification redirects only after manager acceptance; detached/failed fallback; exact local/SSH navigation, cold readiness and stale/forged replay rejection');
})().catch(e=>{console.error(e);process.exitCode=1});
