const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('scripts/manager_core/desktop_profile_resume.cjs','utf8');
async function check(renderer){
  const id='11111111-1111-4111-8111-111111111111';
  const context={require,process:{env:{CODEX_RECORD_SIGNALS:'unused'}},setInterval:()=>({unref(){}}),setTimeout:()=>1,clearTimeout(){},
    document:{addEventListener(){}},window:{addEventListener(){}}};
  vm.createContext(context);vm.runInContext(source,context);
  if(!renderer)vm.runInContext(fs.readFileSync('scripts/manager_core/desktop_signal_files.cjs','utf8'),context);
  vm.runInContext(fs.readFileSync('scripts/manager_core/desktop_'+(renderer?'renderer_':'')+'record_sync.cjs','utf8'),context);
  let provider='openai',model='gpt-6-astra',sourceProvider='cc_deepseek',writes=[];
  const m={hostId:'local',threadStore:{threadsById:new Map()},requestClient:{async sendRequest(method,params){
    if(method==='config/read')return{config:{model_provider:provider,model,model_reasoning_effort:'high'}};
    if(method==='thread/read')return{thread:{modelProvider:sourceProvider}};
    writes.push([method,params]);return params;
  }}};
  context[renderer?'__codexRendererRecordSync':'__codexRecordSync'].register(m);
  for(const method of ['thread/resume','thread/fork']){
    const original={threadId:id,model:'deepseek-flash',config:{model_provider:'cc_deepseek',model:'deepseek-flash'}};
    const p=await m.requestClient.sendRequest(method,original);
    assert.equal(p.modelProvider,'openai');assert.equal(p.model,'gpt-6-astra');assert.equal(p.config.model,'gpt-6-astra');
    assert.equal(p.config.model_reasoning_effort,'high');assert.equal(original.config.model_provider,'cc_deepseek');
  }
  sourceProvider='openai';const same=await m.requestClient.sendRequest('thread/resume',{threadId:id,model:'gpt-other',config:{model_reasoning_effort:'low'}});
  assert.equal(same.model,'gpt-other');assert.equal(same.config.model_reasoning_effort,'low');
  provider='cc_second';model='second-api';
  const other=await m.requestClient.sendRequest('thread/resume',{threadId:id,model:'gpt-other'});
  assert.equal(other.modelProvider,'cc_second');assert.equal(other.model,'second-api');
  m.hostId='remote-ssh-discovered:fixture';sourceProvider='cc_missing';provider='openai';model='gpt-6-astra';
  const ssh=await m.requestClient.sendRequest('thread/resume',{threadId:id});assert.equal(ssh.modelProvider,'openai');
  const turn={threadId:id,model:'selected-gpt',effort:'low'};
  assert.equal(await m.requestClient.sendRequest('turn/start',turn),turn);
  assert(writes.every(([method])=>['thread/resume','thread/fork','turn/start'].includes(method)));
}
(async()=>{await check(false);await check(true);console.log('PASS: real main/renderer registrations route cross-provider local/SSH resumes and forks, preserving same-provider choices and caller objects');})().catch(e=>{console.error(e);process.exitCode=1});
