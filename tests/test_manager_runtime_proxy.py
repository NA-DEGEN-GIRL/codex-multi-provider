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

STATUS_DLL_INIT_FAILED = 0xC0000142
STATUS_ACCESS_VIOLATION = 0xC0000005
STATUS_STACK_BUFFER_OVERRUN = 0xC0000409

# A runtime whose first `failures` starts die during process start with an
# NTSTATUS code (they never read stdin), and that answers every request later.
# argv: state directory, failures, exit code, seconds before dying, notify|quiet.
FLAKY_RUNTIME = r'''import json, os, sys, time
from pathlib import Path
state = Path(sys.argv[1])
failures, code, delay = int(sys.argv[2]), int(sys.argv[3], 0), float(sys.argv[4])
with open(state / 'starts.txt', 'a', encoding='utf-8') as handle:
    handle.write('%d\n' % os.getpid())
attempt = len((state / 'starts.txt').read_text(encoding='utf-8').split())
if attempt <= failures:
    if sys.argv[5] == 'notify':
        sys.stdout.write(json.dumps({'method': 'fixture/started', 'params': {}}) + '\n')
        sys.stdout.flush()
    time.sleep(delay)
    os._exit(code - (1 << 32) if code > 0x7FFFFFFF else code)
if sys.platform == 'win32':
    import ctypes
    pids = (ctypes.c_uint32 * 64)()
    count = ctypes.WinDLL('kernel32').GetConsoleProcessList(pids, 64)
    (state / 'console.json').write_text(json.dumps(list(pids[:min(count, 64)])), encoding='utf-8')
with open(state / ('received-%d.txt' % attempt), 'ab') as log:
    for line in sys.stdin.buffer:
        log.write(line)
        log.flush()
        message = json.loads(line)
        if 'id' in message:
            sys.stdout.write(json.dumps({'id': message['id'], 'result': {'attempt': attempt}}) + '\n')
            sys.stdout.flush()
'''

# Runs proxy() in-process with test-sized retry delays, then normal interpreter
# finalization (daemon readers still blocked on stdin must not abort it).
# FIXTURE_PROXY_PATCH: 'slow-spawn' makes every process creation take 0.5 s
# (a loaded machine); 'no-reader-thread' cannot start the second runtime
# output reader (thread exhaustion).
RETRY_BOOTSTRAP = '''import os, sys, threading, time, types
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from manager_core import runtime_proxy
runtime_proxy.START_RETRY_DELAYS = tuple(float(value) for value in sys.argv[5].split(','))
fixture = os.environ.pop('FIXTURE_PROXY_PATCH', '')
if fixture == 'slow-spawn':
    popen = runtime_proxy.subprocess.Popen
    def slow(*arguments, **options):
        time.sleep(0.5)
        return popen(*arguments, **options)
    runtime_proxy.subprocess = types.SimpleNamespace(**{**vars(runtime_proxy.subprocess), 'Popen': slow})
elif fixture == 'no-reader-thread':
    class Thread(threading.Thread):
        readers = 0
        def start(self):
            if self._args[1:] == ('runtime',):
                Thread.readers += 1
                if Thread.readers == 2:
                    raise RuntimeError('cannot start a new thread')
            super().start()
    runtime_proxy.threading = types.SimpleNamespace(**{**vars(threading), 'Thread': Thread})
raise SystemExit(runtime_proxy.proxy(Path(sys.executable), [sys.argv[2], *sys.argv[6:]], Path(sys.argv[3]),
                                     sys.argv[4], dict(os.environ)))
'''


def process_running(pid):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return False
    try:
        return kernel.WaitForSingleObject(handle, 0) == 258  # WAIT_TIMEOUT: still running.
    finally:
        kernel.CloseHandle(handle)


def clean_environment(root, **extra):
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith('CODEX_')}
    environment.update(CODEX_HOME=str(root), CODEX_MANAGER_REAL_RUNTIME=sys.executable, **extra)
    return environment


