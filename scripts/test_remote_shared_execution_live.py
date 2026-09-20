"""Linux smoke test: isolated SSH helpers and synthetic records, no real account/API.

Run with --runtime pointing at an installed managed Linux binary directory and
--helpers pointing at the current remote_helpers directory. Only temporary homes
and a loopback model fixture are used; no existing daemon is stopped or changed.
"""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import shutil
import sqlite3
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
from uuid import UUID, uuid4, uuid5


class Model(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length', 0)))
        item = dict(id='msg_fixture', type='message', role='assistant', status='completed',
                    content=[dict(type='output_text', text='fixture reply', annotations=[])])
        events = [dict(type='response.created', response=dict(id='resp_fixture', status='in_progress')),
                  dict(type='response.output_item.done', output_index=0, item=item),
                  dict(type='response.completed', response=dict(id='resp_fixture', status='completed',
                       output=[item], usage=dict(input_tokens=8, output_tokens=3, total_tokens=11)))]
        body = ''.join('data: ' + json.dumps(e) + '\n\n' for e in events).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Client:
    def __init__(self, args, env, cwd):
        self.process = subprocess.Popen(args, env=env, cwd=cwd, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        text=True, encoding='utf-8')
        self.messages = queue.Queue()
        self.sequence = 0
        def read():
            for line in self.process.stdout:
                self.messages.put(json.loads(line))
            self.messages.put(None)
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()
        self.call('initialize', dict(clientInfo=dict(name='shared-record-fixture', version='1'),
                                     capabilities=dict(experimentalApi=True)))
        self.send(dict(method='initialized'))

    def send(self, value):
        self.process.stdin.write(json.dumps(value) + '\n')
        self.process.stdin.flush()

    def receive(self, predicate, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = self.messages.get(timeout=max(.01, deadline - time.monotonic()))
            if value is None:
                raise RuntimeError('fixture runtime exited')
            if predicate(value):
                return value
        raise TimeoutError('fixture response timeout')

    def call(self, method, params, *, error=False):
        self.sequence += 1
        self.send(dict(id=self.sequence, method=method, params=params))
        value = self.receive(lambda v: v.get('id') == self.sequence)
        if error:
            return value.get('error', {})
        if 'error' in value:
            raise RuntimeError(method + ': ' + str(value['error']))
        return value['result']

    def turn(self, thread):
        self.call('turn/start', dict(threadId=thread, input=[dict(type='text', text='fixture request')]))
        done = self.receive(lambda v: v.get('method') == 'turn/completed')
        assert done['params']['turn']['status'] == 'completed', done

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=10)
        self.reader.join(timeout=2)
        self.process.stdout.close()


def run(runtime, helpers):
    server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    clients = []
    try:
        with tempfile.TemporaryDirectory(prefix='codex-shared-record-fixture-') as directory:
            root = Path(directory).resolve()
            home = root / 'user'
            source = home / '.codex'
            source.mkdir(parents=True)
            workspace = root / 'workspace'
            workspace.mkdir()
            config = ('model="fixture"\nmodel_provider="fixture"\napproval_policy="never"\n'
                      'sandbox_mode="danger-full-access"\n[model_providers.fixture]\nname="Fixture"\n'
                      f'base_url="http://127.0.0.1:{server.server_port}/v1"\nwire_api="responses"\n'
                      'requires_openai_auth=false\n')
            (source / 'config.toml').write_text(config)
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith(('CODEX_', 'OPENAI_', 'XDG_'))}
            env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / '.config'), CODEX_HOME=str(source))
            seed = Client([str(runtime / 'codex'), 'app-server'], env, workspace)
            clients.append(seed)
            thread = seed.call('thread/start', dict(cwd=str(workspace)))['thread']['id']
            seed.turn(thread)
            seed.close()
            clients.remove(seed)
            # Any consumption of the source credential would fail parsing. Only
            # record data, not source auth/config, belongs to the selected worker.
            (source / 'auth.json').write_text('foreign-credential-sentinel')
            source_config = (source / 'config.toml').read_bytes()
            base = root / 'manager'
            # Old account setups copied the same canonical task into separate
            # homes. Keep such a copy to prove that an explicit saved link pins
            # its own source, instead of choosing a random duplicate.
            duplicate_profile = base / 'profiles' / str(uuid4())
            duplicate = duplicate_profile / 'codex'
            shutil.copytree(source, duplicate)
            (duplicate / 'managed-source.json').write_text(json.dumps(dict(
                host_id='local', store_id='manager:' + duplicate_profile.name)))
            for database in duplicate.glob('state_*.sqlite'):
                with sqlite3.connect(database) as db:
                    db.execute('UPDATE threads SET rollout_path=replace(rollout_path,?,?)',
                               (str(source), str(duplicate)))
            original_copy = {str(p.relative_to(duplicate)): p.read_bytes()
                             for p in (duplicate / 'sessions').rglob('*.jsonl')}
            test_runtime = base / 'runtime' / 'fixture'
            test_runtime.mkdir(parents=True)
            for name in ('codex', 'codex-code-mode-host'):
                os.link(runtime / name, test_runtime / name)
            revision = 'b' * 64
            sid = 'legacy:' + hashlib.sha256(str(source).encode()).hexdigest()
            projection = str(uuid5(UUID('14a0f21b-b529-45fc-bd9e-b07637424fa3'),
                                   'local\0' + sid + '\0' + thread))

            def worker(read_only=False):
                profile = base / 'profiles' / str(uuid4())
                definition = profile / 'definitions' / revision
                definition.mkdir(parents=True)
                (definition / 'config.toml').write_text(config)
                (profile / 'credentials').mkdir()
                (profile / 'credentials' / (revision + '.json')).write_text('{}')
                (profile / 'definitions' / (revision + '.json')).write_text(json.dumps(dict(
                    revision=revision, profile_id=profile.name, runtime=str(test_runtime),
                    definition=str(definition), managed_sources=True, source_catalog=True,
                    mixed_source_catalog=True)))
                code = 'import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);import launch;'
                if read_only:
                    code += 'launch.shared_execution_environment=lambda *args:{};'
                code += 'launch.run(Path(sys.argv[2]),sys.argv[3],["app-server"])'
                client = Client([sys.executable, '-c', code, str(helpers), str(profile), revision], env, workspace)
                clients.append(client)
                return client

            old = worker(read_only=True)
            old.call('thread/resume', dict(threadId=projection))
            failure = old.call('turn/start', dict(threadId=projection,
                               input=[dict(type='text', text='fixture request')]), error=True)
            assert 'read-only record catalog' in failure.get('message', ''), failure
            old.close()
            clients.remove(old)
            first = worker()
            inspected = first.call('thread/read', dict(threadId=projection, includeTurns=False))
            assert inspected['thread']['id'] == projection
            assert not (base / 'shared-record-routes.json').exists(), 'background reads must not choose a source'
            resumed = first.call('thread/resume', dict(threadId=projection))
            assert resumed['thread']['id'] == thread
            assert json.loads((base / 'shared-record-routes.json').read_text())[thread] == sid
            first.turn(thread)
            # Keep the first actor alive: opening a second profile must use a
            # separate writer identity while appending to the same original task.
            second = worker()
            assert second.call('thread/resume', dict(threadId=projection))['thread']['id'] == thread
            second.turn(thread)
            history = second.call('thread/read', dict(threadId=thread, includeTurns=True))
            assert len(history['thread']['turns']) == 3, len(history['thread']['turns'])
            assert (source / 'auth.json').read_text() == 'foreign-credential-sentinel'
            assert (source / 'config.toml').read_bytes() == source_config
            assert original_copy == {str(p.relative_to(duplicate)): p.read_bytes()
                                     for p in (duplicate / 'sessions').rglob('*.jsonl')}
            print(json.dumps(dict(old_readonly_error_reproduced=True, saved_projection_resumed=True,
                                  two_profiles_completed_turns=True, canonical_turns=3,
                                  duplicate_source_preserved=True, explicit_source_selection=True,
                                  source_credentials_and_config_preserved=True)))
            for client in clients[:]:
                client.close()
                clients.remove(client)
    finally:
        for client in clients:
            client.close()
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--helpers', type=Path, required=True)
    args = parser.parse_args()
    run(args.runtime.resolve(strict=True), args.helpers.resolve(strict=True))
