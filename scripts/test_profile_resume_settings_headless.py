"""Cold-resume model/effort regression using only isolated loopback accounts."""
import json
import os
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4

from manager_core.runtime_build import resolve
from test_shared_editing_headless import Client, Fixture, ROOT


def adapt(client, params):
    """Run the shipped JavaScript adapter with actual native read responses."""
    config = client.rpc('config/read', {'includeLayers': False})
    record = client.rpc('thread/read', {'threadId': params['threadId'], 'includeTurns': False})
    node = shutil.which('node') or str(Path(os.environ['ProgramFiles']) / 'nodejs/node.exe')
    script = r'''
const fs=require('node:fs'),vm=require('node:vm');
const data=JSON.parse(fs.readFileSync(0,'utf8')),ctx={};vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx);
ctx.__codexProfileResume({threadStore:{threadsById:new Map()}},
  async method=>{if(method==='config/read')return data.config;
    if(method==='thread/read')return data.record;throw Error(method)},
  {},'thread/resume',data.params).then(result=>process.stdout.write(JSON.stringify(result)))
  .catch(error=>{console.error(error);process.exitCode=1});
'''
    output = subprocess.run([node, '-e', script, str(ROOT / 'scripts/manager_core/desktop_profile_resume.cjs')],
        input=json.dumps(dict(config=config, record=record, params=params)),
        text=True, encoding='utf8', capture_output=True, check=True, timeout=15)
    return json.loads(output.stdout)


def run():
    output = ROOT / 'work' / ('profile-resume-settings-' + uuid4().hex[:8])
    output.mkdir(parents=True)
    binary = Path(resolve(ROOT)['runtime'])
    fixture = Fixture()
    live = []
    report = dict(passed=False, real_model_calls=0, gui_used=False, cases={})
    try:
        for case in ('legacy_routing', 'same_provider', 'explicit_choice'):
            home = output / case
            def start():
                config = ('model="gpt-5.5"\nmodel_provider="fixture"\nmodel_reasoning_effort="xhigh"\n'
                    'cli_auth_credentials_store="ephemeral"\napproval_policy="never"\n'
                    '[features]\nshell_tool=false\n'
                    '[model_providers.fixture]\nname="Loopback fixture"\nbase_url="http://127.0.0.1:'
                    + str(fixture.server.server_port) + '/v1"\nenv_key="LOCAL_FIXTURE_TOKEN"\nwire_api="responses"\n')
                client = Client(binary, home, None, 'A', fixture.server.server_port, str(uuid4()), config_text=config)
                live.append(client)
                return client
            seed = start()
            created = seed.rpc('thread/start', dict(cwd=str(output), model='gpt-6-astra',
                config={'model_reasoning_effort': 'ultra'}))
            tid = created['thread']['id']
            seed.turn(tid, 'Reply with the loopback fixture response.')
            seed.close(); live.remove(seed)
            cold = start()
            params = dict(threadId=tid)
            if case == 'legacy_routing':
                params.update(modelProvider='fixture', config={'model_provider': 'fixture'})
            else:
                if case == 'explicit_choice':
                    params.update(model='gpt-5.6-sol', config={'model_reasoning_effort': 'low'})
                params = adapt(cold, params)
            resumed = cold.rpc('thread/resume', params)
            observed = (resumed['model'], resumed['reasoningEffort'])
            expected = {'legacy_routing': ('gpt-5.5', 'xhigh'), 'same_provider': ('gpt-6-astra', 'ultra'),
                        'explicit_choice': ('gpt-5.6-sol', 'low')}[case]
            assert observed == expected, (case, observed, expected)
            report['cases'][case] = dict(model=observed[0], effort=observed[1])
            cold.close(); live.remove(cold)
        assert len(fixture.calls) == 3
        # The runtime can map a UI effort label to its model's wire vocabulary;
        # the persistence contract is the native resume response checked above.
        report['wire_efforts'] = [call.get('reasoning', {}).get('effort') for call in fixture.calls]
        report.update(passed=True, loopback_calls=len(fixture.calls))
    finally:
        for client in live:
            client.close()
        fixture.close()
        (output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf8')
    print(json.dumps(report))


if __name__ == '__main__':
    run()
