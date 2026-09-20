"""Shared Windows skill worker and dedicated SSH tunnels, outside UI launch paths."""
import base64
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import secrets
import shlex
import subprocess
import threading
import time
from uuid import uuid4

from .personal_skills import inventory
from .release_code import script_path
from .store import atomic_json


SUPPORTED = {
    '3d-assets': ('asset_auto.cli', 'assetctl.py', '''doctor list tripo-balance tripo-plan text-motion-plan compare-animations
        process-plan tripo-process-plan resume-tripo resume-process resume-tripo-process prepare-segment resume-text-motion
        generate edit process tripo-process assess text-motion blender-edit merge-animations resume-blender-edit
        resume-merge-animations inspect godot review job'''.split()),
    'game-audio': ('game_audio.cli', 'audioctl.py', '''doctor plan generate import edit job resume account voices history
        recover-history analyze audition verify review benchmark-plan adopt-music'''.split()),
}
_TUNNEL_IO_TIMEOUT = 30
_TUNNEL_EXIT_TIMEOUT = 2


def registrations(source, enabled):
    result = []
    for row in inventory(source):
        if row['name'] not in SUPPORTED or not row['enabled'] or not enabled.get(row['name'], False):
            continue
        skill = Path(row['path']).resolve().parent
        root = skill.parents[2]
        module, wrapper, commands = SUPPORTED[row['name']]
        python = root / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        if not python.is_file() or not (skill / 'scripts' / wrapper).is_file():
            continue
        result.append(dict(name=row['name'], root=str(root), skill_dir=str(skill), python=str(python),
                           module=module, commands=commands, description=row['description']))
    return result


def projection(rows):
    """Preserve relative docs links in a lightweight checkout-shaped projection."""
    files = {}
    for row in rows:
        root, skill = Path(row['root']), Path(row['skill_dir'])
        candidates = list(skill.rglob('*.md'))
        candidates += [skill / 'scripts' / SUPPORTED[row['name']][1]]
        candidates += list((root / 'docs').rglob('*.md')) if (root / 'docs').is_dir() else []
        candidates += [p for p in (root / 'README.md', root / 'INSTALL.md') if p.is_file()]
        for path in candidates:
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(root) or path.is_symlink() or path.stat().st_size > 1024 * 1024:
                raise ValueError('스킬 문서가 등록된 실행 폴더 범위를 벗어났습니다.')
            relative = row['name'] + '/' + path.relative_to(root).as_posix()
            files[relative] = base64.b64encode(path.read_bytes()).decode()
    return files


