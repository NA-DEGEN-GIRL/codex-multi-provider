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
import select
import shutil
import signal
import socket
import stat
import struct
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


def npm_installation():
    """Find the invoking user's actual global package, including shell shims."""
    npm = shutil.which('npm')
    if not npm:
        return None
    try:
        root = Path(command(npm, ['root', '-g']))
        if not root.is_absolute():
            return None
        root = root.resolve(strict=True)
        package = root / '@openai/codex'
        manifest = package / 'package.json'
        if manifest.stat().st_size > MAX_JSON:
            return None
        data = json.loads(manifest.read_text(encoding='utf-8'))
        if data.get('name') != '@openai/codex' or not version(data.get('version')):
            return None
        launcher = package / 'bin/codex.js'
        if not launcher.is_file() or not os.access(package.parent, os.W_OK):
            return None
        return dict(npm=npm, root=str(root), launcher=str(launcher), version=data['version'])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def npm_peer(installation):
    """Pin only the default-home socket's npm Codex peer, never workspace SSH."""
    path = Path.home() / '.codex/app-server-control/app-server-control.sock'
    if not path.exists():
        return None
    if path.is_symlink() or not stat.S_ISSOCK(path.stat().st_mode):
        raise ValueError('stock_socket_untrusted')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(2)
        try:
            connection.connect(str(path))
        except ConnectionRefusedError:
            return None
        pid, uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if uid != os.getuid():
        raise ValueError('stock_socket_owner_changed')
    process = Path('/proc') / str(pid)
    fields = (process / 'stat').read_text().rsplit(')', 1)[1].split()
    executable = os.readlink(process / 'exe')
    installed_root = Path(installation['root']) / '@openai'
    relative = Path(executable.removesuffix(' (deleted)')).relative_to(installed_root)
    # npm atomically renames old packages during installation; a running old
    # executable can legitimately point into .codex-<suffix> (deleted).
    if relative.parts[0] != 'codex' and not re.fullmatch(r'\.codex-[A-Za-z0-9]+', relative.parts[0]):
        raise ValueError('stock_process_not_npm_codex')
    argv = (process / 'cmdline').read_bytes().split(b'\0')
    if b'app-server' not in argv or b'--listen' not in argv:
        raise ValueError('stock_process_not_default_server')
    address = argv[argv.index(b'--listen') + 1]
    if address not in (b'unix://', ('unix://' + str(path)).encode()):
        raise ValueError('stock_process_not_default_server')
    env = dict(item.split(b'=', 1) for item in (process / 'environ').read_bytes().split(b'\0') if b'=' in item)
    if (env.get(b'CODEX_HOME') not in (None, str(Path.home() / '.codex').encode())
            or any(key.startswith(b'CODEX_MANAGER_') for key in env)):
        raise ValueError('stock_process_not_default_home')
    feature_args = []
    for index, item in enumerate(argv[:-1]):
        if item in (b'-c', b'--config') and re.fullmatch(rb'features\.[a-z_]+=(?:true|false)', argv[index + 1]):
            feature_args.extend(['-c', argv[index + 1].decode('ascii')])
    return dict(pid=pid, birth=fields[19], executable=executable,
                feature_args=feature_args,
                boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip())


