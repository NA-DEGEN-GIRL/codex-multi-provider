// The shell positions an independent, unowned top-level viewport window.
// Never SetParent or attach its input queue: Chromium TSF needs native activation.
// Never patch the installed application or suppress detached-window behavior.
(() => {
  const root = process.env.CODEX_MANAGER_ROOT;
  if (process.platform !== 'win32' || !root) return;
  const fs = require('node:fs');
  const path = require('node:path');
  const { app, Menu, screen } = require('electron');
  const marker = path.join(root, 'work', 'control-center', 'window-hosts', `${process.pid}.json`);
  const checks = new Set(), presentations = new Set();
  let pendingMenu, hasPendingMenu = false, quitting = false;
  const setApplicationMenu = Menu?.setApplicationMenu;
  if (typeof setApplicationMenu === 'function') {
    // Owl's global menu setter updates native styles directly, bypassing every
    // BrowserWindow.setMenu wrapper. Retain the latest menu for explicit detach.
    Menu.setApplicationMenu = function (menu) {
      if ([...checks].some(check => check())) {
        pendingMenu = menu; hasPendingMenu = true; return;
      }
      hasPendingMenu = false;
      return Reflect.apply(setApplicationMenu, this, [menu]);
    };
  }
  const timer = setInterval(() => {
    for (const present of presentations) present();
    if (hasPendingMenu && ![...checks].some(check => check())) {
      hasPendingMenu = false;
      Reflect.apply(setApplicationMenu, Menu, [pendingMenu]);
      pendingMenu = undefined;
    }
  }, 750);
  timer.unref();
  try {
    fs.mkdirSync(path.dirname(marker), {recursive:true});
    fs.watch(path.dirname(marker), {persistent:false}, (_, filename) => {
      // Health samples, render acknowledgements and other profiles share this
      // directory. They must not trigger native layout work in every process.
      if (filename && filename.toString() !== path.basename(marker)) return;
      for (const present of presentations) present();
    });
  } catch { /* The timer remains a fallback on filesystems without watches. */ }
  app.on('browser-window-created', (_, win) => {
    const hwnd = win.getNativeWindowHandle().readBigUInt64LE().toString();
    let lastHostState, recovering;
    const hostState = () => {
      try {
        const stat = fs.statSync(marker);
        if (stat.size > 32768) return false;
        const state = JSON.parse(fs.readFileSync(marker, 'utf8'));
        if (state.version !== 1 || state.appPid !== process.pid || state.hwnd !== hwnd ||
            !Number.isSafeInteger(state.shellPid) || state.shellPid <= 0) return false;
        if (state.mode === 'viewport') lastHostState = state;
        if (state.mode !== 'released') process.kill(state.shellPid, 0); // Existence only; no signal.
        return state;
      } catch { return false; }
    };
    const embedded = () => { const state = hostState(); return Boolean(state && state.mode !== 'released'); };
    // Win32 ShowWindow does not update Owl's internal visibility/compositor.
    // Keep the real non-activating show method before installing geometry guards.
    // Present on actual viewport selection, keeping native compositor state in
    // step with the shell. Repeated show/focus calls cannot restart this cycle.
    const showInactive = win.showInactive, hide = win.hide, setOpacity = win.setOpacity;
    const contents = win.webContents, setThrottling = contents.setBackgroundThrottling;
    let nativeThrottling = contents.getBackgroundThrottling?.() ?? true, selectedFrames = false;
    if (typeof setThrottling === 'function') contents.setBackgroundThrottling = function(value) {
      nativeThrottling = value;
      return Reflect.apply(setThrottling, this, [selectedFrames ? false : value]);
    };
    const keepFrames = selected => {
      if (selected === selectedFrames) return;
      selectedFrames = selected;
      if (typeof setThrottling === 'function') Reflect.apply(setThrottling, contents, [selected ? false : nativeThrottling]);
    };
    const minimum = typeof win.getMinimumSize === 'function' ? win.getMinimumSize() : undefined;
    const setMinimumSize = win.setMinimumSize;
    const setBounds = win.setBounds;
    const standaloneBounds = typeof win.getBounds === 'function' ? win.getBounds() : undefined;
    const setAlwaysOnTop = win.setAlwaysOnTop, setSkipTaskbar = win.setSkipTaskbar;
    const recoverOrphan = () => {
      if (!lastHostState?.recovery || recovering === lastHostState.token) return;
      try { process.kill(lastHostState.shellPid, 0); return; }
      catch (error) { if (error.code !== 'ESRCH') return; }
      try {
        if (typeof setAlwaysOnTop === 'function') Reflect.apply(setAlwaysOnTop, win, [false]);
        if (typeof setSkipTaskbar === 'function') Reflect.apply(setSkipTaskbar, win, [false]);
        const release = JSON.parse(fs.readFileSync(path.join(root,'artifacts','manager','current.json'),'utf8'));
        const prefix = path.resolve(root,'artifacts','manager','releases') + path.sep;
        const helper = path.resolve(release.shell);
        if (release.native_viewport_version !== 1 || !helper.toLowerCase().startsWith(prefix.toLowerCase()) ||
            path.basename(helper).toLowerCase() !== 'codex.controlcenter.exe') return;
        recovering = lastHostState.token;
        const child = require('node:child_process').spawn(helper, ['--recover-viewport', marker],
          {windowsHide:true,stdio:'ignore'});
        child.once('error', () => { recovering = undefined; });
        child.once('exit', code => {
          if (code !== 0 || embedded() || win.isDestroyed()) return;
          lastHostState = undefined;
          if (standaloneBounds) Reflect.apply(setBounds, win, [standaloneBounds, false]);
          Reflect.apply(showInactive, win, []);
        });
        child.unref();
      } catch { /* The native app and its work remain alive if recovery is unavailable. */ }
    };
    let rendererReady = false, presentedLease, viewportVisible, appliedBounds, presentedEpoch;
    let geometryAttempts = 0, lastGeometryAttempt = 0;
    const present = () => {
      if (win.isDestroyed()) return;
      const state = hostState();
      // WM_CLOSE may only hide the desktop window. Explicit manager shutdown
      // must use Electron's normal application quit path (including its cleanup
      // handlers), even when the renderer has never become ready.
      if (state?.mode === 'shutdown') {
        lastHostState = undefined;
        if (!quitting) { quitting = true; app.quit(); }
        return;
      }
      if (quitting || !rendererReady) return;
      if (!state) {
        keepFrames(false);
        recoverOrphan();
        if (presentedLease && minimum && typeof setMinimumSize === 'function') Reflect.apply(setMinimumSize, win, minimum);
        presentedLease = undefined; viewportVisible = undefined; appliedBounds = undefined;
        return;
      }
      if (state.mode === 'released') {
        keepFrames(false);
        lastHostState = undefined;
        if (minimum && typeof setMinimumSize === 'function') Reflect.apply(setMinimumSize, win, minimum);
        Reflect.apply(state.visible ? showInactive : hide, win, []);
        presentedLease = undefined; viewportVisible = undefined; appliedBounds = undefined;
        try {
          const current = JSON.parse(fs.readFileSync(marker, 'utf8'));
          if (current.token === state.token && current.mode === 'released') {
            fs.unlinkSync(marker);
            try { fs.unlinkSync(marker + '.render.json'); } catch {}
          }
        } catch {}
        return;
      }
      if (state.mode === 'viewport' && state.bounds) {
        const key = `${state.token}:` + JSON.stringify(state.bounds);
        const {x,y,width,height,dpi} = state.bounds;
        const pixels = {x,y,width,height};
        const scale = dpi / 96;
        const logical = typeof screen?.screenToDipRect === 'function' ? screen.screenToDipRect(win, pixels) :
          {x:Math.round(x/scale),y:Math.round(y/scale),width:Math.round(width/scale),height:Math.round(height/scale)};
        const actual = typeof win.getBounds === 'function' ? win.getBounds() : undefined;
        const matches = actual && Object.keys(logical).every(k => Math.abs(actual[k] - logical[k]) <= 1);
        const changed = key !== appliedBounds;
        if (changed || matches) geometryAttempts = 0;
        if (changed || (!matches && actual && geometryAttempts < 3 && Date.now() - lastGeometryAttempt >= 1000)) {
          appliedBounds = key;
          geometryAttempts++; lastGeometryAttempt = Date.now();
          if (typeof setMinimumSize === 'function') Reflect.apply(setMinimumSize, win, [0,0]);
          Reflect.apply(setBounds, win, [logical, false]);
        }
      }
      if (state.mode === 'viewport' && !state.visible) {
        keepFrames(false);
        // Update Owl/Chromium as well as the HWND. A Win32-only hide leaves its
        // compositor thinking it is still visible and restore can stay gray.
        if (viewportVisible !== false) Reflect.apply(hide, win, []);
        viewportVisible = false;
        return;
      }
      const lease = state.token || `${state.shellPid}:${state.hwnd}`;
      const epoch = state.presentationEpoch || 0;
      if (presentedLease === lease && presentedEpoch === epoch && viewportVisible !== false) return;
      // A fast minimize/restore may coalesce the hidden marker write. The epoch
      // still forces one compositor transition, not a repeating show loop.
      if (presentedLease === lease && presentedEpoch !== epoch && viewportVisible !== false)
        Reflect.apply(hide, win, []);
      let shown = false, error;
      try {
        keepFrames(true);
        if (state.mode === 'viewport' && typeof setMinimumSize === 'function') Reflect.apply(setMinimumSize, win, [0,0]);
        if (typeof setOpacity === 'function') Reflect.apply(setOpacity, win, [1]);
        // A Win32-hidden surface still needs its native render view presented
        // on selection. Show without activating or replacing the editor.
        Reflect.apply(showInactive, win, []);
        // Owl has no webContents.invalidate. A Win32-only resize/show leaves
        // its compositor's old same-size surface intact. Commit a native size
        // transition once per selection/restore, then the exact target bounds.
        // The viewport clip hides the extra pixel; never activate or reload.
        if (state.mode === 'viewport' && state.bounds) {
          const {x,y,width,height,dpi} = state.bounds, scale = dpi / 96;
          const target = typeof screen?.screenToDipRect === 'function' ? screen.screenToDipRect(win, {x,y,width,height}) :
            {x:Math.round(x/scale),y:Math.round(y/scale),width:Math.round(width/scale),height:Math.round(height/scale)};
          Reflect.apply(setBounds, win, [{...target, width:target.width + 1}, false]);
          Reflect.apply(setBounds, win, [target, false]);
        }
        shown = win.isVisible();
        if (shown) { presentedEpoch = epoch; presentedLease = lease; viewportVisible = true; }
      } catch (failure) { error = String(failure).slice(0, 500); }
      try {
        fs.writeFileSync(marker + '.render.json', JSON.stringify({version:1,
          appPid:process.pid, hwnd, shellPid:state.shellPid, token:state.token,
          rendererReady, shown, presentationEpoch:epoch, error}));
      } catch { /* Diagnostic failure must not prevent display. */ }
    };
    const ready = () => { rendererReady = true; present(); };
    win.once('ready-to-show', ready);
    win.webContents.once('did-finish-load', ready);
    checks.add(embedded);
    presentations.add(present);
    win.once('closed', () => { checks.delete(embedded); presentations.delete(present); });
    for (const name of ['setTitleBarOverlay', 'setBackgroundMaterial', 'setBounds', 'setContentBounds',
      'setSize', 'setContentSize', 'setMinimumSize', 'setMaximumSize', 'setPosition',
      'setMenu', 'setMenuBarVisibility', 'setAutoHideMenuBar', 'center', 'setResizable', 'setMaximizable',
      'setMinimizable', 'setFullScreenable', 'setMovable', 'setClosable', 'setHasShadow',
      'setAlwaysOnTop', 'setSkipTaskbar', 'setOpacity', 'setBackgroundColor', 'setTitle',
      'show', 'showInactive', 'hide', 'maximize', 'minimize', 'restore', 'unmaximize', 'setFullScreen', 'focus', 'blur']) {
      const original = win[name];
      if (typeof original !== 'function') continue;
      win[name] = function (...args) {
        const state = hostState();
        if (!state || state.mode === 'released') {
          // The native startup fade may still be at zero when hosting starts.
          // Managed windows start opaque; the shell controls when they appear.
          if (name === 'setOpacity') args = [1];
          return Reflect.apply(original, this, args);
        }
        if (name === 'show' || name === 'showInactive') present();
      };
    }
  });
})();