class Tunnel:
    def __init__(self, remote, alias, python, local_port, installer, stopping=None):
        self.stopping = stopping or threading.Event()
        self.closed = threading.Event()
        self._close_done = threading.Event()
        self._close_lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._io_threads = []
        self._sender = None
        remote._alias(alias)
        if not isinstance(python, str) or not python.startswith('/') or '\n' in python:
            raise ValueError('SSH Python 실행 경로를 확인하지 못했습니다.')
        command = 'exec ' + shlex.quote(python) + ' -u -c ' + shlex.quote(installer)
        argv = [remote.ssh, '-F', str(remote.ssh_config), '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                '-o', 'ConnectTimeout=10', '-o', 'ConnectionAttempts=1', '-o', 'ExitOnForwardFailure=yes',
                '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3', '-o', 'ControlMaster=no',
                '-o', 'ControlPath=none', '-o', 'LogLevel=INFO',
                '-R', '127.0.0.1:0:127.0.0.1:' + str(local_port), alias, command]
        self.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', errors='replace', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.lines, ports = queue.Queue(), queue.Queue()
        def errors():
            try:
                for line in self.process.stderr:
                    found = re.search(r'Allocated port (\d+) for remote forward', line)
                    if found: ports.put(int(found[1]))
            except (OSError, ValueError):
                pass
            finally:
                ports.put(None)
                self.process.stderr.close()
        def output():
            try:
                for line in self.process.stdout:
                    try: self.lines.put(json.loads(line))
                    except ValueError: pass
            except (OSError, ValueError):
                pass
            finally:
                self.lines.put(None)
                self.process.stdout.close()
        self._start_io(errors, 'skill-tunnel-stderr')
        self._start_io(output, 'skill-tunnel-stdout')
        try:
            self.port = self._wait(ports, 18)
            if self.port is None: raise ValueError('SSH에서 스킬 연결 포트를 열지 못했습니다.')
        except (queue.Empty, ValueError):
            self.close()
            raise ValueError('SSH 스킬 연결 실패: 원격 포트 전달 허용과 연결 상태를 확인하세요.') from None
        self.signature = None

    def _start_io(self, target, name):
        with self._io_lock:
            if self.closed.is_set():
                raise ValueError('SSH 스킬 연결이 종료되었습니다.')
            thread = threading.Thread(target=target, name=name, daemon=True)
            self._io_threads = [item for item in self._io_threads if item.is_alive()]
            self._io_threads.append(thread)
            thread.start()
            return thread

    def _wait(self, channel, timeout):
        deadline = time.monotonic() + timeout
        while not self.stopping.is_set() and not self.closed.is_set():
            try: return channel.get(timeout=min(.25, max(.001, deadline - time.monotonic())))
            except queue.Empty:
                if time.monotonic() >= deadline: raise
        raise ValueError('SSH 스킬 연결을 종료하고 있습니다.')

    def update(self, payload):
        if self.stopping.is_set() or self.closed.is_set():
            raise ValueError('SSH 스킬 연결이 종료되었습니다.')
        signature = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        if signature == self.signature and self.process.poll() is None:
            return self.result
        sent = queue.Queue(maxsize=1)
        content = json.dumps(payload, ensure_ascii=False) + '\n'

        def send():
            try:
                self.process.stdin.write(content)
                self.process.stdin.flush()
                sent.put(None)
            except (OSError, ValueError) as error:
                sent.put(error)
            finally:
                # The writer owns its buffer lock here. After transport exit a
                # blocked write unwinds before it closes its own stream.
                if self.closed.is_set():
                    try: self.process.stdin.close()
                    except (OSError, ValueError): pass
        try:
            self._sender = self._start_io(send, 'skill-tunnel-stdin')
            error = self._wait(sent, _TUNNEL_IO_TIMEOUT)
            if error is not None:
                raise error
            response = self._wait(self.lines, _TUNNEL_IO_TIMEOUT)
        except (OSError, ValueError, queue.Empty):
            self.close()
            raise ValueError('SSH 스킬 안내를 전달하지 못했습니다. 다음 확인에서 연결을 복구합니다.') from None
        if not response or not response.get('ok'):
            raise ValueError((response or {}).get('error', 'SSH 스킬 연결이 종료되었습니다.'))
        self.signature, self.result = signature, response['result']
        return self.result

    def close(self):
        # Never call a buffered stream's close while another thread may be
        # blocked inside its read/write. Ending this owned SSH process first
        # releases the pipe operations, including a full stdin pipe.
        if not self._close_lock.acquire(blocking=False):
            self._close_done.wait(timeout=2 * _TUNNEL_EXIT_TIMEOUT + 2)
            return
        try:
            with self._io_lock:
                self.closed.set()
                threads = list(self._io_threads)
            if self.process.poll() is None:
                try: self.process.terminate()
                except OSError: pass
            try:
                self.process.wait(timeout=_TUNNEL_EXIT_TIMEOUT)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=_TUNNEL_EXIT_TIMEOUT)
            deadline = time.monotonic() + 2
            for thread in threads:
                thread.join(timeout=max(0, deadline - time.monotonic()))
            # Reader/writer threads close their own streams if pipe unwinding
            # takes longer than the bounded join. The transport is already dead.
            if not any(thread.is_alive() for thread in threads):
                for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                    try: stream.close()
                    except (OSError, ValueError): pass
        finally:
            self._close_done.set()
            self._close_lock.release()


