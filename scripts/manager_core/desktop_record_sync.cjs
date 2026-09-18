// Cross-window invalidations only. Titles/history come from each app's runtime;
// the signal never navigates, focuses, changes credentials or submits a turn.
(() => {
  const directory = process.env.CODEX_RECORD_SIGNALS || process.env.CODEX_MANAGER_RECORD_SIGNALS;
  if (!directory) return;
  const fs = require('node:fs').promises;
  const path = require('node:path');
  const uuid = /^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i;
  const managers = new Set(), seen = new Map(), pending = new Map();
  const deletedKeys = new Set();
  const archivedKeys = new Set(), visibility = new Map();
  const refreshing = new WeakMap(), activity = new WeakMap();
  const profile = process.env.CODEX_MANAGER_PROFILE_ID;
  const ownFile = (profile || '00000000-0000-4000-8000-000000000001') + (profile ? '.desktop.json' : '.json');
  const generation = require('node:crypto').randomUUID();
  const changed = new Map();
  const signals = new Set(['thread/started','thread/name/updated','thread/settings/updated',
    'thread/project/updated',
    'thread/archived','thread/unarchived','thread/deleted','turn/started','turn/completed',
    'item/started','item/completed','item/agentMessage/delta']);
  let sequence = 0, dirty = false;
  let catalog = null, running = false, stopped = false;
  const counters = { scans: 0, listRefreshes: 0, historyRefreshes: 0, deferred: 0, unavailable: 0, failures: 0 };
  let lastDiagnostic = 0;
  const active = (manager, id) => {
    // A hydrated status may describe a turn running in ANOTHER runtime. Using
    // that status here leaves the viewer stuck at its first active snapshot.
    // Only this manager's requests/native notifications block history merges.
    const local = activity.get(manager);
    return local?.turns.has(id) || local?.requests.has(id) ||
      manager.hasInFlightConversationResume(id);
  };
  const localManagers = () => {
    // Replaced native managers can own entire transcript stores. A strong Set
    // must release them; merely skipping disposed entries retains those graphs.
    for (const manager of managers) if (manager.disposed) managers.delete(manager);
    return [...managers];
  };
  async function scan() {
    let names;
    try { names = await fs.readdir(directory); }
    catch (error) { if (error.code === 'ENOENT') return; throw error; }
    const files = names.filter(f => f !== ownFile && f !== profile+'.json' && /^[a-f0-9-]{36}(?:\.desktop)?\.json$/i.test(f)).slice(0, 64);
    for (const file of files) {
      try {
      const full = path.join(directory, file), info = await fs.stat(full);
      if (info.size > 32768 || !info.isFile()) continue;
      const version = `${info.mtimeMs}:${info.size}`;
      if (seen.get(file)?.fileVersion === version) continue;
      const data = JSON.parse(await fs.readFile(full, 'utf8'));
      if (![1,2].includes(data.version) || typeof data.generation !== 'string' ||
          !Array.isArray(data.changes) || data.changes.length > 256) continue;
      const previous = seen.get(file), sequence = previous?.generation === data.generation ? previous.sequence : 0;
      let last = sequence;
      for (const change of data.changes) {
        if (!Array.isArray(change) || ![2,4].includes(change.length)) continue;
        const [id, seq, host = 'local', kind = 'changed'] = change;
        if (typeof host !== 'string' || host.length > 256 || !['changed','deleted','archived','unarchived'].includes(kind)) continue;
        if (!uuid.test(id) || !Number.isSafeInteger(seq) || seq <= sequence) continue;
        last = Math.max(last, seq);
        const key=host+'\0'+id;
        if(kind==='deleted')deletedKeys.add(key);
        if(kind!=='deleted'&&deletedKeys.has(key))continue;
        if(kind==='archived')archivedKeys.add(key);
        if(kind==='unarchived')archivedKeys.delete(key);
        if(kind==='changed'&&archivedKeys.has(key))continue;
        while(archivedKeys.size>4096)archivedKeys.delete(archivedKeys.values().next().value);
        while(deletedKeys.size>4096)deletedKeys.delete(deletedKeys.values().next().value);
        pending.set(key, { id, host, kind, deleted:kind==='deleted', remaining: 3, due: Date.now(), failures: 0 });
      }
      while (pending.size > 1024) pending.delete(pending.keys().next().value);
      seen.set(file, { fileVersion: version, generation: data.generation, sequence: last });
      } catch { counters.failures++; }
    }
  }
  async function tick() {
    if (running || stopped) return;
    running = true;
    try {
      counters.scans++;
      // Managed runtimes already publish through their proxy. The original
      // desktop needs the same notifications to travel in the other direction.
      if (dirty) {
        const entries = [...changed.values()], serial = sequence;
        await fs.mkdir(directory, {recursive:true});
        const full = path.join(directory, ownFile), temporary = full + '.' + generation + '.tmp';
        await fs.writeFile(temporary, JSON.stringify({version:2,generation,changes:entries}), 'utf8');
        await fs.rename(temporary, full);
        if (sequence === serial) dirty = false;
      }
      await scan();
      const locals = localManagers();
      if (!locals.length) return;
      const visible = key => {const e=pending.get(key);return locals.some(m => m.hostId===e.host && m.threadStore.isConversationActive(e.id));};
      const ids = [...pending].filter(([, state]) => state.due <= Date.now() && locals.some(m=>m.hostId===state.host))
        .sort(([a], [b]) => Number(visible(b)) - Number(visible(a)))
        .slice(0, 8).map(([id]) => id);
      if (!ids.length) return;
      const imports = new Map(), deletions = new Map(), archives = new Map(), restores = new Map();
      // Unpersisted/ephemeral IDs are not imports. Native import retries are
      // unbounded, so only publish a summary after its history read succeeds.
      for (const key of ids) {
        const current = pending.get(key), {id, host} = current;
        const targets=locals.filter(m=>m.hostId===host);
        if(!targets.length)continue;
        if(current.deleted){
          for(const m of targets)m.handleThreadDeletion([id]);
          if(!deletions.has(host))deletions.set(host,[]);deletions.get(host).push(id);
          pending.delete(key);continue;
        }
        if(current.kind==='archived'){
          for(const m of targets)m.handleThreadArchived(id);
          if(!archives.has(host))archives.set(host,[]);archives.get(host).push(id);
          pending.delete(key);continue;
        }
        if(current.kind==='unarchived'){
          for(const m of targets)m.handleThreadUnarchived(id);
          if(!restores.has(host))restores.set(host,[]);restores.get(host).push(id);
          current.kind='changed';
        }
        let deferred = false, failed = false, available = false;
        for (const manager of targets) {
          if (active(manager, id)) { deferred = true; counters.deferred++; continue; }
          refreshing.set(manager.threadStore, { manager, id });
          try {
            manager.threadStore.backgroundThreadLookups?.delete(id);
            manager.threadStore.threadReadStates?.delete(id);
            await manager.threadStore.hydrateThreads([id], {
              addToRecentConversations: true,
              includeTurns: true, maxTurns: 8,
              retainHistoryPagination: true, notifyAnyCallbacks: true, throwOnReadError: true
            });
            if (active(manager, id)) { deferred = true; continue; }
            const summary = manager.threadStore.threadsById.get(id);
            if (!summary) { failed = true; counters.unavailable++; continue; }
            if (summary.name) manager.threadStore.applyThreadTitleUpdate(id, summary.name);
            available = true;
            counters.historyRefreshes++;
          } catch (error) {
            failed = true;
            if (/thread not loaded|thread.*not found/i.test(String(error?.message || error))) counters.unavailable++;
            else counters.failures++;
          } finally { refreshing.delete(manager.threadStore); }
        }
        if (available) {if(!imports.has(host))imports.set(host,[]);imports.get(host).push(id);}
        if (pending.get(key) !== current) continue;
        current.due = Date.now() + (failed ? 3000 : deferred ? 1500 : 700);
        if (failed) {
          if (++current.failures >= 3) pending.delete(key);
        } else if (!deferred && --current.remaining <= 0) pending.delete(key);
      }
      for(const host of new Set([...imports.keys(),...deletions.keys(),...archives.keys(),...restores.keys()])) {
        const threadIds=imports.get(host)||[], deletedThreadIds=deletions.get(host)||[];
        try {
          for(const win of require('electron').BrowserWindow.getAllWindows())
            if(!win.isDestroyed()&&!win.webContents.isDestroyed())
              win.webContents.send('codex_desktop:message-for-view',{type:'manager-record-invalidated',hostId:host,threadIds,deletedThreadIds,
                archivedThreadIds:archives.get(host)||[],unarchivedThreadIds:restores.get(host)||[]});
        } catch {counters.failures++;}
        if(threadIds.length){catalog?.getCoordinator(host)?.handleImportedThreads(threadIds);counters.listRefreshes++;}
      }
    } catch {
      // Failures affect only synchronization, never ordinary task execution.
      counters.failures++;
      for (const [id, entry] of pending) {
        entry.due = Date.now() + 3000;
        if (++entry.failures >= 5) pending.delete(id);
      }
    } finally {
      if (Date.now() - lastDiagnostic >= 2000) {
        lastDiagnostic = Date.now();
        try {
          const diagnostics = path.join(directory, 'status');
          await fs.mkdir(diagnostics, {recursive:true});
          // No prompts, titles, credentials or task contents in diagnostics.
          const full = path.join(diagnostics, `${process.pid}.json`);
          const temporary = full + '.' + generation + '.tmp';
          await fs.writeFile(temporary, JSON.stringify({version:2, pid:process.pid,
            updatedAt:new Date().toISOString(), ...globalThis.__codexRecordSync.status()}), 'utf8');
          await fs.rename(temporary, full);
        } catch { /* Diagnostic I/O never affects ordinary execution. */ }
      }
      running = false;
    }
  }
  globalThis.__codexRecordSync = {
    publish(id, host='local', kind='changed') {
      if(!uuid.test(id || '') || typeof host!=='string' || host.length>256 || !['changed','deleted','archived','unarchived'].includes(kind))return;
      if(profile && host==='local' && kind!=='deleted')return;
      const key=host+'\0'+id;
      if(kind==='deleted'){deletedKeys.add(key);pending.delete(key);}
      if(kind!=='deleted'&&deletedKeys.has(key))return;
      if(kind==='archived')archivedKeys.add(key);
      if(kind==='unarchived')archivedKeys.delete(key);
      if(kind!=='changed')visibility.set(key,kind);
      kind=visibility.get(key)||kind;
      while(visibility.size>4096)visibility.delete(visibility.keys().next().value);
      changed.delete(key);changed.set(key,[id,++sequence,host,kind]);dirty=true;
      while(changed.size>256)changed.delete(changed.keys().next().value);
    },
    observe(manager, method, params) {
      const id = params?.thread?.id || params?.threadId;
      if (!uuid.test(id || '')) return;
      const local = activity.get(manager);
      const turnId = params?.turn?.id || params?.turnId;
      if (local) {
        if (method === 'turn/started' || method === 'item/started' || method === 'item/agentMessage/delta')
          local.turns.set(id, turnId || null);
        if (method === 'turn/completed' && (!turnId || local.turns.get(id) === turnId)) local.turns.delete(id);
        if (method === 'thread/closed' || method === 'thread/deleted' ||
            (method === 'thread/status/changed' && params?.status?.type === 'idle')) local.turns.delete(id);
      }
      if (signals.has(method)) this.publish(id,manager.hostId,({'thread/deleted':'deleted','thread/archived':'archived','thread/unarchived':'unarchived'})[method]||'changed');
    },
    register(manager) {
      localManagers();
      if (manager.disposed || managers.has(manager)) return;
      managers.add(manager);
      // Shared plugin/skill revisions refresh through this manager's own
      // app-server request client; optional so isolated adapters still load.
      globalThis.__codexPluginSync?.register(manager);
      const local = { turns:new Map(), requests:new Map() };
      activity.set(manager, local);
      const client = manager.requestClient, send = client?.sendRequest;
      if (typeof send === 'function') client.sendRequest = async function(method, params, ...rest) {
        const id = params?.threadId;
        if(globalThis.__codexProfileResume)params=await globalThis.__codexProfileResume(manager,send,this,method,params);
        const track = uuid.test(id || '') && (method === 'turn/start' || method === 'turn/steer');
        if (track) local.requests.set(id, (local.requests.get(id) || 0) + 1);
        try { return await Reflect.apply(send, this, [method, params, ...rest]); }
        finally {
          if (track) {
            const count = local.requests.get(id) - 1;
            if (count) local.requests.set(id, count); else local.requests.delete(id);
          }
        }
      };
    },
    catalog(value) { catalog = value; },
    canApply(store) {
      const request = refreshing.get(store);
      return !request || (!deletedKeys.has(request.manager.hostId+'\0'+request.id) && !archivedKeys.has(request.manager.hostId+'\0'+request.id) && !active(request.manager, request.id));
    },
    tick,
    status() { return { ...counters, managers: localManagers().length, sources:seen.size, pending: pending.size,
      archived:archivedKeys.size, deleted:deletedKeys.size }; },
    stop() { stopped = true; clearInterval(timer); }
  };
  const timer = setInterval(tick, 400);
  timer.unref();
})();