class RetryHarness:
    """A desktop stand-in: keeps stdin open and collects replies on a thread."""

    def __init__(self, test, *, failures, code=STATUS_DLL_INIT_FAILED, die_after=0.3,
                 mode='quiet', delays='0.05,0.05', patch=''):
        directory = tempfile.TemporaryDirectory()
        test.addCleanup(directory.cleanup)  # Cleanups run last-in first-out: after close().
        self.root = root = Path(directory.name)
        self.state = root / 'state'
        self.state.mkdir()
        runtime = root / 'flaky.py'
        runtime.write_text(FLAKY_RUNTIME, encoding='utf-8')
        bootstrap = root / 'bootstrap.py'
        bootstrap.write_text(RETRY_BOOTSTRAP, encoding='utf-8')
        self.snapshot = root / 'observer.json'
        self.process = subprocess.Popen(
            [sys.executable, str(bootstrap), str(SCRIPT_ROOT), str(runtime), str(self.snapshot), str(uuid4()),
             delays, str(self.state), str(failures), hex(code), str(die_after), mode],
            env=clean_environment(root, FIXTURE_PROXY_PATCH=patch), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.replies = queue.Queue()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        test.addCleanup(self.close)

    def _read(self):
        for line in self.process.stdout:
            self.replies.put(json.loads(line))

    def send(self, *messages):
        try:
            self.process.stdin.write(b''.join(json.dumps(item).encode() + b'\n' for item in messages))
            self.process.stdin.flush()
        except OSError:
            pass  # The proxy may already have exited; the test asserts on that.

    def starts(self):
        path = self.state / 'starts.txt'
        return [int(pid) for pid in path.read_text(encoding='utf-8').split()] if path.exists() else []

    def received(self, attempt):
        path = self.state / ('received-%d.txt' % attempt)
        return [json.loads(line) for line in path.read_bytes().splitlines()] if path.exists() else []

    def observed(self):
        return json.loads(self.snapshot.read_text(encoding='utf-8'))

    def finish(self, timeout=15):
        """Wait for the proxy while its stdin stays open (the desktop still waits)."""
        code = self.process.wait(timeout=timeout)
        self.reader.join(timeout=2)
        return code, self.process.stderr.read()

    def close(self):
        if self.process.poll() is None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            try:
                stream.close()
            except OSError:
                pass


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
            self.assertEqual((data['last_exit']['exit_code'], data['last_exit']['initialize_completed']), (0, True))

    def test_runtime_exit_before_initialize_is_recorded(self):
        # Like a runtime refusing its stores: it exits at startup, before any reply.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / 'runtime.py'
            fake.write_text('import sys\nsys.stderr.write("failed to initialize\\n")\nraise SystemExit(3)\n',
                            encoding='utf-8')
            bootstrap = root / 'bootstrap.py'
            bootstrap.write_text("import sys,os\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1])\nfrom manager_core.runtime_proxy import proxy\nraise SystemExit(proxy(Path(sys.executable),[sys.argv[2]],Path(sys.argv[3]),sys.argv[4],dict(os.environ)))\n", encoding='utf-8')
            environment = {key: value for key, value in os.environ.items() if not key.upper().startswith('CODEX_')}
            environment.update(CODEX_HOME=str(root), CODEX_MANAGER_REAL_RUNTIME=sys.executable)
            snapshot = root / 'observer.json'
            response = subprocess.run([sys.executable, str(bootstrap), str(SCRIPT_ROOT), str(fake), str(snapshot),
                                       str(uuid4())], env=environment, capture_output=True, timeout=15,
                                      input=json.dumps({'id': 1, 'method': 'initialize', 'params': {}}).encode() + b'\n')
            self.assertEqual(response.returncode, 3)
            # The runtime's own stderr is inherited, never piped through the proxy.
            self.assertIn(b'failed to initialize', response.stderr)
            last_exit = json.loads(snapshot.read_text(encoding='utf-8'))['last_exit']
            self.assertEqual((last_exit['exit_code'], last_exit['initialize_completed']), (3, False))
            self.assertIsInstance(last_exit['uptime_ms'], int)
            self.assertGreaterEqual(last_exit['uptime_ms'], 0)
            self.assertTrue(last_exit['exited_at'].endswith('+00:00'))

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


class EarlyRuntimeExitTests(unittest.TestCase):
    def test_entrypoint_reports_the_runtime_code_while_the_app_still_waits(self):
        # The desktop keeps stdin open while it waits for initialize. A runtime
        # that dies first used to leave a reader holding stdin's buffered lock,
        # and interpreter shutdown aborted (0xC0000005 replaced the real code).
        codes = [7] + ([STATUS_STACK_BUFFER_OVERRUN] if os.name == 'nt' else [])
        for code in codes:
            with self.subTest(code=hex(code)), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                profile_id = str(uuid4())
                observer = root / 'work/control-center/instances' / profile_id / 'runtime-state.json'
                state = root / 'state'
                state.mkdir()
                runtime = root / 'flaky.py'
                runtime.write_text(FLAKY_RUNTIME, encoding='utf-8')
                environment = clean_environment(root, CODEX_MANAGER_ROOT=str(root), CODEX_MANAGER_PROFILE_ID=profile_id,
                                                CODEX_MANAGER_OBSERVER_PATH=str(observer))
                process = subprocess.Popen(
                    [sys.executable, str(SCRIPT_ROOT / 'manager_core' / 'runtime_proxy.py'), '--', str(runtime),
                     str(state), '9', hex(code), '0.3', 'quiet', 'app-server'],
                    env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                try:
                    process.stdin.write(json.dumps({'id': 1, 'method': 'initialize', 'params': {}}).encode() + b'\n')
                    process.stdin.flush()
                    began = time.monotonic()
                    returncode = process.wait(timeout=15)
                    elapsed = time.monotonic() - began
                    error = process.stderr.read()
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
                    for stream in (process.stdin, process.stdout, process.stderr):
                        stream.close()
                self.assertNotIn(b'Fatal Python error', error)
                self.assertEqual(returncode & 0xFFFFFFFF, code, error)
                self.assertLess(elapsed, 5)
                last_exit = json.loads(observer.read_text(encoding='utf-8'))['last_exit']
                self.assertEqual((last_exit['exit_code'] & 0xFFFFFFFF, last_exit['initialize_completed']), (code, False))
                self.assertNotIn('retries', last_exit)
                self.assertEqual(len((state / 'starts.txt').read_text(encoding='utf-8').split()), 1)

    def test_entrypoint_turns_an_unexpected_failure_into_a_fixed_diagnostic(self):
        # Whatever escapes main() still leaves through os._exit, and only a
        # fixed line is shown: exception text can quote paths or values.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile_id = str(uuid4())
            observer = root / 'work/control-center/instances' / profile_id / 'runtime-state.json'
            wrapper = root / 'wrapper.py'
            wrapper.write_text('''import runpy, sys
sys.path.insert(0, sys.argv[1])
import manager_core.proxy_auth
class Broken:
    def __init__(self, *arguments, **options):
        raise RuntimeError('fixture-detail-must-not-appear')
manager_core.proxy_auth.AuthProxy = Broken
sys.argv = [sys.argv[2], '--', 'app-server']
runpy.run_path(sys.argv[0], run_name='__main__')
''', encoding='utf-8')
            environment = clean_environment(root, CODEX_MANAGER_ROOT=str(root), CODEX_MANAGER_PROFILE_ID=profile_id,
                                            CODEX_MANAGER_OBSERVER_PATH=str(observer))
            result = subprocess.run(
                [sys.executable, str(wrapper), str(SCRIPT_ROOT), str(SCRIPT_ROOT / 'manager_core' / 'runtime_proxy.py')],
                env=environment, stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(result.returncode, 70, result.stderr)
        self.assertIn(b'Codex managed runtime proxy stopped unexpectedly.', result.stderr)
        self.assertNotIn(b'fixture-detail-must-not-appear', result.stderr)
        self.assertNotIn(b'Traceback', result.stderr)

    def test_only_windows_process_start_failures_are_transient(self):
        from manager_core.runtime_proxy import transient_start_failure
        # A busy launcher can notice a loader failure late; the replay seal
        # (first output, start window) bounds which starts are replaced. An
        # access violation is a crash of runtime code, however early.
        cases = [(STATUS_DLL_INIT_FAILED, True), (STATUS_DLL_INIT_FAILED - (1 << 32), True),
                 (STATUS_ACCESS_VIOLATION, False), (STATUS_ACCESS_VIOLATION - (1 << 32), False),
                 (STATUS_STACK_BUFFER_OVERRUN, False), (1, False), (0, False), (None, False),
                 (float(STATUS_DLL_INIT_FAILED), False)]
        for code, expected in cases:
            with self.subTest(code=code):
                self.assertIs(transient_start_failure(code, windows=True), expected)
        self.assertFalse(transient_start_failure(STATUS_DLL_INIT_FAILED, windows=False))

    def test_exit_status_keeps_the_ntstatus_bits(self):
        from manager_core.runtime_proxy import exit_status
        self.assertEqual(exit_status(7), 7)
        if os.name == 'nt':
            self.assertEqual(exit_status(STATUS_DLL_INIT_FAILED), STATUS_DLL_INIT_FAILED - (1 << 32))
            self.assertEqual(exit_status(STATUS_DLL_INIT_FAILED - (1 << 32)), STATUS_DLL_INIT_FAILED - (1 << 32))


class RecordingStream:
    def __init__(self, broken=False):
        self.broken, self.data, self.closed = broken, bytearray(), threading.Event()

    def write(self, body):
        if self.broken:
            raise BrokenPipeError('the child exited')
        self.data.extend(body)

    def flush(self):
        pass

    def close(self):
        self.closed.set()


class FakeChild:
    def __init__(self, stream):
        self.stdin = stream


class RuntimeInputTests(unittest.TestCase):
    def setUp(self):
        from manager_core.runtime_proxy import RuntimeInput
        self.gaps = []
        self.dead = RecordingStream(broken=True)
        self.input = RuntimeInput(self.dead, lambda: self.gaps.append('gap'), limit=1024)

    def wait_for(self, condition):
        deadline = time.monotonic() + 5
        while not condition() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(condition())

    def test_replacement_receives_kept_input_once_in_order_without_a_gap(self):
        first_writer = self.input.writer
        self.input.write(b'{"id":1}\n')
        first_writer.thread.join(5)  # The exited child's pipe failed.
        self.input.write(b'{"id":2}\n')  # Kept, not raised, while replaceable.
        replacement = RecordingStream()
        child = self.input.replace(lambda: FakeChild(replacement))
        self.assertIs(child.stdin, replacement)
        self.input.write(b'{"id":3}\n')
        self.wait_for(lambda: bytes(replacement.data) == b'{"id":1}\n{"id":2}\n{"id":3}\n')
        self.assertTrue(self.dead.closed.is_set())
        self.input.seal()
        self.assertEqual(self.gaps, [])
        self.assertFalse(self.input.replaceable())
        self.assertIsNone(self.input.replace(lambda: self.fail('sealed input is never replayed')))

    def test_failed_write_is_a_gap_once_no_replacement_follows(self):
        first_writer = self.input.writer
        self.input.write(b'{"id":1}\n')
        first_writer.thread.join(5)
        self.assertEqual(self.gaps, [])
        self.input.seal()
        self.assertEqual(self.gaps, ['gap'])
        with self.assertRaises(OSError):
            self.input.write(b'{"id":2}\n')

    def test_admin_writes_and_the_replay_bound_seal(self):
        self.input.write(b'x' * 10, before_write=lambda: True)
        self.assertFalse(self.input.replaceable())
        from manager_core.runtime_proxy import RuntimeInput
        bounded = RuntimeInput(RecordingStream(), lambda: None, limit=16)
        bounded.write(b'a' * 16)
        self.assertTrue(bounded.replaceable())
        bounded.write(b'b')
        self.assertFalse(bounded.replaceable())

    def test_a_seal_reports_the_deferred_gap_even_when_its_write_fails(self):
        # An admin write or the replay bound seals the input after the exited
        # child's writer failed: the write raises, and the gap is still reported.
        from manager_core.runtime_proxy import RuntimeInput
        for name, body, before_write in (('admin', b'{"id":2}\n', lambda: True), ('bound', b'x' * 2048, None)):
            with self.subTest(name):
                gaps = []
                runtime_input = RuntimeInput(RecordingStream(broken=True), lambda: gaps.append('gap'), limit=1024)
                writer = runtime_input.writer
                runtime_input.write(b'{"id":1}\n')
                writer.thread.join(5)
                self.assertEqual(gaps, [])
                with self.assertRaises(OSError):
                    runtime_input.write(body, before_write)
                self.assertEqual(gaps, ['gap'])
                self.assertFalse(runtime_input.replaceable())

    def test_closed_input_is_not_replaced(self):
        self.input.close()
        self.assertFalse(self.input.replaceable())
        self.assertIsNone(self.input.replace(lambda: self.fail('closed input is never replayed')))
        with self.assertRaises(OSError):
            self.input.write(b'{"id":1}\n')


@unittest.skipUnless(os.name == 'nt', 'Windows NTSTATUS process-start failures')
class RuntimeStartRetryTests(unittest.TestCase):
    def test_replacement_runtime_gets_every_frame_once_and_status_records_retries(self):
        harness = RetryHarness(self, failures=2)
        requests = [{'id': 1, 'method': 'initialize', 'params': {}}] + [
            {'id': index, 'method': 'config/read', 'params': {'index': index}} for index in range(2, 30)]
        harness.send(*requests[:10])
        time.sleep(.5)  # Some frames reach the first child, the rest its exited pipe.
        harness.send(*requests[10:20])
        time.sleep(.3)
        harness.send(*requests[20:])
        replies = [harness.replies.get(timeout=15) for _ in requests]
        self.assertEqual(sorted(reply['id'] for reply in replies), list(range(1, 30)))
        self.assertEqual({reply['result']['attempt'] for reply in replies}, {3})
        starts = harness.starts()
        self.assertEqual(len(starts), 3)
        self.assertEqual([item['id'] for item in harness.received(3)], list(range(1, 30)))
        # The proxy's own console is shared, not a new console per runtime.
        console = json.loads((harness.state / 'console.json').read_text(encoding='utf-8'))
        self.assertIn(harness.process.pid, console)
        deadline = time.monotonic() + 8
        while True:
            state = harness.observed()
            if (state.get('runtime_process_id') == starts[2] and state['initialized']) or time.monotonic() > deadline:
                break
            time.sleep(.05)
        self.assertEqual(state['runtime_process_id'], starts[2])
        self.assertTrue(state['initialized'])
        self.assertEqual([item['exit_code'] for item in state['start_retries']], [STATUS_DLL_INIT_FAILED] * 2)
        self.assertTrue(all(item['uptime_ms'] >= 0 and item['exited_at'] for item in state['start_retries']))
        self.assertNotIn('protocol_gap', state['stream_incomplete_reasons'])
        harness.process.stdin.close()
        code, error = harness.finish()
        self.assertEqual(code, 0, error)
        self.assertTrue(harness.replies.empty())
        final = harness.observed()
        self.assertEqual((final['last_exit']['exit_code'], final['last_exit']['retries']), (0, 2))
        self.assertEqual(len(final['start_retries']), 2)
        self.assertNotIn('protocol_gap', final['stream_incomplete_reasons'])

    def test_retries_are_bounded_and_the_last_start_failure_is_reported(self):
        harness = RetryHarness(self, failures=9)
        harness.send({'id': 1, 'method': 'initialize', 'params': {}})
        code, error = harness.finish()
        self.assertNotIn(b'Fatal Python error', error)
        self.assertEqual(code & 0xFFFFFFFF, STATUS_DLL_INIT_FAILED, error)
        self.assertEqual(len(harness.starts()), 3)
        state = harness.observed()
        self.assertEqual((state['last_exit']['exit_code'], state['last_exit']['retries'],
                          state['last_exit']['initialize_completed']), (STATUS_DLL_INIT_FAILED, 2, False))
        self.assertEqual(len(state['start_retries']), 2)
        self.assertTrue(harness.replies.empty())

    def test_uptime_includes_process_creation_for_every_start(self):
        # On a loaded machine creating the process is part of the start. The
        # replacement's clock, like the first child's, starts before it.
        harness = RetryHarness(self, failures=2, patch='slow-spawn')
        harness.send({'id': 1, 'method': 'initialize', 'params': {}})
        self.assertEqual(harness.replies.get(timeout=20)['result']['attempt'], 3)
        harness.process.stdin.close()
        code, error = harness.finish()
        self.assertEqual(code, 0, error)
        retries = harness.observed()['start_retries']
        self.assertEqual(len(retries), 2)
        # Each failed child lived die_after (0.3 s) after a 0.5 s process creation.
        self.assertTrue(all(item['uptime_ms'] >= 800 for item in retries), retries)

    def test_replacement_without_an_output_reader_is_stopped(self):
        harness = RetryHarness(self, failures=1, patch='no-reader-thread')
        harness.send({'id': 1, 'method': 'initialize', 'params': {}})
        code, error = harness.finish()  # The app's input stays open.
        self.assertNotIn(b'Fatal Python error', error)
        self.assertNotIn(b'Traceback', error)
        self.assertEqual(code & 0xFFFFFFFF, STATUS_DLL_INIT_FAILED, error)
        state = harness.observed()
        self.assertEqual((state['last_exit']['exit_code'], state['last_exit']['retries'],
                          state['last_exit']['initialize_completed']), (STATUS_DLL_INIT_FAILED, 1, False))
        # The status names the replacement, which no longer runs.
        self.assertNotIn(state['runtime_process_id'], harness.starts()[:1])
        self.assertFalse(process_running(state['runtime_process_id']))
        self.assertTrue(harness.replies.empty())

    def test_runtime_output_or_a_closed_app_input_prevents_a_retry(self):
        # An access violation is never retried, however early it came.
        cases = [dict(mode='notify'), dict(code=STATUS_ACCESS_VIOLATION, die_after=0.2), dict(close=True)]
        for case in cases:
            with self.subTest(**case):
                close = case.pop('close', False)
                harness = RetryHarness(self, failures=9, delays='2,2', **case)
                harness.send({'id': 1, 'method': 'initialize', 'params': {}})
                if close:
                    deadline = time.monotonic() + 10
                    while not harness.starts() and time.monotonic() < deadline:
                        time.sleep(.02)
                    time.sleep(.6)  # The first child has exited; the proxy waits to retry.
                    harness.process.stdin.close()
                code, error = harness.finish()
                self.assertNotIn(b'Fatal Python error', error)
                self.assertEqual(code & 0xFFFFFFFF, case.get('code', STATUS_DLL_INIT_FAILED), error)
                self.assertEqual(len(harness.starts()), 1)
                state = harness.observed()
                self.assertNotIn('start_retries', state)
                self.assertNotIn('retries', state['last_exit'])
                if case.get('mode') == 'notify':
                    self.assertEqual(harness.replies.get(timeout=2)['method'], 'fixture/started')


@unittest.skipUnless(os.name == 'nt', 'Windows consoles')
class RuntimeConsoleTests(unittest.TestCase):
    def test_runtime_shares_an_existing_console_and_never_opens_one(self):
        probe = ('import sys; sys.path.insert(0, sys.argv[1]); '
                 'from manager_core.runtime_proxy import runtime_creation_flags; print(runtime_creation_flags())')
        for flags, expected in ((subprocess.CREATE_NO_WINDOW, 0),
                                (subprocess.DETACHED_PROCESS, subprocess.CREATE_NO_WINDOW)):
            with self.subTest(flags=flags):
                result = subprocess.run([sys.executable, '-c', probe, str(SCRIPT_ROOT)], capture_output=True,
                                        text=True, timeout=15, creationflags=flags)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(int(result.stdout), expected)


if __name__ == '__main__':
    unittest.main()
