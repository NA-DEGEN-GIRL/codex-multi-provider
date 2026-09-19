const assert=require('node:assert/strict'), fs=require('node:fs'), vm=require('node:vm'), path=require('node:path');
const source=fs.readFileSync('scripts/manager_core/desktop_signal_files.cjs','utf8');
function fixture({watching=true,maxFiles=256,maxBytes=1024}={}){
  const f={now:0,files:new Map(),calls:{mkdir:0,lists:0,stats:0,reads:0,closed:0},events:[],missing:false};
  const io={
    async mkdir(){f.calls.mkdir++;f.missing=false;},
    async readdir(){f.calls.lists++;if(f.directoryError)throw f.directoryError;if(f.missing)throw Object.assign(Error(),{code:'ENOENT'});return [...f.files.keys()];},
    async stat(name){f.calls.stats++;const file=f.files.get(path.basename(name));if(!file)throw Object.assign(Error(),{code:'ENOENT'});
      return {size:file.size??file.text.length,mtimeMs:file.stamp,ctimeMs:file.stamp,ino:1,isFile:()=>true};},
    async readFile(name){f.calls.reads++;const file=f.files.get(path.basename(name));if(f.read)return f.read(name,file);return file.text;},
  };
  const native={promises:io};
  if(watching)native.watch=(_,opts,fn)=>{assert.equal(opts.persistent,false);f.watch=fn;return{close(){f.calls.closed++;},on(_,handler){f.watchError=handler;}}};
  const ctx={Date:{now:()=>f.now},require:name=>name==='node:fs'?native:name==='electron'?{app:{once(_,cb){f.quit=cb}}}:require(name)};
  vm.createContext(ctx);vm.runInContext(source,ctx);
  f.service=ctx.__codexSignalFiles;
  f.reader=f.service.create('fixture',{accept:name=>name.endsWith('.json'),maxFiles,maxBytes});
  f.put=(name,value,stamp=1)=>f.files.set(name,{text:typeof value==='string'?value:JSON.stringify(value),stamp});
  f.scan=()=>f.reader.scan((name,data)=>f.events.push([name,data]));
  return f;
}
(async()=>{
  const f=fixture();for(let i=0;i<8;i++)f.put(i+'.json',{value:i});
  await f.scan();assert.equal(f.events.length,8);
  for(let n=1;n<=60;n++){f.now=n*100;await f.scan();}
  assert.equal(f.calls.mkdir,1,'idle ticks do not repeatedly create/check the directory');
  assert.equal(f.calls.reads,8,'unchanged declarations are parsed once');
  assert.equal(f.calls.lists,4,'idle directory sweep is bounded to two seconds');
  assert.equal(f.calls.stats,32);
  console.log('MEASURE: 8 files / 61 polls: reads 488 -> '+f.calls.reads+', directory scans 61 -> '+f.calls.lists+' (simulated idle workload)');
  const lists=f.calls.lists;
  f.put('2.json',{value:'changed'},2);f.watch('rename','2.json');await f.scan();
  assert.equal(f.calls.lists,lists,'a known changed file does not enumerate every writer');
  assert.equal(f.events.at(-1)[1].value,'changed');
  const reads=f.calls.reads;f.watch('change','2.json.tmp');f.watch('change','../2.json');await f.scan();
  assert.equal(f.calls.reads,reads,'temporary/unrelated/escaping names do not wake readers');
  f.put('2.json',{value:'missed'},3);f.now+=2000;await f.scan();assert.equal(f.events.at(-1)[1].value,'missed');
  f.put('2.json',{value:'atomic'},3);f.watch('rename','2.json');await f.scan();assert.equal(f.events.at(-1)[1].value,'atomic');
  f.put('2.json',{value:'unknown-name'},3);f.watch('rename',null);await f.scan();assert.equal(f.events.findLast(e=>e[0]==='2.json')[1].value,'unknown-name');
  let finish;
  f.read=async(name,file)=>name.endsWith('2.json')?new Promise(resolve=>{finish=()=>resolve(file.text);}):file.text;
  f.watch('change','2.json');const pending=f.scan();
  while(!finish)await new Promise(r=>setImmediate(r));
  f.put('2.json',{value:'during-read'},4);f.watch('change','2.json');finish();await pending;f.read=null;
  await f.scan();assert.equal(f.events.at(-1)[1].value,'during-read','a watch arriving during I/O survives completion');
  f.files.delete('2.json');f.watch('rename','2.json');await f.scan();
  f.put('2.json',{value:'recreated'},4);f.watch('rename','2.json');await f.scan();assert.equal(f.events.at(-1)[1].value,'recreated');
  f.watchError(Error('watch lost'));f.put('2.json',{value:'watch-failed'},5);await f.scan();assert.equal(f.events.at(-1)[1].value,'watch-failed');
  f.quit();assert.equal(f.service.status().readers,0);const stopped=f.calls.stats;await f.scan();assert.equal(f.calls.stats,stopped);

  const corrupt=fixture();corrupt.put('bad.json','{');corrupt.put('good.json',{value:1});
  for(let n=0;n<5;n++)await corrupt.scan();
  assert.equal(corrupt.events.length,1,'bad peer does not block a valid peer');
  assert.equal(corrupt.calls.reads,4,'one good read plus three bounded corrupt-file attempts');
  corrupt.put('bad.json',{fixed:true});corrupt.watch('change','bad.json');await corrupt.scan();
  assert.equal(corrupt.events.at(-1)[1].fixed,true);
  corrupt.directoryError=Object.assign(Error('blocked'),{code:'EACCES'});corrupt.now=2000;
  await assert.rejects(corrupt.scan(),/blocked/,'failed full scan must not let callers seed stale local declarations');
  corrupt.directoryError=null;corrupt.missing=true;
  await assert.rejects(corrupt.scan(),e=>e.code==='ENOENT');await corrupt.scan();assert.equal(corrupt.calls.mkdir,2);
  corrupt.reader.close();

  const noWatch=fixture({watching:false});noWatch.put('one.json',{version:1});
  await noWatch.scan();await noWatch.scan();assert.equal(noWatch.calls.reads,1);
  noWatch.put('one.json',{version:2},2);await noWatch.scan();assert.equal(noWatch.events.at(-1)[1].version,2);noWatch.reader.close();
  const bounded=fixture({maxFiles:2,maxBytes:64});bounded.put('big.json','x'.repeat(65));bounded.put('race.json','x');bounded.put('overflow.json',{unexpected:true});
  bounded.read=async()=>'{"large":"'+'x'.repeat(65)+'"}';await bounded.scan();assert.equal(bounded.events.length,0);
  assert.equal(bounded.calls.stats,2);assert.equal(bounded.calls.reads,1);bounded.reader.close();
  const unicode=fixture({maxBytes:64});unicode.put('text.json',{value:'가'.repeat(30)});
  await unicode.scan();assert.equal(unicode.events.length,0,'UTF-8 bytes, not JS string length, bound parsing');
  assert.equal(unicode.service.status().rejected,1);unicode.reader.close();
  const stoppedRead=fixture();stoppedRead.put('one.json',{value:1});let finishStop;
  stoppedRead.read=async()=>new Promise(resolve=>finishStop=resolve);const read=stoppedRead.scan();
  while(!finishStop)await new Promise(r=>setImmediate(r));
  stoppedRead.reader.close();finishStop('{"value":1}');await read;assert.equal(stoppedRead.events.length,0);
  console.log('PASS: change-only I/O, fallback scans, atomic replacement, concurrent writes, corruption, limits and lifecycle');
})().catch(error=>{console.error(error);process.exitCode=1});
