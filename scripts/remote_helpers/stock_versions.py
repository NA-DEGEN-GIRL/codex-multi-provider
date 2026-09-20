"""Inspect default Codex versions; only an explicitly confirmed request updates it.

No account files or task bodies are read. Official daemons do not provide the
manager's atomic idle fence, so this module never claims safe automatic restart.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid

MAX_JSON = 32768
VERSION = re.compile(r'(?:codex-cli )?(\d+\.\d+\.\d+(?:[-+][a-zA-Z0-9.-]+)?)\Z')


def version(value):
    match = VERSION.fullmatch(value.strip()) if isinstance(value, str) else None
    return match[1] if match else None


def environment():
    result = {k: v for k, v in os.environ.items() if not k.startswith(('CODEX_', 'OPENAI_'))}
    result['CODEX_HOME'] = str(Path.home() / '.codex')
    return result


def base():
    return Path.home() / '.local/share/codex-control-center/stock-updates'


def read_json(path):
    if not path.exists():
        return None
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_JSON or info.st_uid != os.getuid():
        raise ValueError('invalid_stock_update_record')
    with path.open('rb') as stream:
        data = stream.read(MAX_JSON + 1)
    if len(data) > MAX_JSON:
        raise ValueError('oversized_stock_update_record')
    return json.loads(data)


def atomic(path, value):
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            os.chmod(temporary, 0o600)
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def owned_directory(path):
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError('symlink_stock_update_directory')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.stat().st_uid != os.getuid():
        raise ValueError('foreign_stock_update_directory')
    os.chmod(path, 0o700)


def command(cli, args):
    # Spool to disk and read at most MAX_JSON bytes instead of allocating a PIPE
    # response of arbitrary size. The command also has a bounded execution time.
    with tempfile.TemporaryFile() as output:
        completed = subprocess.run([str(cli), *args], stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.DEVNULL,
                                   env=environment(), timeout=20, check=False)
        output.seek(0)
        value = output.read(MAX_JSON + 1)
    if completed.returncode or len(value) > MAX_JSON:
        raise ValueError('stock_version_probe_failed')
    return value.decode('utf-8').strip()


def worker_alive(job):
    try:
        process = Path('/proc') / str(int(job['pid']))
        fields = (process / 'stat').read_text().rsplit(')', 1)[1].split()
        return (fields[0] != 'Z' and fields[19] == job['birth']
                and str(base() / 'jobs' / (job['id'] + '.json')).encode()
                in (process / 'cmdline').read_bytes().split(b'\0'))
    except (OSError, ValueError, KeyError, IndexError):
        return False


def public_job():
    pointer = read_json(base() / 'current.json')
    if not pointer:
        return None
    identity = str(uuid.UUID(pointer['id']))
    job = read_json(base() / 'jobs' / (identity + '.json'))
    if not job or job.get('id') != identity:
        raise ValueError('stock_update_job_missing')
    state = job['state']
    if state == 'starting' and time.time() - job['at'] > 30:
        state = 'attention'
    if state == 'applying' and not worker_alive(job):
        state = 'attention'
    messages = {'starting': '기본 Codex 업데이트를 시작하고 있습니다.',
                'applying': '기본 Codex 설치·서비스 재시작 중입니다.',
                'complete': '기본 Codex 업데이트 후 서비스 버전을 확인했습니다.',
                'attention': '업데이트 완료를 확인하지 못했습니다. 중복 실행하지 않고 확인을 기다립니다.'}
    return dict(id=identity, state=state, message=messages.get(state, '업데이트 상태 확인 필요'),
                verification_pending=state == 'attention' and job.get('command_succeeded') is True)


def probe():
    architecture = platform.machine().lower()
    architecture = {'amd64': 'x86_64', 'arm64': 'aarch64'}.get(architecture, architecture)
    machine = Path('/etc/machine-id').read_text().strip()
    identity_fields = (machine, str(os.getuid()), str(Path.home()))
    identity = hashlib.sha256('\0'.join(identity_fields).encode()).hexdigest()
    result = dict(cli_version=None, daemon_version=None, daemon_state='unknown',
                  platform=sys.platform, architecture=architecture,
                  host_identity=identity,
                  managed_host_identity=hashlib.sha256('\\0'.join(identity_fields).encode()).hexdigest(),
                  state='unavailable', update_supported=False, safe_auto_update=False,
                  message='기본 Codex 버전을 확인하지 못했습니다.', update_job=public_job())
    found = shutil.which('codex')
    if not found:
        result['message'] = '기본 Codex CLI가 설치되어 있지 않습니다.'
        return result
    cli = Path(found)
    info = cli.stat()
    # npm commonly creates user-owned 0775 launchers. Reject world-writable or
    # foreign-owned launchers without excluding that normal installation mode.
    if not cli.is_absolute() or not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid()) or info.st_mode & 0o002:
        raise ValueError('untrusted_stock_cli')
    result['cli_version'] = version(command(cli, ['--version']))
    try:
        observed = json.loads(command(cli, ['app-server', 'daemon', 'version']))
    except (ValueError, subprocess.TimeoutExpired):
        result['message'] = '이 기본 Codex 버전은 서비스 상태 확인을 지원하지 않습니다.'
        return result
    result['daemon_version'] = version(observed.get('appServerVersion'))
    result['managed_installation_version'] = version(observed.get('managedCodexVersion'))
    result['daemon_state'] = str(observed.get('status', 'unknown'))[:64]
    help_text = command(cli, ['app-server', 'daemon', 'update', '--help'])
    result['update_supported'] = ('daemon update' in help_text and bool(result['cli_version']))
    result['version_fingerprint'] = hashlib.sha256(json.dumps([
        identity, str(cli), str(cli.resolve()), info.st_mtime_ns, info.st_size,
        result['cli_version'], result['daemon_version'], result['daemon_state']], separators=(',', ':')).encode()).hexdigest()
    # A confirmation is consumed by creating its journal, even when the updater
    # finds no newer release and all version strings remain unchanged.
    result['observation_id'] = hashlib.sha256((result['version_fingerprint'] + ':'
        + (result['update_job'] or {}).get('id', '')).encode()).hexdigest()
    result['state'] = ('current' if result['cli_version'] is not None
                       and result['daemon_version'] == result['cli_version'] else 'update_needed')
    result['message'] = ('CLI와 서비스 버전이 일치합니다. 공식 최신 버전은 기본 Codex 업데이터가 확인합니다.'
                         if result['state'] == 'current' else 'CLI와 서비스 버전이 다릅니다. 기본 Codex 작업 종료 후 수동으로 적용하세요.')
    if result['daemon_state'] != 'running':
        result.update(state='stopped', message='기본 Codex 서비스가 실행 중이지 않거나 상태를 확인할 수 없습니다.')
    reconcile_completed_command(result)
    return result


def reconcile_completed_command(observed):
    job = observed.get('update_job')
    if not job or job['state'] != 'attention' or not service_verified(observed):
        return
    path = base() / 'jobs' / (job['id'] + '.json')
    record = read_json(path)
    # A successful command is durable before verification. Re-observation may
    # finish that verification after a transient disconnect, but never reruns it.
    if record and record.get('command_succeeded') is True and not worker_alive(record):
        record.update(state='complete', at=time.time())
        atomic(path, record)
        observed['update_job'] = public_job()


def service_verified(observed):
    # Official update may advance the standalone install independently of npm.
    expected = observed.get('managed_installation_version') or observed.get('cli_version')
    return (observed.get('daemon_state') == 'running' and expected is not None
            and observed.get('daemon_version') == expected)


@contextmanager
def update_lock(wait=False):
    import fcntl
    owned_directory(base())
    descriptor = os.open(base() / 'update.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'r+b') as stream:
        info = os.fstat(stream.fileno())
        if info.st_nlink != 1 or info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode):
            raise ValueError('invalid_stock_update_lock')
        deadline = time.monotonic() + (30 if wait else 0)
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        yield


def start(request, source):
    if request.get('confirmed') is not True:
        raise ValueError('stock_update_requires_confirmation')
    if not re.fullmatch('[0-9a-f]{64}', request.get('observation_id', '')):
        raise ValueError('stock_update_requires_current_observation')
    prior = public_job()
    if prior and prior['state'] in ('starting', 'applying', 'attention'):
        return probe()
    with update_lock():
        observed = probe()
        prior = observed.get('update_job')
        if prior and prior['state'] in ('starting', 'applying', 'attention'):
            return observed
        if not observed['update_supported'] or observed.get('observation_id') != request['observation_id']:
            raise ValueError('stock_versions_changed_refresh_required')
        helpers, jobs = base() / 'helpers', base() / 'jobs'
        owned_directory(helpers)
        owned_directory(jobs)
        content = source.encode('utf-8')
        helper = helpers / (hashlib.sha256(content).hexdigest() + '.py')
        if helper.exists():
            if helper.is_symlink() or helper.read_bytes() != content:
                raise ValueError('stock_update_helper_changed')
        else:
            descriptor = os.open(helper, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        identity = str(uuid.uuid4())
        path = jobs / (identity + '.json')
        job = dict(id=identity, state='starting', at=time.time(), observation_id=observed['observation_id'],
                   version_fingerprint=observed['version_fingerprint'])
        atomic(path, job)
        atomic(base() / 'current.json', dict(id=identity))
        # A disconnect cannot orphan an unjournaled update or rerun the command.
        subprocess.Popen([sys.executable, str(helper), '--worker', str(path)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         env=environment(), start_new_session=True, close_fds=True)
        observed['update_job'] = public_job()
        return observed


def run_worker(path):
    path = Path(path)
    if path.parent.resolve() != (base() / 'jobs').resolve() or path.is_symlink():
        raise ValueError('invalid_stock_update_job')
    with update_lock(wait=True):
        job = read_json(path)
        if job.get('state') != 'starting':
            return
        try:
            observed = probe()
            if observed.get('version_fingerprint') != job['version_fingerprint']:
                raise ValueError('stock_versions_changed_before_start')
            fields = (Path('/proc/self/stat')).read_text().rsplit(')', 1)[1].split()
            job.update(state='applying', pid=os.getpid(), birth=fields[19])
            atomic(path, job)
            completed = subprocess.run([shutil.which('codex'), 'app-server', 'daemon', 'update'],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       env=environment(), timeout=600, check=False)
            job.update(command_succeeded=completed.returncode == 0)
            atomic(path, job)
            after = probe()
            verified = completed.returncode == 0 and service_verified(after)
            job.update(state='complete' if verified else 'attention', at=time.time())
        except Exception:
            job.update(state='attention', at=time.time())
        atomic(path, job)


def dispatch(request, source):
    if request.get('operation') == 'inspect':
        return probe()
    if request.get('operation') == 'update':
        return start(request, source)
    raise ValueError('invalid_stock_version_operation')


if __name__ == '__main__' and len(sys.argv) == 3 and sys.argv[1] == '--worker':
    run_worker(sys.argv[2])
