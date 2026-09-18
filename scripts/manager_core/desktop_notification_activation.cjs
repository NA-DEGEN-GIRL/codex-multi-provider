// The native callback still owns exact task/host navigation. Tell the enclosing
// shell about an explicit toast click only; never forward completion events.
(() => {
  if (process.platform !== 'win32' || !process.env.CODEX_MANAGER_ROOT) return;
  const fs = require('node:fs/promises');
  const path = require('node:path');
  const net = require('node:net');
  const { BrowserWindow } = require('electron');
  const marker = path.join(process.env.CODEX_MANAGER_ROOT, 'work', 'control-center',
    'window-hosts', `${process.pid}.json`);
  const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
  const profile = process.env.CODEX_MANAGER_PROFILE_ID, turns = new Map();
  let writingTurns = false, turnsDirty = false, turnsLoaded = false;
  const turnDirectory = path.join(process.env.CODEX_MANAGER_ROOT,'work','control-center','notifications','activity');
  async function saveTurns() {
    if(writingTurns) return; writingTurns = true;
    try {
      await fs.mkdir(turnDirectory,{recursive:true});
      if (!turnsLoaded) {
        turnsLoaded = true;
        try {
          const previous = JSON.parse(await fs.readFile(path.join(turnDirectory,profile+'.json'),'utf8'));
          if(previous.version===1 && previous.profileId===profile && Array.isArray(previous.turns))
            for(const turn of previous.turns.slice(-256))
              if(uuid.test(turn.threadId || '') && typeof turn.hostId==='string' && turn.hostId.length<=256 && Number.isSafeInteger(turn.at)) {
                const key=turn.hostId+'/'+turn.threadId;
                if(!turns.has(key)) turns.set(key,turn);
              }
          while(turns.size>256) {
            const oldest=[...turns].sort((a,b)=>a[1].at-b[1].at)[0][0];turns.delete(oldest);
          }
        } catch { }
      }
      while(turnsDirty) {
        turnsDirty=false;
        const file=path.join(turnDirectory,profile+'.json'),temporary=file+'.'+process.pid+'.tmp';
        await fs.writeFile(temporary,JSON.stringify({version:1,profileId:profile,turns:[...turns.values()]}));
        await fs.rename(temporary,file);
      }
    } catch { } finally { writingTurns=false; }
  }
  globalThis.__codexManagerNotificationActivity = (hostId, method, params) => {
    if(method !== 'turn/started' || !uuid.test(profile || '')) return;
    const threadId=params?.thread?.id || params?.threadId;
    if(!uuid.test(threadId || '') || typeof hostId!=='string' || !hostId || hostId.length>256 || /[\x00-\x1f]/.test(hostId))return;
    const key=hostId+'/'+threadId; turns.delete(key);turns.set(key,{threadId,hostId,at:Date.now()});
    while(turns.size>256)turns.delete(turns.keys().next().value);
    turnsDirty=true;void saveTurns();
  };
  async function readLease() {
    const stat = await fs.stat(marker);
    if (stat.size > 32768) return null;
    const lease = JSON.parse(await fs.readFile(marker, 'utf8'));
    if (lease.version !== 1 || lease.appPid !== process.pid || !Number.isSafeInteger(lease.shellPid) ||
        lease.shellPid <= 0 || !/^[a-f0-9]{32}$/.test(lease.token || '') ||
        !new RegExp(`^codex-workspace-notify-${lease.shellPid}-[a-f0-9]{32}$`).test(lease.notificationPipe || '')) return null;
    process.kill(lease.shellPid, 0);
    return lease;
  }
  globalThis.__codexManagerNotificationShow = async (notice, contents, fallback) => {
    let accepted = false;
    const clickedAt = Date.now();
    try {
      const win = contents && BrowserWindow.fromWebContents(contents), lease = await readLease();
      if (!win || win.isDestroyed() || !lease || lease.mode !== 'viewport' ||
          win.getNativeWindowHandle().readBigUInt64LE().toString() !== lease.hwnd) throw Error('not hosted');
      const route = notice.navigationPath ? new URL(notice.navigationPath, 'https://codex.invalid') : null;
      const threadId = route ? route.pathname.match(/^\/local\/([^/?#]+)\/?$/)?.[1] : notice.conversationId;
      const hostId = route?.searchParams.get('hostId') || 'local';
      if (!uuid.test(threadId || '') || hostId.length > 256 || /[\x00-\x1f]/.test(hostId)) throw Error('unsupported route');
      const message = {version:1, kind:'show', appPid:process.pid, hwnd:lease.hwnd, token:lease.token,
        clickedAt, id:require('node:crypto').randomUUID(), notification:{id:String(notice.id).slice(0,256),
          kind:notice.kind,title:String(notice.title || '').slice(0,300),body:String(notice.body || '').slice(0,2000),threadId,hostId}};
      accepted = await new Promise(resolve => {
        const client = net.createConnection(`\\\\.\\pipe\\${lease.notificationPipe}`);
        let data = '', done = false;
        const finish = value => { if (done) return; done = true; client.destroy(); resolve(value); };
        client.setTimeout(1800, () => finish(false));
        client.on('error', () => finish(false)); client.on('end', () => finish(false));
        client.once('connect', () => client.write(JSON.stringify(message) + '\n'));
        client.on('data', chunk => {
          data += chunk.toString();
          if(data.length > 1024) return finish(false);
          if(data.includes('\n')) { try { finish(JSON.parse(data).accepted === true); } catch { finish(false); } }
        });
        client.unref();
      });
    } catch { }
    if (!accepted) fallback();
  };

  // Navigate using the app's own queued renderer-message path. No OS codex://
  // handler, foreground change, synthetic input, model request or ownership move.
  let navigating = false, completed;
  const managers = new Map();
  const readyViews = new Set();
  const commandFile = marker + '.navigate.json';
  async function navigate() {
    if (!managers.size || navigating) return;
    navigating = true;
    try {
      const lease = await readLease();
      if (!lease || lease.mode !== 'viewport') return;
      if ((await fs.stat(commandFile)).size > 8192) return;
      const command = JSON.parse(await fs.readFile(commandFile,'utf8'));
      if (command.id === completed || !/^[a-f0-9]{32}$/.test(command.id || '') || command.token !== lease.token ||
          command.appPid !== process.pid || command.hwnd !== lease.hwnd || command.shellPid !== lease.shellPid ||
          command.createdAt < Date.now() - 30000 || command.createdAt > Date.now() + 1000 ||
          !uuid.test(command.threadId || '') || typeof command.hostId !== 'string' || !command.hostId ||
          command.hostId.length > 256 || /[\x00-\x1f]/.test(command.hostId)) return;
      const win = BrowserWindow.getAllWindows().find(w => !w.isDestroyed() &&
        w.getNativeWindowHandle().readBigUInt64LE().toString() === lease.hwnd);
      if (!win || win.webContents.isDestroyed() || !readyViews.has(win.webContents.id)) return;
      const manager = managers.get(win.webContents.id);
      if (!manager || (typeof manager.isWebContentsReady === 'function' && !manager.isWebContentsReady(win.webContents.id))) return;
      completed = command.id;
      const route = '/local/' + command.threadId + (command.hostId === 'local' ? '' : '?hostId=' + encodeURIComponent(command.hostId));
      manager.sendMessageToWebContents(win.webContents,{type:'navigate-to-route',path:route});
      await fs.writeFile(commandFile + '.ack', JSON.stringify({id:command.id,token:lease.token,threadId:command.threadId,hostId:command.hostId}));
    } catch { }
    finally { navigating = false; }
  }
  globalThis.__codexManagerNavigation = {
    register(value, contents) { if(managers.get(contents.id) === value) return; managers.set(contents.id,value); void navigate(); },
    ready(contents) { if(readyViews.has(contents.id))return;readyViews.add(contents.id);void navigate(); }
  };
  try {
    require('node:fs').watch(path.dirname(marker), {persistent:false}, (_, file) => {
      if (file?.toString() === path.basename(commandFile)) void navigate();
    });
  } catch { }
  const navigationTimer = setInterval(navigate, 1000); navigationTimer.unref();
  globalThis.__codexManagerNotificationClick = async (notification, contents) => {
    const clickedAt = Date.now();
    try {
      const win = contents && BrowserWindow.fromWebContents(contents);
      if (!win || win.isDestroyed()) return;
      const hwnd = win.getNativeWindowHandle().readBigUInt64LE().toString();
      const stat = await fs.stat(marker);
      if (stat.size > 2048) return;
      const lease = JSON.parse(await fs.readFile(marker, 'utf8'));
      if (lease.version !== 1 || lease.appPid !== process.pid || lease.hwnd !== hwnd ||
          !Number.isSafeInteger(lease.shellPid) || lease.shellPid <= 0 ||
          !/^[a-f0-9]{32}$/.test(lease.token || '') ||
          !new RegExp(`^codex-workspace-notify-${lease.shellPid}-[a-f0-9]{32}$`).test(lease.notificationPipe || '')) return;
      process.kill(lease.shellPid, 0);
      if (Date.now() - clickedAt > 1500) return;
      const client = net.createConnection(`\\\\.\\pipe\\${lease.notificationPipe}`);
      client.setTimeout(1500, () => client.destroy());
      client.on('error', () => client.destroy());
      client.once('connect', () => {
        client.end(JSON.stringify({version: 1, appPid: process.pid, hwnd, token: lease.token,
          clickedAt, id: require('node:crypto').randomUUID()}) + '\n');
      });
      client.unref();
    } catch { /* Detached/closed shell: preserve ordinary native notification behavior. */ }
  };
})();
