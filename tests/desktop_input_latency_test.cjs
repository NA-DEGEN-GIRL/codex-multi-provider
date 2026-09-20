const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('scripts/manager_core/desktop_renderer_record_sync.cjs','utf8');
let now=1000;
const observers=new Map(),events={};
class Observer {
  static supportedEntryTypes=['event','longtask'];
  constructor(callback){this.callback=callback;}
  observe(options){this.options=options;observers.set(options.type,this);}
  disconnect(){this.disconnected=true;}
  send(entries){this.callback({getEntries:()=>entries});}
}
const document={visibilityState:'visible',querySelector:()=>null,addEventListener(){}};
const ctx={PerformanceObserver:Observer,performance:{now:()=>now},Date:{now:()=>now},document,
  window:{addEventListener:(name,fn)=>events[name]=fn},setTimeout:()=>1,clearTimeout(){}};
vm.runInNewContext(source,ctx);
const health=()=>JSON.parse(JSON.stringify(ctx.__codexRendererRecordSync.inputHealth()));
assert.equal(observers.get('event').options.durationThreshold,16);
assert.equal(health().event_timing_supported,true);
const composer={closest:selector=>selector==='[data-codex-composer]'};
function entry(overrides={}){return {name:'keydown',target:composer,startTime:now,
  processingStart:now+80,duration:128,key:'PRIVATE_KEY',data:'PRIVATE_TEXT',...overrides};}
observers.get('event').send([entry(),entry({name:'click'}),entry({target:{closest:()=>false}})]);
assert.equal(health().slow_input_samples,1);
assert.equal(health().input_delay_max_ms,80);
assert.equal(health().input_duration_p95_ms,128);
observers.get('longtask').send([{startTime:now,duration:240,attribution:'PRIVATE_URL'}]);
assert.equal(health().long_task_max_ms,240);
assert(!JSON.stringify(health()).includes('PRIVATE'),'never retain keys, text, targets or attribution');
observers.get('event').send(Array.from({length:1000},()=>entry()));
assert.equal(health().slow_input_samples,256,'samples remain bounded during sustained typing');
document.visibilityState='hidden';observers.get('event').send([entry({duration:9999})]);
assert.equal(health().input_duration_max_ms,128,'hidden windows do not contaminate visible typing diagnostics');
now+=30001;assert.equal(health().slow_input_samples,0);assert.equal(health().long_task_samples,0);
events.pagehide();assert([...observers.values()].every(o=>o.disconnected));
const unsupported={...ctx,PerformanceObserver:undefined};vm.runInNewContext(source,unsupported);
assert.equal(unsupported.__codexRendererRecordSync.inputHealth().event_timing_supported,false);
console.log('PASS: passive compositor-input timings, 30-second bounded samples, hidden/unsupported fallback and no input contents');
