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
  // restored: unarchives applied here. mine: when this profile's own
  // connection last streamed a turn of a shared (SSH) task.
  const restoredKeys = new Set(), mine = new Map(), liveSent = new Map();
  const refreshing = new WeakMap(), activity = new WeakMap();
  const profile = process.env.CODEX_MANAGER_PROFILE_ID;
  const ownFile = (profile || '00000000-0000-4000-8000-000000000001') + (profile ? '.desktop.json' : '.json');
  const reader=()=>globalThis.__codexSignalFiles.create(directory,{maxFiles:64,maxBytes:32768,createDirectory:false,
    accept:f=>f!==ownFile&&f!==profile+'.json'&&/^[a-f0-9-]{36}(?:\.desktop)?\.json$/i.test(f)});
  let files=reader();
  const generation = require('node:crypto').randomUUID();
  const changed = new Map();
  const KINDS = ['changed','deleted','archived','unarchived'];
  const signals = new Set(['thread/started','thread/name/updated','thread/settings/updated',
    'thread/project/updated',
    'thread/archived','thread/unarchived','thread/deleted','turn/started','turn/completed',
    'item/started','item/completed','item/agentMessage/delta']);
  let sequence = 0, dirty = false;
  let catalog = null, running = false, stopped = false;
  let nextBackgroundRefresh = 0, wasForeground = null;
  const BACKGROUND_REFRESH_MS = 2000;
  // Peers flush every 400 ms while a turn streams. A change is read once after
  // QUIET_MS without newer signals, and at least every SETTLE_MAX_MS while it
  // keeps changing; meanwhile an open, visible transcript is refreshed by its
  // renderer at most every LIVE_MS. A peer's copy of a shared-host turn this
  // profile streamed itself within SELF_MS is not read back.
  const QUIET_MS = 1500, SETTLE_MAX_MS = 5000, LIVE_MS = 2000, SELF_MS = 3000, STALE_MS = 864e5, MISSING_RETRY_MS = 3000;
  const ORIGINAL = '00000000-0000-4000-8000-000000000001';
  const counters = { scans: 0, listRefreshes: 0, catalogUpdates: 0, summaryRefreshes: 0, historyRefreshes: 0, deferred: 0, unavailable: 0, failures: 0,
    retries: 0, selfSkipped: 0, staleWriters: 0, liveNotices: 0,
    hubPolls: 0, hubEvents: 0, hubResets: 0, hubCatchUps: 0, hubFallbacks: 0, hubPublished: 0, hubPublishBusy: 0, hubPublishDropped: 0 };
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
  const hasVisibleWindow = () => {
    try {
      return require('electron').BrowserWindow.getAllWindows().some(win =>
        !win.isDestroyed() && (typeof win.isVisible!=='function' || win.isVisible()));
    } catch { return true; } // Unknown adapters retain the foreground behavior.
  };
  // Only a shared (SSH) host echoes a task to every profile, and only turn/item
  // notifications show this profile's connection streamed that task itself.
  // Local tasks are never echoed: managed profiles do not publish them.
  const note = (host, id, method) => {
    if (host === 'local' || !/^(turn|item)\//.test(method)) return;
    const key = host+'\0'+id;mine.delete(key);mine.set(key,Date.now());
    while(mine.size>4096)mine.delete(mine.keys().next().value);
  };
  const selfObserved = (key, state) => (mine.get(key) ?? -Infinity) >= state.last - SELF_MS;
  // Startup replays retained changes only from current writers: the manager's
  // profile folders beside record-signals, trusted when they include this
  // profile, plus the original app's writer. Without that list an unknown
  // writer's file must be younger than STALE_MS. Writes seen during this
  // process always apply.
  let writers;
  async function listWriters() {
    try {
      const ids = (await fs.readdir(path.join(path.dirname(directory), 'profiles'))).map(String).filter(n => uuid.test(n)).map(n => n.toLowerCase());
      if (ids.length && (!profile || ids.includes(profile.toLowerCase()))) return new Set(ids);
    } catch { /* No trusted list: fall back to the file age. */ }
    return null;
  }
  async function currentWriter(file) {
    const id = file.slice(0, 36).toLowerCase();
    if (id === ORIGINAL || writers?.has(id)) return true;
    if (writers) return false;
    try { return Date.now() - (await fs.stat(path.join(directory, file))).mtimeMs < STALE_MS; }
    catch { return true; }
  }
  // Native handleImportedThreads re-reads each task (thread/read, source
  // thread_catalog), then applies it exactly like observeThread (verified in
  // 26.915/26.917: same converter, same store calls; threadsById holds the raw
  // Thread plus turns:[]). A summary this tick's read applied is that Thread,
  // so it goes to observeThread directly. Any other coordinator shape, a read
  // that was not applied, a pending local title or a retried read takes the
  // native path (one fresh read).
  const verified = new WeakMap();
  const nativeShape = coordinator => {
    if (!verified.has(coordinator)) {
      let ok = false;
      try {
        ok = /^observeThread\((\w+)\)\{this\.observeThreads\(\[\1\]\);let (\w+)=[\w$]+\(\1,this\.store\.hostId\);if\(\2==null\)\{this\.publishMutation\(this\.store\.applyAuthoritativeRemoval\(\1\.id\),\[\]\);return\}this\.publishMutation\(this\.store\.applyObservedEntry\(\2\),\[\2\]\)\}$/
            .test(Function.prototype.toString.call(coordinator.observeThread)) &&
          /(\w+)\.onAccepted\?\.\(this\),this\.publishMutation\(this\.store\.applyObservedEntry\(\1\.entry\),\[\1\.entry\]\)/
            .test(Function.prototype.toString.call(coordinator.refreshThread));
      } catch { /* Unknown coordinator: native path. */ }
      verified.set(coordinator, ok);
    }
    return verified.get(coordinator);
  };
  function importThreads(coordinator, host, rows) {
    if (!coordinator) return;
    const fallback = [];
    for (const [id, thread, fresh] of rows) {
      if (fresh && nativeShape(coordinator) && coordinator.syncEnabled === true && !coordinator.disposed && coordinator.store?.hostId === host &&
          thread?.id === id && Number.isFinite(thread.updatedAt) && thread.source != null) {
        try { coordinator.observeThread(thread); counters.catalogUpdates++; continue; } catch { counters.failures++; }
      }
      fallback.push(id);
    }
    if (fallback.length) { coordinator.handleImportedThreads(fallback); counters.listRefreshes++; }
  }
  // Changes retained before this process started are already durable. Read
  // each once so moves/renames made while closed still apply; repeated reads
  // only serve live writes that may still be settling. Every profile replays
  // every peer's backlog at startup, so this multiplies across the workspace.
  let backlog = true;
  // The valid [id, seq, host, kind] entries of one writer file, or null.
  const entries = data => [1,2].includes(data?.version) && typeof data.generation === 'string' &&
    Array.isArray(data.changes) && data.changes.length <= 256 ? data.changes.flatMap(change => {
      if (!Array.isArray(change) || ![2,4].includes(change.length)) return [];
      const [id, seq, host = 'local', kind = 'changed'] = change;
      return typeof host === 'string' && host.length <= 256 && KINDS.includes(kind) && uuid.test(id) &&
        Number.isSafeInteger(seq) ? [[id, seq, host, kind]] : [];
    }) : null;
  // Every peer report takes this path, from a writer file or from the hub.
  // old: durable before this read (startup backlog or a catch-up). seq: the
  // hub journal position a report came from (0 for a file entry).
  function admit(id, host, kind, now, old, seq = 0, quiet = QUIET_MS) {
    const key=host+'\0'+id;
    if(kind==='deleted')deletedKeys.add(key);
    if(kind!=='deleted'&&deletedKeys.has(key))return;
    if(kind==='archived'){archivedKeys.add(key);restoredKeys.delete(key);}
    // Writers repeat a task's last visibility on every later change, so an
    // unarchive already applied here is an ordinary change from then on.
    if(kind==='unarchived'){if(!archivedKeys.has(key)&&restoredKeys.has(key))kind='changed';else archivedKeys.delete(key);}
    if(kind==='changed'&&archivedKeys.has(key))return;
    while(archivedKeys.size>4096)archivedKeys.delete(archivedKeys.values().next().value);
    while(deletedKeys.size>4096)deletedKeys.delete(deletedKeys.values().next().value);
    const prev=pending.get(key);
    if(kind==='changed'&&prev?.kind==='unarchived'){prev.last=now;return;}
    // Live changes wait until their writers are quiet, but a task that keeps
    // changing is still read every SETTLE_MAX_MS. The backlog is already
    // durable and is read once, immediately. A durable copy of a change that
    // is still live here (a redelivery) keeps the live rules and retries.
    const first=prev?.kind===kind ? prev.first : now;
    old = prev ? prev.backlog && old : old;
    pending.set(key, { id, host, kind, deleted:kind==='deleted', backlog:old, first, last: now,
      due: kind==='changed'&&!old ? Math.min(now+quiet, first+SETTLE_MAX_MS) : now, failures: 0,
      seq: prev ? Math.min(prev.seq, seq) : seq });
  }
  // The pipeline never evicts a report it has not processed. When it is full,
  // only an entry for a host no manager here has served for PIN_MS gives way
  // (it no longer pins the saved cursor either); otherwise admission waits.
  const MAX_PENDING = 1024, WRITERS_MS = 60000;
  function room(key, now) {
    if (pending.size < MAX_PENDING || pending.has(key)) return true;
    const served = new Set(localManagers().map(m => m.hostId));
    for (const [other, s] of pending) if (!served.has(s.host) && now - s.last >= PIN_MS) { pending.delete(other); return true; }
    return false;
  }
  // A pass that stopped at a full pipeline leaves `seen` where it stopped;
  // the files are re-read once there is room again.
  let backpressure = false, listedAt = -Infinity;
  async function scan(relist = false) {
    // Profiles created since the last listing are current writers too.
    if (backlog && (relist || writers === undefined || Date.now() - listedAt >= WRITERS_MS)) { writers = await listWriters(); listedAt = Date.now(); }
    let cut = false;
    try { await files.scan(async (file,data)=>{
      const rows = entries(data);
      if (!rows) return false;
      const previous = seen.get(file), sequence = previous?.generation === data.generation ? previous.sequence : 0;
      // A writer that is no longer a profile keeps its file: never replay it.
      const stale = backlog && !previous && !await currentWriter(file);
      if (stale) counters.staleWriters++;
      let last = sequence;
      const now = Date.now();
      for (const [id, seq, host, kind] of rows.sort((a, b) => a[1] - b[1])) {
        if (seq <= sequence) continue;
        if (!stale && !room(host + '\0' + id, now)) { cut = true; break; }
        last = seq;
        if (!stale) admit(id, host, kind, now, backlog);
      }
      seen.delete(file);seen.set(file, { generation: data.generation, sequence: last });
      while(seen.size>256)seen.delete(seen.keys().next().value);
    }); } catch(error) { if(error.code!=='ENOENT')throw error; }
    if (cut) backpressure = true; else backlog = false;
    return !cut;
  }
  // Record hub (manager service): one multiplexed pipe connection long-polls
  // what peers changed since this profile's cursor, so no profile rescans
  // every peer file. The own file is still written (the hub and older readers
  // ingest it). Any failure falls back to the files at once, and the hub is
  // retried every HUB_RETRY_MS with backoff. Deadlines are checked by the tick:
  // no timers, and never a synchronous wait on the event loop.
  const HUB_WAIT_MS = 20000, HUB_GRACE_MS = 10000, HUB_CONNECT_MS = 3000, HUB_RETRY_MS = 30000, HUB_RETRY_MAX_MS = 300000,
    HUB_QUIET_MS = QUIET_MS - 750, REBASE_MS = 30000, REBASE_LAG_MS = 15000, SAVE_MS = 2000, PIN_MS = 600000,
    PUBLISH_MS = 400, PUBLISH_WAIT_MS = 5000, MAX_LINE = 8 * 1024 * 1024;
  const { Buffer } = require('node:buffer');
  // Exactly the name instances.py validates and passes (never a path).
  const pipe = /^CodexControlCenter\.service\.[0-9A-F]{32}$/.test(process.env.CODEX_MANAGER_RECORDS_PIPE || '') ? process.env.CODEX_MANAGER_RECORDS_PIPE : null;
  const hubProfile = pipe && uuid.test(profile || '') ? profile.toLowerCase() : null;
  const hubFile = hubProfile && path.join(directory, 'status', hubProfile + '.hub.json');
  // starting: first answer pending, files wait (at most HUB_CONNECT_MS);
  // on: the hub serves; connecting/fallback: files serve. resets/caught: file
  // catch-ups requested by hub resets and completed. gen: bumped by every
  // reset and failure, so work begun before one never lands after it.
  const hub = { state: hubProfile ? 'starting' : 'off', startedAt: Date.now(), socket: null, calls: new Map(), serial: 0,
    epoch: null, cursor: null, polling: false, draining: false, inbox: [], resets: 0, caught: 0, catchUpAt: 0, catchUpTries: 0,
    gen: 0, refresh: false, failures: 0, retryAt: 0, candidate: null, candidateAt: 0, rebaseAt: 0,
    savedEpoch: null, savedCursor: -1, savedAt: -Infinity, outbox: new Map(), sending: false, publishAt: 0, busyDelay: 0, publishOff: false };
  function hubFail(reason, socket = hub.socket) {
    if (socket !== hub.socket || !['starting','connecting','on'].includes(hub.state)) return;
    hub.socket = null; socket?.destroy();
    for (const call of hub.calls.values()) call.reject(Error(reason));
    hub.calls.clear(); hub.polling = hub.sending = false; hub.outbox.clear();
    // Received events stay queued. An unconfirmed snapshot is not a baseline:
    // the files are re-read in full against the last confirmed one.
    hub.candidate = null; hub.refresh = true; hub.gen++; hub.state = 'fallback'; hub.reason = reason; counters.hubFallbacks++;
    hub.retryAt = Date.now() + Math.min(HUB_RETRY_MS * 2 ** hub.failures++, HUB_RETRY_MAX_MS);
  }
  function hubCall(command, args, wait) {
    const id = 'r' + ++hub.serial, socket = hub.socket;
    return new Promise((resolve, reject) => {
      hub.calls.set(id, { resolve, reject, deadline: Date.now() + wait });
      socket.write(JSON.stringify({ id, command, args }) + '\n');
    });
  }
  function hubConnect() {
    if (stopped) return;
    if (hub.state !== 'starting') hub.state = 'connecting';
    // What the first poll (and its continuation pages) returns changed while
    // this connection was away: durable, so it is read once like the backlog.
    hub.draining = true;
    const socket = hub.socket = require('node:net').connect('\\\\.\\pipe\\' + pipe);
    let parts = [], size = 0;
    socket.on('data', chunk => {
      let start = 0, end;
      while ((end = chunk.indexOf(10, start)) !== -1) {
        if (size + end - start > MAX_LINE) { parts = []; return hubFail('oversized', socket); }
        parts.push(chunk.subarray(start, end)); start = end + 1;
        const line = Buffer.concat(parts).toString('utf8'); parts = []; size = 0;
        let value;
        try { value = JSON.parse(line); } catch { return hubFail('protocol', socket); }
        const call = hub.calls.get(value?.id);
        if (call) { hub.calls.delete(value.id); call.resolve(value); }
        if (hub.socket !== socket) return;
      }
      if (start < chunk.length) {
        parts.push(chunk.subarray(start)); size += chunk.length - start;
        if (size > MAX_LINE) { parts = []; hubFail('oversized', socket); }
      }
    });
    socket.on('error', () => hubFail('error', socket));
    socket.on('close', () => hubFail('closed', socket));
    hubPoll();
  }
  function hubPoll() {
    if (hub.polling || !hub.socket || hub.inbox.length >= 2048) return;
    const socket = hub.socket, first = hub.state !== 'on';
    // No hosts filter: like a file entry, a report for a host this profile
    // does not serve yet stays pending until its manager registers.
    const args = { profile: hubProfile, wait_ms: first ? 0 : HUB_WAIT_MS };
    if (hub.epoch != null) Object.assign(args, { epoch: hub.epoch, cursor: hub.cursor });
    hub.polling = true; counters.hubPolls++;
    hubCall('records.poll', args, first ? HUB_CONNECT_MS : HUB_WAIT_MS + HUB_GRACE_MS).then(reply => {
      if (hub.socket !== socket) return;
      hub.polling = false;
      const r = reply?.ok === true ? reply.result : null;
      if (!r || r.closing === true || typeof r.epoch !== 'string' || !r.epoch || r.epoch.length > 64 ||
          !Number.isSafeInteger(r.cursor) || r.cursor < 0 || !Array.isArray(r.events) || r.events.length > 512 ||
          (r.reset !== true && r.epoch !== hub.epoch))
        return hubFail(r?.closing === true ? 'closing' : String(reply?.error?.code || 'protocol').slice(0, 64), socket);
      const now = Date.now(), old = hub.draining;
      hub.draining = old && r.reset !== true && r.more === true;
      if (r.reset === true) {
        // Unknown epoch or compacted history: a file catch-up (the backlog
        // path), then only what the hub delivers after this cursor. Positions
        // from the old epoch mean nothing now: queued reports pin until read.
        for (const e of hub.inbox) e.seq = 0;
        for (const s of pending.values()) s.seq = 0;
        Object.assign(hub, { epoch: r.epoch, cursor: r.cursor, candidate: null, refresh: true, catchUpAt: 0, catchUpTries: 0 });
        hub.resets++; hub.gen++; counters.hubResets++;
      } else {
        for (const e of r.events)
          if (e && typeof e.host === 'string' && e.host.length <= 256 && uuid.test(e.id || '') && KINDS.includes(e.kind) &&
              Number.isSafeInteger(e.seq) && e.seq > hub.cursor && e.seq <= r.cursor) {
            hub.inbox.push({ id: e.id, host: e.host, kind: e.kind, seq: e.seq, old }); counters.hubEvents++;
          }
        hub.cursor = r.cursor;
        // Everything the files held REBASE_LAG_MS before this complete reply
        // has been delivered: that snapshot is where a fallback resumes.
        if (r.more !== true && hub.candidate && now >= hub.candidateAt + REBASE_LAG_MS) {
          for (const [file, value] of hub.candidate) {
            const known = seen.get(file);
            if (known?.generation === value.generation && known.sequence >= value.sequence) continue;
            seen.delete(file); seen.set(file, value);
          }
          while (seen.size > 256) seen.delete(seen.keys().next().value);
          hub.candidate = null; backlog = false;
        }
      }
      // A skipped startup backlog needs a baseline at once, for a later fallback.
      if (hub.state !== 'on') { hub.state = 'on'; hub.rebaseAt = backlog && hub.caught === hub.resets ? now : now + REBASE_MS; }
      hub.failures = 0;
      hubPoll();
    }, () => {});
  }
  // This profile's own reports, sent directly as well as through its file.
  function hubReport(host, id, kind) {
    // The hub refuses a whole batch over one host it would not accept.
    if (hub.state !== 'on' || hub.publishOff || !host || Buffer.byteLength(host) > 256 || /[\u0000-\u001f\u007f-\u009f]/.test(host)) return;
    const key = host + '\0' + id;
    hub.outbox.delete(key); hub.outbox.set(key, { host, id, kind });
    while (hub.outbox.size > 1024) hub.outbox.delete(hub.outbox.keys().next().value);
  }
  function hubFlush(now) {
    if (hub.state !== 'on' || hub.publishOff || hub.sending || !hub.outbox.size || now < hub.publishAt) return;
    const socket = hub.socket, batch = [...hub.outbox.values()].slice(0, 256);
    for (const e of batch) hub.outbox.delete(e.host + '\0' + e.id);
    hub.sending = true; hub.publishAt = now + PUBLISH_MS;
    hubCall('records.publish', { profile: hubProfile, events: batch }, PUBLISH_WAIT_MS).then(reply => {
      if (hub.socket !== socket) return;
      hub.sending = false;
      // closing: nothing was taken; the file still carries it.
      if (reply?.ok === true) { if (reply.result?.closing !== true) { counters.hubPublished += batch.length; hub.busyDelay = 0; } return; }
      const code = reply?.error?.code;
      if (code === 'records_busy') {
        hub.busyDelay = Math.min(Math.max(hub.busyDelay * 2, 1000), 30000);
        hub.publishAt = Date.now() + hub.busyDelay; counters.hubPublishBusy++;
        for (const e of batch) { const key = e.host + '\0' + e.id; if (!hub.outbox.has(key)) hub.outbox.set(key, e); }
        return;
      }
      // A refused batch is dropped; the file still carries it.
      if (code === 'invalid_request') { counters.hubPublishDropped++; return; }
      // profile_mismatch or any other refusal: the file path alone remains.
      hub.publishOff = true; hub.outbox.clear();
    }, () => {});
  }
  function hubTick() {
    if (hub.state === 'off' || stopped) return;
    const now = Date.now();
    // A request past its deadline means the service stopped answering.
    for (const call of hub.calls.values()) if (now > call.deadline) { hubFail('timeout'); break; }
    if (hub.state === 'starting' && !hub.socket && now >= hub.startedAt + HUB_CONNECT_MS) hubFail('timeout');
    if (hub.state === 'fallback' && now >= hub.retryAt) hubConnect();
    hubFlush(now);
  }
  // The persisted cursor never passes a report that is still queued here: a
  // restarted profile resumes where processing, not delivery, stopped. A file
  // catch-up requested or in progress (seq 0) is never skipped by a restart.
  async function hubSave(now) {
    if (!hubFile || hub.epoch == null || hub.caught !== hub.resets || now - hub.savedAt < SAVE_MS) return;
    let safe = hub.cursor;
    for (const e of hub.inbox) safe = Math.min(safe, e.seq - 1);
    for (const s of pending.values()) if (now - s.last < PIN_MS) safe = Math.min(safe, s.seq - 1);
    if (safe < 0 || (hub.epoch === hub.savedEpoch && safe <= hub.savedCursor)) return;
    const epoch = hub.epoch, temporary = hubFile + '.' + generation + '.tmp';
    hub.savedAt = now;
    await fs.mkdir(path.dirname(hubFile), {recursive:true});
    await fs.writeFile(temporary, JSON.stringify({ version: 1, epoch, cursor: safe }), 'utf8');
    await fs.rename(temporary, hubFile);
    hub.savedEpoch = epoch; hub.savedCursor = safe;
  }
  async function hubLoad() {
    try {
      const saved = JSON.parse(await fs.readFile(hubFile, 'utf8'));
      if (saved?.version === 1 && typeof saved.epoch === 'string' && saved.epoch && saved.epoch.length <= 64 &&
          Number.isSafeInteger(saved.cursor) && saved.cursor >= 0)
        Object.assign(hub, { epoch: saved.epoch, cursor: saved.cursor, savedEpoch: saved.epoch, savedCursor: saved.cursor });
    } catch { /* No usable cursor: the hub resets and the backlog runs. */ }
  }
  // While the hub serves, peer files are only re-baselined (no task reads).
  // A snapshot becomes the fallback baseline once a later complete poll shows
  // that the hub delivered what it held. It is built aside and published only
  // when complete, so a reply arriving mid-scan never confirms fresher state.
  async function rebase(now) {
    const found = new Map(hub.candidate || []), gen = hub.gen;
    try { await files.scan(async (file, data) => {
      const rows = entries(data);
      if (!rows) return false;
      const previous = found.get(file) || seen.get(file);
      let last = previous?.generation === data.generation ? previous.sequence : 0;
      for (const [, seq] of rows) last = Math.max(last, seq);
      found.delete(file); found.set(file, { generation: data.generation, sequence: last });
      while (found.size > 256) found.delete(found.keys().next().value);
    }); } catch (error) { if (error.code !== 'ENOENT') counters.failures++; }
    if (hub.gen === gen && hub.state === 'on') { hub.candidate = found; hub.candidateAt = now; }
  }
  // A reset's catch-up repeats (with backoff) until one pass reads every file
  // without a failure: a file mid-replace (EPERM/EBUSY) or held by a scanner
  // must not be skipped, since the hub never delivers its older entries.
  async function catchUp(now) {
    const target = hub.resets, failures = globalThis.__codexSignalFiles.status().failures;
    backlog = true;
    // Stopped at a full pipeline: it resumes where it stopped once there is room.
    if (!await scan(true)) return;
    if (globalThis.__codexSignalFiles.status().failures === failures) {
      // A reset that arrived during this pass needs a pass of its own.
      if (hub.resets === target) hub.caught = target; else hub.refresh = true;
      hub.catchUpTries = 0; counters.hubCatchUps++;
    } else {
      // A fresh reader retries every file, including one it gave up on.
      hub.refresh = true; hub.catchUpAt = now + Math.min(400 * 2 ** hub.catchUpTries++, 30000);
    }
  }
  // Hub reports enter the pipeline only while it has room (see room()). The
  // rest wait in the inbox, which pins the saved cursor and pauses polling.
  // They go first: the files wait while the inbox is not empty.
  function admitInbox(now) {
    let taken = 0;
    for (const e of hub.inbox) {
      if (!room(e.host + '\0' + e.id, now)) break;
      admit(e.id, e.host, e.kind, now, e.old, e.seq, HUB_QUIET_MS); taken++;
    }
    hub.inbox.splice(0, taken);
  }
  // Peer changes: from the hub while it serves this profile, else the files.
  async function receive() {
    try {
      const now = Date.now();
      admitInbox(now);
      if (backpressure && pending.size < MAX_PENDING && !hub.inbox.length) { backpressure = false; hub.refresh = true; }
      if (hub.refresh) { hub.refresh = false; files.close(); files = reader(); }
      if (hub.caught !== hub.resets) { if (now >= hub.catchUpAt && !backpressure) await catchUp(now); }
      else if (hub.state === 'on') { if (now >= hub.rebaseAt) { hub.rebaseAt = now + REBASE_MS; await rebase(now); } }
      else if (hub.state !== 'starting' && !backpressure) await scan();
    } finally { hubPoll(); }
  }
  async function tick() {
    hubTick();
    if (running || stopped) return;
    running = true;
    // Only the reports this tick tried to apply are charged for its failure;
    // a failed listing, catch-up or own write never ages a queued report out.
    let attempted = [];
    try {
      counters.scans++;
      // Managed runtimes already publish through their proxy. The original
      // desktop needs the same notifications to travel in the other direction.
      if (dirty) {
        const own = [...changed.values()], serial = sequence;
        await fs.mkdir(directory, {recursive:true});
        const full = path.join(directory, ownFile), temporary = full + '.' + generation + '.tmp';
        await fs.writeFile(temporary, JSON.stringify({version:2,generation,changes:own}), 'utf8');
        await fs.rename(temporary, full);
        if (sequence === serial) dirty = false;
      }
      try { await receive(); } catch { counters.failures++; }
      const locals = localManagers();
      if (!locals.length) return;
      // Preloaded accounts retain the latest invalidation for each task. They
      // do not need to reread the catalog on every streaming delta. Selection
      // bypasses this gate on the next tick; removals/restores bypass it always.
      const foreground = hasVisibleWindow(), now = Date.now();
      const refreshBackground = foreground || now >= nextBackgroundRefresh;
      // Selecting a hidden profile reads the latest revision of every pending
      // change at once, including a task that is still streaming.
      if (foreground && wasForeground === false)
        for (const s of pending.values()) if (s.kind === 'changed') s.due = Math.min(s.due, now);
      wasForeground = foreground;
      const served = host => locals.some(m => m.hostId === host);
      const add = (map, host, value) => {if(!map.has(host))map.set(host,[]);map.get(host).push(value);};
      // A task that keeps changing (a peer's streaming turn) is read here only
      // every SETTLE_MAX_MS. Its renderer may refresh an open, visible
      // transcript at most every LIVE_MS; the notice alone costs no read.
      const lives = new Map();
      if (foreground) for (const [key, s] of pending) {
        const sent = liveSent.get(key) ?? -Infinity;
        if (s.kind !== 'changed' || s.backlog || s.due <= now || now - s.first < LIVE_MS || s.last <= sent ||
            now - sent < LIVE_MS || selfObserved(key, s) || !served(s.host)) continue;
        liveSent.delete(key); liveSent.set(key, now); counters.liveNotices++; add(lives, s.host, s.id);
      }
      while (liveSent.size > 1024) liveSent.delete(liveSent.keys().next().value);
      const visible = key => {const e=pending.get(key);return locals.some(m => m.hostId===e.host && m.threadStore.isConversationActive(e.id));};
      const ids = [...pending].filter(([, state]) => state.due <= now &&
          (state.kind!=='changed' || refreshBackground) && served(state.host))
        .sort(([a], [b]) => Number(visible(b)) - Number(visible(a)))
        .slice(0, 8).map(([id]) => id);
      attempted = ids;
      if (!foreground && ids.some(key=>pending.get(key).kind==='changed'))
        nextBackgroundRefresh = now + BACKGROUND_REFRESH_MS;
      const imports = new Map(), deletions = new Map(), archives = new Map(), restores = new Map();
      // Unpersisted/ephemeral IDs are not imports. Native import retries are
      // unbounded, so only publish a summary after its history read succeeds.
      for (const key of ids) {
        const current = pending.get(key), {id, host} = current;
        const targets=locals.filter(m=>m.hostId===host);
        if(!targets.length)continue;
        if(current.deleted){
          for(const m of targets)m.handleThreadDeletion([id]);
          add(deletions,host,id);
          pending.delete(key);continue;
        }
        if(current.kind==='archived'){
          for(const m of targets)m.handleThreadArchived(id);
          add(archives,host,id);
          pending.delete(key);continue;
        }
        if(current.kind==='unarchived'){
          for(const m of targets)m.handleThreadUnarchived(id);
          add(restores,host,id);
          restoredKeys.add(key);while(restoredKeys.size>4096)restoredKeys.delete(restoredKeys.values().next().value);
          // Visibility applies now; the summary read waits for the change to settle.
          current.kind='changed';current.due=current.backlog?now:current.last+QUIET_MS;
          continue;
        }
        // This profile's own connection streamed this shared-host turn; a
        // peer's republished copy adds nothing.
        if (selfObserved(key, current)) { pending.delete(key); counters.selfSkipped++; continue; }
        let deferred = false, failed = false, available = false, notFound = false, advanced = false, fresh = false, summary;
        for (const manager of targets) {
          if (active(manager, id)) { deferred = true; counters.deferred++; continue; }
          refreshing.set(manager.threadStore, { manager, id });
          try {
            const before = manager.threadStore.threadsById.get(id);
            manager.threadStore.backgroundThreadLookups?.delete(id);
            manager.threadStore.threadReadStates?.delete(id);
            await manager.threadStore.hydrateThreads([id], {
              addToRecentConversations: true,
              // The main process owns the catalog, not the visible transcript.
              // Read/validate its summary once. The renderer fetches turns only
              // when visible and not typing; duplicating that work here builds
              // large transcript graphs in every preloaded profile.
              includeTurns: false,
              retainHistoryPagination: true, notifyAnyCallbacks: true, throwOnReadError: true
            });
            if (active(manager, id)) { deferred = true; continue; }
            const read = manager.threadStore.threadsById.get(id);
            if (!read) { failed = notFound = true; counters.unavailable++; continue; }
            if (read.name) manager.threadStore.applyThreadTitleUpdate(id, read.name);
            available = true; summary = read;
            // The native store replaces the entry when it applies a read. The same
            // object means this read was discarded (e.g. a new hydration generation).
            fresh = read !== before && !manager.threadStore.pendingThreadTitlesById?.has?.(id);
            // Signals carry no timestamp. A read returning the summary already
            // held here may precede the writer's durable record.
            if (!before || !(read.updatedAt <= before.updatedAt) || read.name !== before.name) advanced = true;
            counters.summaryRefreshes++;
          } catch (error) {
            failed = true;
            if (/thread not loaded|thread.*not found/i.test(String(error?.message || error))) { notFound = true; counters.unavailable++; }
            else counters.failures++;
          } finally { refreshing.delete(manager.threadStore); }
        }
        const settled = Date.now(), removed = deletedKeys.has(key) || archivedKeys.has(key);
        // After a retry the catalog takes a fresh native read, not this summary.
        const row = [id, summary, fresh && !current.retried];
        if (pending.get(key) !== current) { if (available && !removed) add(imports, host, row); continue; }
        if (available && !advanced && !deferred && !current.retried && !current.backlog) {
          current.retried = true; current.due = settled + QUIET_MS; counters.retries++; continue;
        }
        // A removal observed during the read must not re-add the task.
        if (available && !removed) add(imports, host, row);
        if (notFound && !available) {
          // A live signal can precede the writer's durable record: one later
          // retry. The backlog is durable, so its not-found is final. Either
          // way a newer signal for the task reads it again.
          if (!current.backlog && !current.missed) { current.missed = true; current.due = settled + MISSING_RETRY_MS; counters.retries++; }
          else pending.delete(key);
          continue;
        }
        if (failed && !notFound) { current.due = settled + 3000; if (++current.failures >= 3) pending.delete(key); }
        else if (deferred) current.due = settled + 1500;
        else pending.delete(key);
      }
      for(const host of new Set([...imports.keys(),...deletions.keys(),...archives.keys(),...restores.keys(),...lives.keys()])) {
        const rows=imports.get(host)||[], threadIds=rows.map(([id])=>id), deletedThreadIds=deletions.get(host)||[];
        try {
          for(const win of require('electron').BrowserWindow.getAllWindows())
            if(!win.isDestroyed()&&!win.webContents.isDestroyed())
              win.webContents.send('codex_desktop:message-for-view',{type:'manager-record-invalidated',hostId:host,threadIds,deletedThreadIds,
                archivedThreadIds:archives.get(host)||[],unarchivedThreadIds:restores.get(host)||[],liveThreadIds:lives.get(host)||[]});
        } catch {counters.failures++;}
        if(rows.length)importThreads(catalog?.getCoordinator(host),host,rows);
      }
    } catch {
      // Failures affect only synchronization, never ordinary task execution.
      counters.failures++;
      for (const key of attempted) {
        const entry = pending.get(key);
        if (!entry) continue;
        entry.due = Date.now() + 3000;
        if (++entry.failures >= 5) pending.delete(key);
      }
    } finally {
      try { await hubSave(Date.now()); } catch { /* A later tick saves again; a stale cursor only repeats reads. */ }
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
      if(!uuid.test(id || '') || typeof host!=='string' || host.length>256 || !KINDS.includes(kind))return;
      if(profile && host==='local' && kind!=='deleted')return;
      const key=host+'\0'+id;
      if(kind==='deleted'){deletedKeys.add(key);pending.delete(key);}
      if(kind!=='deleted'&&deletedKeys.has(key))return;
      if(kind==='archived'){archivedKeys.add(key);restoredKeys.delete(key);}
      if(kind==='unarchived'){archivedKeys.delete(key);restoredKeys.add(key);}
      if(kind!=='changed')visibility.set(key,kind);
      kind=visibility.get(key)||kind;
      while(visibility.size>4096)visibility.delete(visibility.keys().next().value);
      changed.delete(key);changed.set(key,[id,++sequence,host,kind]);dirty=true;
      while(changed.size>256)changed.delete(changed.keys().next().value);
      hubReport(host,id,kind);
    },
    observe(manager, method, params) {
      const id = params?.thread?.id || params?.threadId;
      if (!uuid.test(id || '')) return;
      const local = activity.get(manager);
      const turnId = params?.turn?.id || params?.turnId;
      if (local) {
        if (method === 'turn/started' || method === 'item/started' || method === 'item/agentMessage/delta')
          local.turns.set(id, turnId || local.turns.get(id) || null);
        if (method === 'turn/completed' && (!turnId || local.turns.get(id) === turnId)) local.turns.delete(id);
        if (method === 'thread/closed' || method === 'thread/deleted' ||
            (method === 'thread/status/changed' && params?.status?.type === 'idle')) local.turns.delete(id);
      }
      note(manager.hostId, id, method);
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
    status() { return { ...counters, signalFiles:globalThis.__codexSignalFiles.status(), managers: localManagers().length, sources:seen.size, pending: pending.size,
      archived:archivedKeys.size, deleted:deletedKeys.size, writers:writers?writers.size:null,
      hub:hub.state, hubReason:hub.reason ?? null, hubPublish:hub.publishOff?'stopped':'on' }; },
    stop() {
      stopped = true; clearInterval(timer); files.close();
      const socket = hub.socket; hub.socket = null; socket?.destroy();
      for (const call of hub.calls.values()) call.reject(Error('stopped'));
      hub.calls.clear();
    }
  };
  const timer = setInterval(tick, 400);
  timer.unref();
  if (hub.state === 'starting') hubLoad().then(() => { if (hub.state === 'starting') hubConnect(); });
})();