def npm_plan(cli_version):
    installation = npm_installation()
    if not installation or installation['version'] != cli_version:
        return None
    try:
        peer = npm_peer(installation)
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None
    # A pidfd pins the observed process even if its PID gets recycled.
    if peer and (not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal')):
        return None
    value = dict(installation=installation, peer=peer)
    value['fingerprint'] = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    return value


def npm_update(job, path):
    """An explicitly confirmed npm update, scoped to one default-home peer."""
    plan = npm_plan(job['npm_cli_version'])
    if not plan or plan['fingerprint'] != job['npm_plan_fingerprint']:
        raise ValueError('stock_npm_target_changed')
    installation, peer = plan['installation'], plan['peer']
    descriptor = os.pidfd_open(peer['pid']) if peer else None
    try:
        if npm_peer(installation) != peer:
            raise ValueError('stock_npm_target_changed')
        job.update(step='installing_npm')
        atomic(path, job)
        try:
            target = version(json.loads(command(installation['npm'], ['view', '@openai/codex@latest', 'version', '--json'])))
            if not target:
                raise ValueError('invalid_npm_version')
        except (ValueError, OSError, subprocess.TimeoutExpired):
            raise ValueError('stock_npm_install_failed') from None
        current_version = installation['version']
        core = lambda value: tuple(int(x) for x in value.split('-', 1)[0].split('+', 1)[0].split('.'))
        # A registry/channel can lag a locally installed release. Keep that CLI
        # and only refresh its older daemon, never silently downgrade it.
        newer = core(target) > core(current_version) or (core(target) == core(current_version)
                and '-' in current_version and '-' not in target)
        if newer:
            completed = subprocess.run([installation['npm'], 'install', '--global', '@openai/codex@' + target,
                                        '--no-audit', '--no-fund'], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       env=environment(), timeout=600, check=False)
            if completed.returncode:
                raise ValueError('stock_npm_install_failed')
        refreshed = npm_installation()
        if not refreshed or refreshed['root'] != installation['root']:
            raise ValueError('stock_npm_install_unverified')
        cli = refreshed['launcher']
        installed = version(command(cli, ['--version']))
        if installed != refreshed['version']:
            raise ValueError('stock_npm_install_unverified')
        # Installation can rename the still-running executable, so birth/boot
        # and the socket peer are rechecked instead of the old pathname.
        current = npm_peer(refreshed)
        identity_keys = ('pid', 'birth', 'boot')
        if peer and (not current or any(current[k] != peer[k] for k in identity_keys)):
            raise ValueError('stock_npm_target_changed')
        if not peer and current:
            raise ValueError('stock_npm_target_changed')
        job.update(step='restarting_terminal', installed_version=installed)
        atomic(path, job)
        if descriptor is not None:
            signal.pidfd_send_signal(descriptor, signal.SIGTERM)
            if not select.select([descriptor], [], [], 30)[0]:
                raise ValueError('stock_npm_exit_pending')
        job.update(step='starting_terminal', old_process_exited=True)
        atomic(path, job)
        # Standard Codex socket/startup locking serializes with a terminal that
        # happens to open during this transition. No workspace profile is used.
        child = subprocess.Popen([cli, *(peer or {}).get('feature_args', []), 'app-server', '--listen', 'unix://'],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 env=environment(), start_new_session=True, close_fds=True)
        job.update(started_pid=child.pid)
        atomic(path, job)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            try:
                running = npm_peer(refreshed)
                observed = json.loads(command(cli, ['app-server', 'daemon', 'version']))
                if running and version(observed.get('appServerVersion')) == installed:
                    job.update(command_succeeded=True, command_result=dict(status='updated',
                        installed_version=installed, running_version=installed), step='verified', state='complete')
                    return
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
            time.sleep(.2)
        raise ValueError('stock_npm_start_unverified')
    finally:
        if descriptor is not None:
            os.close(descriptor)


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
                'unsupported': '현재 설치 방식에서는 터미널 Codex 자동 업데이트를 지원하지 않습니다.',
                'failed': '터미널 Codex 업데이트를 적용하지 못했습니다. 설치·연결 상태를 다시 확인해 주세요.',
                'attention': '업데이트 완료를 확인하지 못했습니다. 중복 실행하지 않고 확인을 기다립니다.'}
    return dict(id=identity, state=state, message=messages.get(state, '업데이트 상태 확인 필요'),
                step=job.get('step'), code=job.get('code'),
                verification_pending=state == 'attention' and (job.get('command_succeeded') is True or
                    (job.get('update_mode') == 'npm' and job.get('step') == 'starting_terminal')))


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
    try:
        result['cli_version'] = version(command(cli, ['--version']))
        observed = json.loads(command(cli, ['app-server', 'daemon', 'version']))
        if not isinstance(observed, dict):
            raise ValueError('invalid_stock_version_response')
    except (OSError, ValueError, subprocess.TimeoutExpired):
        result['message'] = '이 기본 Codex 버전은 서비스 상태 확인을 지원하지 않습니다.'
        return result
    result['daemon_version'] = version(observed.get('appServerVersion'))
    result['managed_installation_version'] = version(observed.get('managedCodexVersion'))
    result['daemon_state'] = str(observed.get('status', 'unknown'))[:64]
    try:
        help_text = command(cli, ['app-server', 'daemon', 'update', '--help'])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        help_text = ''
    # --help only proves the command exists. npm-only or app-owned daemons may
    # return {status: "unsupported"} with exit code zero without updating.
    result['update_supported'] = ('daemon update' in help_text and bool(result['cli_version'])
                                  and bool(result['managed_installation_version']))
    if not result['managed_installation_version']:
        expected = Path.home() / '.codex/packages/standalone/current/codex'
        result['update_block_reason'] = ('standalone_missing' if
            observed.get('managedCodexPath') == str(expected) and not expected.exists()
            else 'standalone_unavailable')
    elif not result['update_supported']:
        result['update_block_reason'] = 'updater_unavailable'
    result['standalone_missing'] = result.get('update_block_reason') == 'standalone_missing'
    plan = npm_plan(result['cli_version'])
    if plan:
        result.update(update_mode='npm', update_supported=True, npm_plan_fingerprint=plan['fingerprint'])
        result.pop('update_block_reason', None)
    else:
        result['update_mode'] = 'standalone'
    result['version_fingerprint'] = hashlib.sha256(json.dumps([
        identity, str(cli), str(cli.resolve()), info.st_mtime_ns, info.st_size,
        result['cli_version'], result['daemon_version'], result['daemon_state'],
        result['managed_installation_version'], result.get('update_block_reason'),
        result['update_mode'], result.get('npm_plan_fingerprint')], separators=(',', ':')).encode()).hexdigest()
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
    if (result.get('update_job') or {}).get('state') == 'unsupported':
        # A changed installation can be checked again using a fresh observation.
        record = read_json(base() / 'jobs' / (result['update_job']['id'] + '.json'))
        if (record and record.get('update_mode', 'standalone') == result['update_mode']
                and record.get('version_fingerprint') == result['version_fingerprint']):
            result.update(update_supported=False)
            result.setdefault('update_block_reason', 'installation_unsupported')
    return result