class SkillBridge:
    def __init__(self, root, store, personal, remote):
        self.root, self.store, self.personal, self.remote = Path(root), store, personal, remote
        self.directory = store.directory / 'skill-bridge'
        self.path = self.directory / 'settings.json'
        self.lock = threading.RLock()
        self.stopping, self.wake = threading.Event(), threading.Event()
        self.worker, self.server = None, None
        self.tunnels, self.status, self.retry_at = {}, {}, {}
        self.rows = []

    def settings(self):
        if self.path.exists():
            value = json.loads(self.path.read_text(encoding='utf-8'))
            if value.get('version') != 1: raise ValueError('SSH 스킬 설정 형식을 확인하세요.')
            return value
        return dict(version=1, owner=str(uuid4()), skills={}, tokens={}, hosts=None)

    def configure(self, names, *, hosts=None):
        """Explicit CLI/setup configuration; toggles never perform SSH on the caller."""
        with self.lock:
            settings = self.settings()
            if any(name not in SUPPORTED for name in names): raise ValueError('지원하지 않는 SSH 스킬입니다.')
            settings['skills'] = {name: name in names for name in SUPPORTED}
            settings['hosts'] = hosts
            self.directory.mkdir(parents=True, exist_ok=True)
            atomic_json(self.path, settings)
        self.wake.set()

    def decorate(self, catalog):
        with self.lock:
            settings = self.settings()
            for row in catalog['skills']:
                supported = row['name'] in SUPPORTED
                enabled = settings['skills'].get(row['name'], False)
                available = any(row['name'] in value.get('installed', []) and value.get('status') == 'ready'
                                for value in self.status.values())
                message = ('이 스킬은 아직 SSH 실행 연결을 지원하지 않습니다.' if not supported else
                           'SSH에서 이 PC의 제작 환경을 사용합니다.' if available and enabled and row['enabled'] else
                           '개인 스킬 사용을 켜면 SSH에서도 사용할 수 있습니다.' if enabled and not row['enabled'] else
                           'SSH 연결을 백그라운드에서 준비합니다.' if enabled else 'SSH 사용 꺼짐')
                errors = [value.get('message') for value in self.status.values() if value.get('status') == 'error']
                if enabled and errors: message = errors[0]
                row['bridge'] = dict(supported=supported, enabled=enabled,
                                     status='ready' if available else 'pending' if enabled else 'off', message=message)
            catalog['bridge_hosts'] = [dict(alias=alias, **value) for alias, value in self.status.items()]
        return catalog

    def set(self, skill_id, enabled):
        if type(enabled) is not bool: raise ValueError('SSH 스킬 사용 여부가 올바르지 않습니다.')
        row = next((r for r in inventory(self.personal.source) if r['id'] == skill_id), None)
        if row is None or row['name'] not in SUPPORTED: raise ValueError('SSH 실행을 지원하는 개인 스킬을 선택하세요.')
        with self.lock:
            settings = self.settings(); settings['skills'][row['name']] = enabled
            self.directory.mkdir(parents=True, exist_ok=True); atomic_json(self.path, settings)
            # Disable immediately; remote discovery is reconciled asynchronously.
            if self.server:
                self.server.update_registrations(registrations(self.personal.source, settings['skills']))
        self.wake.set()
        return self.decorate(self.personal.list())

    def refresh(self):
        with self.lock:
            if self.server:
                self.server.update_registrations(registrations(self.personal.source, self.settings()['skills']))
        self.wake.set()

    def start(self):
        if self.worker: return
        def watch():
            while not self.stopping.is_set():
                self.wake.clear()
                try: self.reconcile()
                except (OSError, ValueError, RuntimeError) as error:
                    with self.lock: self.status['workspace'] = dict(status='error', message=str(error))
                else:
                    with self.lock: self.status.pop('workspace', None)
                self.wake.wait(30)
        self.worker = threading.Thread(target=watch, name='ssh-skill-bridge', daemon=True)
        self.worker.start()

    def reconcile(self):
        from .skill_bridge_server import BridgeServer
        with self.lock:
            if self.stopping.is_set(): return
            settings = self.settings()
            rows = registrations(self.personal.source, settings['skills'])
            if not rows and not self.tunnels:
                if self.server:
                    self.server.update_registrations([]); self.server.update_tokens({})
                return
            bindings = {}
            for profile in self.store.read().get('profiles', []):
                if profile.get('removed_at'): continue
                for binding in profile.get('remote_bindings', []):
                    alias = binding.get('alias')
                    if alias and (settings.get('hosts') is None or alias in settings['hosts']):
                        bindings[alias] = binding
            for alias in bindings: settings['tokens'].setdefault(alias, secrets.token_urlsafe(32))
            self.directory.mkdir(parents=True, exist_ok=True); atomic_json(self.path, settings)
            tokens = {settings['tokens'][alias]: alias for alias in bindings}
            if self.server is None:
                self.server = BridgeServer(self.directory / 'jobs', rows, tokens)
            else:
                self.server.update_registrations(rows)
                self.server.update_tokens(tokens)
            port = self.server.start()['port']; self.rows = rows
        files = projection(rows)
        installer = script_path(self.root, 'scripts/remote_helpers/skill_bridge_install.py').read_text(encoding='utf-8')
        client = base64.b64encode(script_path(self.root, 'scripts/remote_helpers/skill_bridge_client.py').read_bytes()).decode()
        with self.lock:
            removed = []
            for alias in list(self.tunnels):
                if alias not in bindings:
                    removed.append(self.tunnels.pop(alias))
                    self.status.pop(alias, None)
                    self.retry_at.pop(alias, None)
        for tunnel in removed:
            tunnel.close()
        for alias, binding in bindings.items():
            if self.stopping.is_set(): return
            if time.monotonic() < self.retry_at.get(alias, 0): continue
            try:
                with self.lock: tunnel = self.tunnels.get(alias)
                if tunnel and tunnel.process.poll() is not None:
                    tunnel.close()
                    with self.lock: self.tunnels.pop(alias, None)
                    tunnel = None
                if tunnel is None:
                    tunnel = Tunnel(self.remote, alias, binding['remote_python'], port, installer, self.stopping)
                    with self.lock:
                        stopping = self.stopping.is_set()
                        if not stopping: self.tunnels[alias] = tunnel
                    if stopping:
                        tunnel.close()
                        return
                result = tunnel.update(dict(owner=settings['owner'], files=files, client=client,
                    endpoint='http://127.0.0.1:' + str(tunnel.port) + '/v1', token=settings['tokens'][alias],
                    enabled_skills=[r['name'] for r in rows]))
                conflicts = result.get('conflicts', [])
                with self.lock:
                    self.status[alias] = dict(status='ready' if not conflicts else 'error', installed=result['installed'],
                        message='이 PC의 스킬 연결 준비됨' if not conflicts else '원격에 같은 이름의 스킬이 있어 보존했습니다: ' + ', '.join(conflicts))
                    self.retry_at.pop(alias, None)
            except (OSError, ValueError, RuntimeError, KeyError) as error:
                with self.lock: tunnel = self.tunnels.pop(alias, None)
                if tunnel: tunnel.close()
                with self.lock:
                    self.retry_at[alias] = time.monotonic() + 90
                    if not self.stopping.is_set():
                        self.status[alias] = dict(status='error', installed=[], message=str(error))

    def shutdown(self):
        self.stopping.set(); self.wake.set()
        with self.lock:
            tunnels = list(self.tunnels.values())
            self.tunnels.clear()
        # Close pipes before joining a worker that may be sending a projection.
        for tunnel in tunnels: tunnel.close()
        if self.worker: self.worker.join(timeout=5)
        if self.server: self.server.close()
