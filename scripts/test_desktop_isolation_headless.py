"""Use bundled Node to test the pipe resolver from the actual copied ASAR.

No account files, model calls, installed-app pipes or user windows are touched.
Two processes act as thread owners with the same canonical ID; each client
must reach only its own profile's owner.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from uuid import uuid4

from desktop_launch import child_environment, find_app
from manager_core.desktop_bundle import _PIPE_VARIANTS, _entries, prepare, read_header


def main(root):
    original = find_app()
    app = prepare(root, original)
    assets = Path(app['executable']).parent
    with (assets / 'resources/app.asar').open('rb') as stream:
        header, base = read_header(stream)
        modules = []
        for name, item in _entries(header):
            if name.startswith('.vite/build/') and name.endswith('.js'):
                stream.seek(base + int(item['offset']))
                data = stream.read(item['size'])
                if any(replacement in data for replacement in _PIPE_VARIANTS.values()):
                    modules.append((name, data))
        assert len(modules) == 1
    name, data = modules[0]
    script = r'''
const fs=require('fs'),net=require('net'),vm=require('vm');
const source=fs.readFileSync(process.argv[2],'utf8');
// Use exact known function prefix, then evaluate only its Windows branch.
const start=source.indexOf('if(process.platform===`win32`){let p=process.env.CODEX_MANAGER_DESKTOP_PIPE;');
const end=source.indexOf(';}',start)+2;
if(start<0||end<start)throw Error('missing resolver');
const pipe=vm.runInNewContext('(function(){'+source.slice(start,end)+'})()', {process,i:require('path'),s:require('path')});
if(!pipe.endsWith(process.env.CODEX_MANAGER_DESKTOP_PIPE))throw Error('invalid pipe');
if(process.argv[3]==='server'){
 const server=net.createServer(s=>s.once('data',d=>s.end(JSON.stringify({profile:process.env.CODEX_MANAGER_DESKTOP_PIPE,thread:d.toString()}))));
 server.listen(pipe,()=>process.stdout.write('ready\n'));
 process.stdin.resume();process.stdin.on('end',()=>server.close());
}else{
 const socket=net.connect(pipe,()=>socket.write('same-canonical-thread'));
 let data='';socket.on('data',chunk=>data+=chunk);socket.on('end',()=>process.stdout.write(data));
 socket.on('error',()=>process.exit(2));
}
'''
    environment = child_environment('original')
    profiles = ['codex-manager-' + str(uuid4()) for _ in range(2)]
    processes = []
    checks = {}
    with tempfile.TemporaryDirectory(prefix='codex-pipe-fixture-') as temp:
        directory = Path(temp)
        runner = directory / 'fixture.cjs'; runner.write_text(script, encoding='utf-8')
        module = directory / 'actual-module.js'; module.write_bytes(data)
        # Never execute ChatGPT.exe: this packaged Owl runtime does not honor
        # ELECTRON_RUN_AS_NODE and could forward arguments to the user's app.
        environment.update(CODEX_HOME=str(directory/'home'), CODEX_ELECTRON_USER_DATA_PATH=str(directory/'ui'))
        node = assets / 'resources/cua_node/bin/node.exe'
        args = [str(node), str(runner), str(module)]
        try:
            for profile in profiles:
                env = dict(environment, CODEX_MANAGER_DESKTOP_PIPE=profile)
                process = subprocess.Popen(args+['server'], env=env, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace',
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                processes.append(process)
                # Server creation is synchronous and either prints ready or exits.
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(process.stdout.readline)
                    try: line = future.result(timeout=15)
                    except concurrent.futures.TimeoutError:
                        process.kill(); raise RuntimeError('fixture startup timed out')
                if line.strip() != 'ready':
                    raise RuntimeError('fixture startup failed: ' + line[:800] + process.stderr.read()[:800])
            for index, profile in enumerate(profiles):
                result = subprocess.run(args+['client'], env=dict(environment,CODEX_MANAGER_DESKTOP_PIPE=profile),
                    capture_output=True,text=True,timeout=10,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                result.check_returncode()
                response = json.loads(result.stdout)
                checks['profile_' + str(index) + '_routes_to_own_owner'] = (response['profile']==profile and response['thread']=='same-canonical-thread')
            for value in ('codex-ipc', ''):
                result = subprocess.run(args+['client'], env=dict(environment,CODEX_MANAGER_DESKTOP_PIPE=value),
                    capture_output=True,text=True,timeout=10,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                checks['reject_global' if value else 'reject_missing'] = result.returncode != 0
        finally:
            for process in processes:
                process.stdin.close()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill();process.wait(timeout=5)
                process.stdout.close();process.stderr.close()
    for file in ('ChatGPT.exe','chrome.dll'):
        def digest(path):
            with path.open('rb') as stream: return hashlib.file_digest(stream,'sha256').hexdigest()
        checks[file+'_unchanged'] = digest(assets/file)==digest(Path(original['executable']).parent/file)
    report = dict(verified=all(checks.values()), app_version=app['Version'], checks=checks,
                  scope='Bundled Node + resolver extracted from published ASAR; desktop app is NOT launched; no GUI, auth, or model turn', module=name)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['verified'] else 1


if __name__ == '__main__': sys.exit(main(Path(sys.argv[1]).resolve()))
