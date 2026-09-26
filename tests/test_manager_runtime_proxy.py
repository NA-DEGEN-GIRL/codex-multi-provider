import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from uuid import uuid4

SCRIPT_ROOT = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPT_ROOT))
from manager_core.runtime_proxy import read_frames, runtime_environment


class ProxyTests(unittest.TestCase):
    def test_slow_frontend_reader_does_not_block_runtime_input_or_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / 'input-received'
            runtime = root / 'runtime.py'
            runtime.write_text('''import sys,json
from pathlib import Path
for line in sys.stdin:
 m=json.loads(line)
 if m['id']==1:
  print(json.dumps({'id':1,'result':{'payload':'x'*(2*1024*1024)}}),flush=True)
 else:
  Path(sys.argv[1]).write_text('received')
  print(json.dumps({'id':m['id'],'result':{}}),flush=True)
''')
            bootstrap = root / 'bootstrap.py'
            bootstrap.write_text('''import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from manager_core.runtime_proxy import proxy
raise SystemExit(proxy(Path(sys.executable),[sys.argv[2],sys.argv[3]],Path(sys.argv[4]),sys.argv[5],
 {'CODEX_MANAGER_REAL_RUNTIME':sys.executable,'CODEX_HOME':str(Path(sys.argv[4]).parent)}))
''')
            snapshot = root / 'state.json'
            environment = {k:v for k,v in os.environ.items() if not k.upper().startswith('CODEX_')}
            process = subprocess.Popen([sys.executable,str(bootstrap),str(SCRIPT_ROOT),str(runtime),
                str(marker),str(snapshot),str(uuid4())],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,env=environment,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:
                process.stdin.write(b'{"id":1,"method":"initialize"}\n'); process.stdin.flush()
                # Leave stdout unread until the real OS pipe is full. The runtime
                # has already flushed 2 MiB to the proxy before reading request 2.
                time.sleep(.3)
                process.stdin.write(b'{"id":2,"method":"account/read"}\n'); process.stdin.flush()
                deadline = time.monotonic()+6
                while not marker.exists() and time.monotonic()<deadline: time.sleep(.02)
                self.assertTrue(marker.exists(), 'a full output pipe blocked the opposite direction')
                process.stdin.close(); process.stdin=None
                output, error = process.communicate(timeout=8)
                self.assertEqual(process.returncode,0,error)
                self.assertEqual([json.loads(line)['id'] for line in output.splitlines()],[1,2])
            finally:
                if process.poll() is None: process.kill(); process.wait(timeout=5)
                for stream in (process.stdin,process.stdout,process.stderr):
                    if stream: stream.close()

    def test_status_file_sharing_error_does_not_taint_runtime_protocol(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / 'runtime.py'
            runtime.write_text("import sys,json\nfor line in sys.stdin:\n m=json.loads(line)\n if 'id' in m: print(json.dumps({'id':m['id'],'result':{}}),flush=True)\n")
            snapshot = root / 'status.json'
            marker = root / 'publication-failed'
            bootstrap = root / 'bootstrap.py'
            bootstrap.write_text('''import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from manager_core.runtime_proxy import proxy
real_replace=os.replace
calls=0
def replace(source,target):
 global calls
 if str(target)==sys.argv[3]:
  calls+=1
  if calls in (1,2):
   Path(sys.argv[5]).write_text('fixture status sharing violation')
   raise PermissionError('fixture reader temporarily denies replace')
 return real_replace(source,target)
os.replace=replace
raise SystemExit(proxy(Path(sys.executable),[sys.argv[2]],Path(sys.argv[3]),sys.argv[4],
 {'CODEX_MANAGER_REAL_RUNTIME':sys.executable, 'CODEX_HOME':str(Path(sys.argv[3]).parent)}))
''', encoding='utf-8')
            environment = {k: v for k, v in os.environ.items() if not k.upper().startswith('CODEX_')}
            process = subprocess.Popen([sys.executable, str(bootstrap), str(SCRIPT_ROOT), str(runtime),
                str(snapshot), str(uuid4()), str(marker)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=environment, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                process.stdin.write(b'{"id":1,"method":"initialize","params":{}}\n')
                process.stdin.flush()
                deadline = time.monotonic() + 12
                published = None
                while time.monotonic() < deadline:
                    if marker.exists() and snapshot.exists() and snapshot.stat().st_mtime > marker.stat().st_mtime:
                        try: published = json.loads(snapshot.read_text())
                        except (OSError, ValueError): continue
                        break
                    time.sleep(.05)
                self.assertIsNotNone(published, 'status publication did not recover')
                self.assertTrue(published['initialized'])
                self.assertTrue(published['stream_complete'], published['stream_incomplete_reasons'])
                self.assertEqual(published['stream_incomplete_reasons'], [])
            finally:
                process.stdin.close()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
                process.stdout.close(); process.stderr.close()

    def test_launcher_metadata_never_reaches_runtime_tools(self):
        environment = runtime_environment({'PATH': 'path', 'CODEX_CLI_PATH': 'wrapper',
            'CODEX_MANAGER_REAL_RUNTIME': 'real', 'CODEX_MANAGER_AUTH_SOURCE': 'private-source',
            'CODEX_MANAGER_PROFILE_ID': 'profile', 'CODEX_MANAGER_RECORD_CATALOG': 'catalog',
            'CODEX_MANAGER_SHARED_CATALOG': 'shared-catalog',
            'CODEX_EXTERNAL_PROVIDER_KEY': 'runtime-needs-this'})
        self.assertEqual(environment['CODEX_CLI_PATH'], 'real')
        self.assertNotIn('CODEX_MANAGER_AUTH_SOURCE', environment)
        self.assertNotIn('CODEX_MANAGER_PROFILE_ID', environment)
        self.assertEqual(environment['CODEX_MANAGER_RECORD_CATALOG'], 'catalog')
        self.assertEqual(environment['CODEX_MANAGER_SHARED_CATALOG'], 'shared-catalog')
        self.assertEqual(environment['CODEX_EXTERNAL_PROVIDER_KEY'], 'runtime-needs-this')

    def test_runtime_temp_replaces_every_inherited_temp_spelling(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = os.path.join(directory, 'codex-manager', 'profile')
            environment = runtime_environment({'TEMP': 'C:/big', 'Tmp': 'C:/big', 'CODEX_MANAGER_REAL_RUNTIME': 'real',
                                               'CODEX_MANAGER_RUNTIME_TEMP': temp})
            self.assertTrue(os.path.isdir(temp))
            self.assertEqual({name: value for name, value in environment.items() if name.upper() in ('TEMP', 'TMP')},
                             {'TEMP': temp, 'TMP': temp})
            self.assertNotIn('CODEX_MANAGER_RUNTIME_TEMP', environment)

    def test_runtime_keeps_inherited_temp_without_a_usable_profile_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            blocker = os.path.join(directory, 'file')
            Path(blocker).write_text('x')
            for value in (None, os.path.join(blocker, 'temp')):
                with self.subTest(value=value):
                    source = {'TEMP': 'C:/big', 'CODEX_MANAGER_REAL_RUNTIME': 'real'}
                    if value:
                        source['CODEX_MANAGER_RUNTIME_TEMP'] = value
                    self.assertEqual(runtime_environment(source)['TEMP'], 'C:/big')

    def test_frame_limit_checked_during_read(self):
        with self.assertRaises(ValueError):
            list(read_frames(io.BytesIO(b'x' * 100_000), limit=1024))
        self.assertEqual(list(read_frames(io.BytesIO(b'{"id":1}\n{"id":2}\n'))),
                         [b'{"id":1}\n', b'{"id":2}\n'])

    def test_real_subprocess_stdio_roundtrip_and_sanitized_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / 'fake-runtime.py'
            fake.write_text('''import sys,json
for line in sys.stdin:
    message=json.loads(line)
    if message.get('method')=='initialize':
        print(json.dumps({'id':message['id'],'result':{'userAgent':'fixture','capabilities':message['params']['capabilities']}}),flush=True)
        print(json.dumps({'method':'process/exited','params':{'processHandle':'finished'}}),flush=True)
    elif 'id' in message:
        print(json.dumps({'id':message['id'],'result':{'secret':'SENSITIVE-RPC-TEXT'}}),flush=True)
''', encoding='utf-8')
            snapshot = root / 'observer.json'
            profile_id = str(uuid4())
            bootstrap = root / 'bootstrap.py'
            bootstrap.write_text('''import sys,os
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from manager_core.runtime_proxy import proxy
raise SystemExit(proxy(Path(sys.executable),[sys.argv[2],'app-server'],Path(sys.argv[3]),sys.argv[4],
    {**os.environ,'CODEX_MANAGER_REAL_RUNTIME':sys.executable}))
''', encoding='utf-8')
            messages = [{'id': 1, 'method': 'initialize', 'params': {'capabilities': {
                            'optOutNotificationMethods': ['process/exited']}}},
                        {'method': 'initialized'}, {'id': 2, 'method': 'account/read', 'params': {}}]
            response = subprocess.run([sys.executable, str(bootstrap), str(SCRIPT_ROOT), str(fake),
                                       str(snapshot), profile_id],
                                      input=''.join(json.dumps(message) + '\n' for message in messages),
                                      text=True, encoding='utf-8', capture_output=True, timeout=15,
                                      env={key: value for key, value in os.environ.items()
                                           if key != 'CODEX_MANAGER_AUTH_SOURCE'})
            self.assertEqual(response.returncode, 0, response.stderr)
            replies = [json.loads(line) for line in response.stdout.splitlines()]
            self.assertEqual([reply['id'] for reply in replies], [1, 2])
            self.assertEqual(replies[0]['result']['capabilities']['optOutNotificationMethods'], [])
            self.assertTrue(replies[0]['result']['capabilities']['experimentalApi'])
            self.assertIn('SENSITIVE-RPC-TEXT', response.stdout)
            saved = snapshot.read_text(encoding='utf-8')
            self.assertNotIn('SENSITIVE-RPC-TEXT', saved)
            data = json.loads(saved)
            self.assertFalse(data['connected'])
            self.assertFalse(data['safe_to_restart'])
            self.assertEqual(data['profile_id'], profile_id)

    def test_native_identity_gate_rejects_execution_before_runtime_receives_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / 'runtime.py'
            fake.write_text("import sys,json\nfor line in sys.stdin:\n m=json.loads(line)\n if 'id' in m: print(json.dumps({'id':m['id'],'result':{'received':m['method']}}),flush=True)\n")
            bootstrap = root / 'bootstrap.py'
            bootstrap.write_text("import sys,os\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1])\nfrom manager_core.runtime_proxy import proxy\nraise SystemExit(proxy(Path(sys.executable),[sys.argv[2]],Path(sys.argv[3]),sys.argv[4],dict(os.environ)))\n")
            environment = {key: value for key, value in os.environ.items() if not key.upper().startswith('CODEX_')}
            environment.update(CODEX_HOME=str(root), CODEX_MANAGER_REAL_RUNTIME=sys.executable,
                               CODEX_MANAGER_EXPECTED_ACCOUNT_FINGERPRINT='missing-native-identity')
            methods = ['initialize', 'turn/start', 'account/read', 'account/login/start']
            response = subprocess.run([sys.executable, str(bootstrap), str(SCRIPT_ROOT), str(fake),
                str(root / 'observer.json'), str(uuid4())], env=environment, capture_output=True,
                text=True, encoding='utf-8', timeout=15,
                input=''.join(json.dumps({'id': index, 'method': method}) + '\n' for index, method in enumerate(methods)))
            self.assertEqual(response.returncode, 0, response.stderr)
            replies = {item['id']: item for item in map(json.loads, response.stdout.splitlines())}
            self.assertEqual(replies[0]['result']['received'], 'initialize')
            self.assertEqual(replies[2]['result']['received'], 'account/read')
            self.assertEqual(replies[1]['error']['code'], -32045)
            self.assertEqual(replies[3]['error']['code'], -32045)

    def test_bound_desktop_startup_without_initialized_notification(self):
        from test_manager_proxy_auth import tokens
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            credential = tokens()
            (root / 'auth.json').write_text(json.dumps({'tokens': {
                'access_token': credential.access_token, 'account_id': credential.account_id}}), encoding='utf-8')
            fake = root / 'runtime.py'
            fake.write_text('''import sys,json
authenticated=False
for line in sys.stdin:
    m=json.loads(line)
    if m.get('method')=='initialize': result={'userAgent':'fixture'}
    elif m.get('method')=='account/login/start':
        authenticated=True
        result={'type':'chatgptAuthTokens'}
    elif m.get('method')=='configRequirements/read': result={'authenticated':authenticated,'requirements':None}
    else: raise RuntimeError('Unexpected startup message')
    print(json.dumps({'id':m['id'],'result':result}),flush=True)
''', encoding='utf-8')
            bootstrap = root / 'bootstrap.py'
            bootstrap.write_text("import sys,os\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1])\nfrom manager_core.runtime_proxy import proxy\nraise SystemExit(proxy(Path(sys.executable),[sys.argv[2]],Path(sys.argv[3]),sys.argv[4],dict(os.environ)))\n", encoding='utf-8')
            environment = {key: value for key, value in os.environ.items() if not key.upper().startswith('CODEX_')}
            environment.update(CODEX_HOME=str(root), CODEX_MANAGER_REAL_RUNTIME=sys.executable,
                               CODEX_MANAGER_AUTH_SOURCE=str(root))
            snapshot = root / 'observer.json'
            process = subprocess.Popen([sys.executable, str(bootstrap), str(SCRIPT_ROOT), str(fake),
                str(snapshot), str(uuid4())], env=environment, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
            replies = queue.Queue()
            def receive():
                for line in process.stdout: replies.put(json.loads(line))
            reader = threading.Thread(target=receive, daemon=True)
            reader.start()
            try:
                process.stdin.write(json.dumps({'id': 'init', 'method': 'initialize', 'params': {}})+'\n')
                process.stdin.flush()
                self.assertEqual(replies.get(timeout=10)['id'], 'init')
                process.stdin.write(json.dumps({'id': 'config', 'method': 'configRequirements/read'})+'\n')
                process.stdin.flush()
                self.assertEqual(replies.get(timeout=10), {'id': 'config', 'result': {
                    'authenticated': True, 'requirements': None}})
                process.stdin.close()
                self.assertEqual(process.wait(timeout=10), 0)
                reader.join(timeout=2)
                self.assertTrue(replies.empty(), 'Internal account binding must not leak to the native client.')
                state = json.loads(snapshot.read_text(encoding='utf-8'))
                self.assertTrue(state['initialized'])
                self.assertEqual(state['auth_binding']['state'], 'ready')
                self.assertFalse(state['connected'])
                self.assertNotIn(credential.access_token, snapshot.read_text(encoding='utf-8'))
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)
                process.stdin.close()
                process.stdout.close()
                process.stderr.close()


if __name__ == '__main__':
    unittest.main()
