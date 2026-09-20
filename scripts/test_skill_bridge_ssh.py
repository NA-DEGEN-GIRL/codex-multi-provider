"""Explicit live SSH smoke: local skill calls, input upload, audio import and verified return.

Run only against a user-authorized host. No model generation or paid API is used.
Installs the managed projections and retains a uniquely named remote temp folder.
"""
import argparse
import json
from pathlib import Path
import shlex
import sys
from uuid import uuid4

from manager_core.personal_skills import PersonalSkills
from manager_core.remote import RemoteManager
from manager_core.skill_bridge import SkillBridge
from manager_core.store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--host', required=True)
    args = parser.parse_args()
    root = args.root.resolve(); original = Store(root)
    class ScopedStore:
        directory = original.directory
        def read(self):
            state = original.read()
            for profile in state['profiles']:
                profile['remote_bindings'] = [b for b in profile.get('remote_bindings', []) if b['alias'] == args.host]
            return state
    store = ScopedStore()
    remote = RemoteManager(root)
    binding = next(b for p in store.read()['profiles'] for b in p.get('remote_bindings', []))
    bridge = SkillBridge(root, store, PersonalSkills(original), remote)
    report_path = root / 'work' / ('skill-bridge-smoke-' + uuid4().hex[:8] + '.json')
    report = dict(passed=False, paid_calls=0, model_inference=False)
    try:
        bridge.configure(['3d-assets', 'game-audio'])
        bridge.reconcile()
        report['hosts'] = bridge.status
        assert bridge.status[args.host]['status'] == 'ready', bridge.status
        observed = remote.inspect(args.host)
        cli_path = observed.get('cli_path')
        assert cli_path, 'Native Codex CLI is required for the discovery smoke.'
        code = 'NATIVE_CLI = ' + json.dumps(cli_path) + '\n' + r'''
import hashlib,json,math,os,pathlib,queue,struct,subprocess,sys,tempfile,threading,wave
from uuid import uuid4
home=pathlib.Path.home()
client_path=home/'.codex/workspace-skill-bridge/client.py'
sys.path.insert(0,str(client_path.parent))
from client import Client
client=Client()
scratch=pathlib.Path(tempfile.mkdtemp(prefix='codex-skill-bridge-smoke-'))
receipt={'temporary_directory':str(scratch),'checks':[]}
names={r['name'] for r in client.call('catalog')['skills']}
assert {'3d-assets','game-audio'} <= names
receipt['checks'].append('both Windows skills discoverable')
environment={k:v for k,v in os.environ.items() if not k.startswith('CODEX_') and k not in ('OPENAI_API_KEY','OPENAI_BASE_URL')}
environment['CODEX_HOME']=str(scratch/'isolated-codex-home')
pathlib.Path(environment['CODEX_HOME']).mkdir()
native_errors=(scratch/'native-stderr.log').open('w')
native=subprocess.Popen([NATIVE_CLI,'app-server'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
    stderr=native_errors,text=True,env=environment,cwd=scratch,start_new_session=True)
responses=queue.Queue()
def receive():
    for line in native.stdout:
        try:responses.put(json.loads(line))
        except ValueError:pass
threading.Thread(target=receive,daemon=True).start()
def rpc(number,method,params):
    native.stdin.write(json.dumps({'id':number,'method':method,'params':params})+'\n');native.stdin.flush()
    for _ in range(100):
        try:message=responses.get(timeout=45)
        except queue.Empty:
            raise AssertionError({'method':method,'exit':native.poll(),'stderr':(scratch/'native-stderr.log').read_text()[-4000:]})
        if message.get('id')==number:
            assert 'error' not in message,message.get('error')
            return message['result']
    raise AssertionError('native reply missing')
try:
    rpc(1,'initialize',{'clientInfo':{'name':'skill-bridge-fixture','version':'1'},'capabilities':{'experimentalApi':True}})
    native.stdin.write(json.dumps({'method':'initialized'})+'\n');native.stdin.flush()
    listed=rpc(2,'skills/list',{'cwds':[str(scratch)],'forceReload':True})
    found={skill['name'] for row in listed['data'] for skill in row['skills']}
    assert {'3d-assets','game-audio'}<=found,found
    receipt['checks'].append('native Linux Codex discovers both projected skills')
finally:
    native.stdin.close()
    try:native.wait(timeout=5)
    except subprocess.TimeoutExpired:native.terminate();native.wait(timeout=5)
    native_errors.close()
for skill,wrapper in [('3d-assets','assetctl.py'),('game-audio','audioctl.py')]:
    result=subprocess.run([sys.executable,str(home/'.agents/skills'/skill/'scripts'/wrapper),'doctor'],capture_output=True,text=True,timeout=100)
    assert result.returncode==0,(skill,result.stderr)
    doctor=json.loads(result.stdout)
    assert isinstance(doctor,dict)
    assert ':\\' in json.dumps(doctor) or ':/' in json.dumps(doctor),(skill,'not Windows runtime')
    receipt['checks'].append(skill+' Linux wrapper executed Windows doctor')
assert 'Blender' in client.call('read',skill='3d-assets',path='runtime:/docs/BLENDER.md')['text']
receipt['checks'].append('canonical Windows documentation readable')
source=scratch/'bridge sample.wav'
with wave.open(str(source),'wb') as out:
    out.setnchannels(1);out.setsampwidth(2);out.setframerate(48000)
    out.writeframes(b''.join(struct.pack('<h',int(3000*math.sin(2*math.pi*440*i/48000))) for i in range(12000)))
uploaded=client.upload(source)
client.fetch('game-audio',uploaded['path'],scratch/'roundtrip.wav')
assert source.read_bytes()==(scratch/'roundtrip.wav').read_bytes()
receipt['checks'].append('SSH input upload and SHA256-verified byte-identical return')
request=scratch/'request.json'
request.write_text(json.dumps({'name':'bridge-smoke-'+uuid4().hex[:10],'source':uploaded['path'],
    'kind':'sfx','provider':'supplied','model':'synthetic-transport-fixture','transport':'supplied',
    'notes':'Connection test only, not generated game audio or listening approval.'}))
spec=client.upload(request)
result=subprocess.run([sys.executable,str(client_path),'run','game-audio','--wait','120','--','import',spec['path']],capture_output=True,text=True,timeout=140)
assert result.returncode==0,result.stderr+'\n'+result.stdout
imported=json.loads(result.stdout)
receipt['audio_import']=imported
receipt['checks'].append('Linux request executed actual Windows audio import without inference')
def paths(value):
    if isinstance(value,dict):
        for child in value.values():yield from paths(child)
    elif isinstance(value,list):
        for child in value:yield from paths(child)
    elif isinstance(value,str):yield value
assert imported['status']=='completed'
revision=imported['revision'].replace('\\','/')
manifest=client.fetch('game-audio',imported['manifest'],scratch/'manifest.json')
client.fetch('game-audio',revision+'/take-001/master.wav',scratch/'imported-master.wav')
with wave.open(str(scratch/'imported-master.wav'),'rb') as audio:
    assert audio.getnframes()>0 and audio.getframerate()==48000
receipt['checks'].append('processed Windows WAV and manifest delivered into remote temporary project')
(scratch/'receipt.json').write_text(json.dumps(receipt,indent=2))
print(json.dumps(receipt))
'''
        result = remote._run(args.host, 'exec ' + shlex.quote(binding['remote_python']) + ' -c ' + shlex.quote(code), timeout=240)
        if result.returncode:
            raise RuntimeError(result.stderr.decode('utf-8', 'replace')[-6000:])
        report.update(passed=True, result=json.loads(result.stdout))
        print(json.dumps(dict(passed=True, checks=report['result']['checks'], report=str(report_path)), ensure_ascii=False))
    finally:
        bridge.shutdown()
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
