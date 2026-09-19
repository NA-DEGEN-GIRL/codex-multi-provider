// Shared change-file reader. Watches are hints, never the only source of truth.
// Metadata sweeps recover missed events; consumers retain their merge semantics.
(() => {
  if (globalThis.__codexSignalFiles) return;
  const nativeFs = require('node:fs'), fs = nativeFs.promises, path = require('node:path');
  const {Buffer} = require('node:buffer');
  const clients = new Set();
  const totals = {directoryScans:0, stats:0, reads:0, rejected:0, failures:0};
  function create(directory, {accept, maxFiles=256, maxBytes=4*1024*1024, createDirectory=true}) {
    const stamps = new Map(), dirty = new Map(), failures = new Map();
    let watcher, initialized=false, stopped=false, busy=false, full=true, forceAll=false, serial=0;
    let lastSweep=-Infinity, lastWatch=-Infinity;
    const mark = filename => {
      const name = filename?.toString();
      if (!name) { full=true; forceAll=true; serial++; return; }
      if (path.basename(name)!==name || !accept(name)) return;
      dirty.set(name, ++serial);
      while (dirty.size>maxFiles*2) { dirty.delete(dirty.keys().next().value); full=true; }
    };
    const close = () => {
      stopped=true; watcher?.close(); watcher=undefined;
      stamps.clear(); dirty.clear(); failures.clear(); clients.delete(client);
    };
    function watch(now) {
      if (watcher || now-lastWatch<2000 || typeof nativeFs.watch!=='function') return;
      lastWatch=now;
      try {
        const current=nativeFs.watch(directory, {persistent:false}, (_,name)=>mark(name));
        watcher=current;
        current.on?.('error', () => {
          current.close(); if(watcher===current)watcher=undefined;
          full=true;
        });
      } catch { /* Keep polling, including when the directory appears later. */ }
    }
    const client = {
      close,
      async scan(consume) {
        if(stopped||busy)return; busy=true;
        try {
          if(!initialized&&createDirectory){await fs.mkdir(directory,{recursive:true});initialized=true;}
          watch(Date.now());
          let names;
          const snapshot=serial, force=forceAll, sweep=full||!watcher||Date.now()-lastSweep>=2000;
          if(sweep){
            totals.directoryScans++;
            names=(await fs.readdir(directory)).filter(n=>path.basename(n)===n&&accept(n)).slice(0,maxFiles);
            const present=new Set(names);
            for(const name of stamps.keys())if(!present.has(name)){stamps.delete(name);failures.delete(name);}
            if(serial===snapshot){full=false;forceAll=false;}
            lastSweep=Date.now();
          }else names=[...dirty.keys()].slice(0,maxFiles);
          for(const name of names){
            if(stopped)return;
            const event=dirty.get(name), fullPath=path.join(directory,name);
            let stamp;
            try {
              totals.stats++;
              const info=await fs.stat(fullPath);
              if(stopped)return;
              stamp=[info.mtimeMs,info.ctimeMs,info.size,info.ino].join(':');
              if(!info.isFile()||info.size>maxBytes){totals.rejected++;}
              else if(force||event!==undefined||stamps.get(name)!==stamp){
                totals.reads++;
                const text=await fs.readFile(fullPath,'utf8');
                if(stopped)return;
                // The file may have grown after stat. Bound parsing as well.
                if(text.length>maxBytes||Buffer.byteLength(text,'utf8')>maxBytes){totals.rejected++;}
                else if(!stopped&&await consume(name,JSON.parse(text))===false)totals.rejected++;
              }
              stamps.delete(name);stamps.set(name,stamp);failures.delete(name);
              if(dirty.get(name)===event)dirty.delete(name);
            }catch(error){
              if(error.code==='ENOENT'){
                stamps.delete(name);failures.delete(name);
                if(dirty.get(name)===event)dirty.delete(name);
              }else{
                totals.failures++;
                const old=failures.get(name), count=old?.stamp===stamp?old.count+1:1;
                failures.set(name,{stamp,count});
                // Retry transient/partial reads, without spinning on an
                // unchanged corrupt writer forever. A watch/version resets it.
                if(stamp&&count>=3){stamps.set(name,stamp);if(dirty.get(name)===event)dirty.delete(name);}
                else if(dirty.get(name)===event)dirty.set(name,event??++serial);
              }
            }
          }
          while(stamps.size>maxFiles)stamps.delete(stamps.keys().next().value);
          while(failures.size>maxFiles)failures.delete(failures.keys().next().value);
        }catch(error){
          if(error.code==='ENOENT'){
            initialized=false;full=true;watcher?.close();watcher=undefined;
          }else totals.failures++;
          throw error;
        }finally{busy=false;}
      },
    };
    clients.add(client);return client;
  }
  globalThis.__codexSignalFiles={create,status:()=>({...totals,readers:clients.size}),
    stop(){for(const client of [...clients])client.close();}};
  try { require('electron').app?.once('will-quit',()=>globalThis.__codexSignalFiles.stop()); }
  catch { /* Standalone fixtures do not need Electron. */ }
})();