def reconcile_completed_command(observed):
    job = observed.get('update_job')
    if not job or job['state'] != 'attention':
        return
    path = base() / 'jobs' / (job['id'] + '.json')
    try:
        with update_lock():
            record = read_json(path)
            # Legacy helpers discarded stdout, including official "unsupported"
            # replies. A missing standalone install and an exited, successful
            # command settle that case without invoking any lifecycle command.
            if not record or worker_alive(record):
                return
            npm_started = record.get('update_mode') == 'npm' and record.get('step') == 'starting_terminal'
            if record.get('command_succeeded') is not True and not npm_started:
                return
            if service_verified(observed) and (not npm_started or
                    observed.get('daemon_version') == record.get('installed_version')):
                state = 'complete'
            elif (observed.get('update_block_reason') == 'standalone_missing'
                    or observed.get('standalone_missing')) and record.get('update_mode', 'standalone') == 'standalone':
                state = 'unsupported'
            else:
                return
            record.update(state=state, at=time.time())
            atomic(path, record)
            observed['update_job'] = public_job()
    except BlockingIOError:
        return


def update_result(output):
    """Retain only documented semantic result fields, never raw paths/errors."""
    try:
        value = json.loads(output)
        if not isinstance(value, dict) or value.get('status') not in ('updated', 'noUpdate', 'unsupported'):
            return None
        return dict(status=value['status'], installed_version=version(value.get('installedVersion')),
                    running_version=version(value.get('runningVersion')))
    except (ValueError, UnicodeError, TypeError):
        return None


def service_verified(observed):
    # Official update may advance the standalone install independently of npm.
    expected = (observed.get('cli_version') if observed.get('update_mode') == 'npm' else
                observed.get('managed_installation_version') or observed.get('cli_version'))
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
                   version_fingerprint=observed['version_fingerprint'], update_mode=observed.get('update_mode', 'standalone'))
        if job['update_mode'] == 'npm':
            job.update(npm_cli_version=observed['cli_version'], npm_plan_fingerprint=observed['npm_plan_fingerprint'])
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
            if job.get('update_mode') == 'npm':
                npm_update(job, path)
                job['at'] = time.time()
                atomic(path, job)
                return
            with tempfile.TemporaryFile() as output:
                completed = subprocess.run([shutil.which('codex'), 'app-server', 'daemon', 'update'],
                                           stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
                                           env=environment(), timeout=600, check=False)
                output.seek(0)
                response = output.read(MAX_JSON + 1)
            result = update_result(response) if len(response) <= MAX_JSON else None
            job.update(command_succeeded=completed.returncode == 0, command_result=result)
            atomic(path, job)
            if completed.returncode == 0 and result and result['status'] == 'unsupported':
                job.update(state='unsupported', at=time.time())
                atomic(path, job)
                return
            after = probe()
            verified = (completed.returncode == 0 and result is not None
                        and result['status'] in ('updated', 'noUpdate') and service_verified(after))
            job.update(state='complete' if verified else 'attention', at=time.time())
        except Exception as error:
            code = str(error)
            allowed = {'stock_npm_target_changed', 'stock_npm_install_failed', 'stock_npm_install_unverified',
                       'stock_npm_exit_pending', 'stock_npm_start_unverified'}
            job.update(state='failed' if code in ('stock_npm_target_changed', 'stock_npm_install_failed',
                       'stock_npm_install_unverified') else 'attention',
                       code=code if code in allowed else 'stock_update_unverified', at=time.time())
        atomic(path, job)


def dispatch(request, source):
    if request.get('operation') == 'inspect':
        return probe()
    if request.get('operation') == 'update':
        return start(request, source)
    raise ValueError('invalid_stock_version_operation')


if __name__ == '__main__' and len(sys.argv) == 3 and sys.argv[1] == '--worker':
    run_worker(sys.argv[2])
