// Read-only view selection. No resume, navigation, focus or model request.
(() => {
  const root = process.env.CODEX_MANAGER_ROOT, profile = process.env.CODEX_MANAGER_PROFILE_ID,
    generation = process.env.CODEX_MANAGER_GENERATION;
  const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
  if (process.platform !== 'win32' || !root || !uuid.test(profile || '') || !uuid.test(generation || '')) return;
  const fs = require('node:fs/promises'), path = require('node:path'), {BrowserWindow} = require('electron');
  const directory = path.join(root,'work','control-center','instances',profile), file = path.join(directory,'active-task.json');
  const nativeFs = require('node:fs');
  const marker = path.join(root,'work','control-center','window-hosts',`${process.pid}.json`);
  let sequence = 0, latest, writing = false, hostedHwnd;
  async function flush() {
    if(writing) return;
    writing = true;
    try {
      await fs.mkdir(directory,{recursive:true});
      while(latest) {
        const value = latest;
        const temporary = file + `.${process.pid}.tmp`;
        await fs.writeFile(temporary, JSON.stringify(value), 'utf8');
        await fs.rename(temporary,file);
        if(latest === value) latest = null;
      }
    } catch { /* Missing context disables editing; never fall back to last RPC. */ }
    finally { writing = false; if(latest) { const retry = setTimeout(flush, 1000); retry.unref?.(); } }
  }
  globalThis.__codexManagerTaskContext = (sender, route, title) => {
    try {
      const win = BrowserWindow.fromWebContents(sender);
      if(!win || win.isDestroyed() || typeof route !== 'string' || route.length > 4096) return;
      const hwnd = win.getNativeWindowHandle().readBigUInt64LE().toString();
      // Avatar, settings/popout and primary windows all send renderer routes.
      // Only the hosted editor may replace the profile's selected task.
      let scoped = false;
      try {
        const lease = JSON.parse(nativeFs.readFileSync(marker,'utf8'));
        if(lease.version===1 && lease.appPid===process.pid && lease.mode==='viewport') {
          scoped = true;
          hostedHwnd = lease.hwnd;
          if(lease.hwnd!==hwnd)return;
        }
      } catch { }
      if(hostedHwnd && hwnd !== hostedHwnd) return;
      if(!scoped && BrowserWindow.getAllWindows?.().find(w=>!w.isDestroyed()) !== undefined &&
          BrowserWindow.getAllWindows().find(w=>!w.isDestroyed()) !== win) return;
      globalThis.__codexManagerNavigation?.ready(sender);
      const url = new URL(route,'https://codex.invalid');
      const match = url.pathname.match(/^\/local\/([^/?#]+)\/?$/);
      let thread = match ? decodeURIComponent(match[1]) : null;
      if(!uuid.test(thread || '')) thread = null;
      const host = url.searchParams.get('hostId') || 'local';
      if(host.length>256 || /[\x00-\x1f]/.test(host)) return;
      latest = {version:1,profile_id:profile,generation,app_pid:process.pid,
        hwnd,sequence:++sequence,
        host_id:host,thread_id:thread,title:typeof title==='string'?title.slice(0,200):'',updated_at:Date.now()};
      void flush();
    } catch { }
  };
})();
