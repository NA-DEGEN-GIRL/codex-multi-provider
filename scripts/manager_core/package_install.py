"""One-shot Windows installer worker and durable, process-bound completion proof."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4

from .process_state import process_liveness
from .store import atomic_json, identifier
from .updates import UpdateError, validate_identity


HASH = re.compile(r'[0-9a-f]{64}')
IDENTITY_FIELDS = ('name', 'publisher', 'version', 'architecture', 'family', 'signature_kind', 'package_status')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_record(path):
    path = Path(path)
    if path.is_symlink() or path.resolve(strict=True) != path or path.stat().st_nlink != 1 or path.stat().st_size > 65536:
        raise ValueError('Invalid installer record')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('Invalid installer record')
    return value


def read_request(path, *, verify_worker=False):
    path = Path(path)
    if not path.is_absolute() or path.name != 'request.json':
        raise ValueError('Invalid installer request path')
    job_id = identifier(path.parent.name)
    root = path.parents[5]
    if path != root / 'work/control-center/updates/installations' / job_id / 'request.json':
        raise ValueError('Invalid installer request scope')
    value = read_record(path)
    if (value.get('version') != 1 or value.get('transaction_id') != job_id
            or any(not isinstance(value.get(key), str) or not HASH.fullmatch(value[key])
                   for key in ('nonce', 'package_sha256', 'worker_sha256'))):
        raise ValueError('Invalid installer request identity')
    validate_identity(value['installed_before'])
    validate_identity(value['target'], value['installed_before'])
    package = Path(value['package_path'])
    if not package.is_absolute() or package.parent != root / 'work/control-center/updates':
        raise ValueError('Installer package is outside its managed cache')
    if verify_worker and digest(Path(__file__).with_name('install_worker.py')) != value['worker_sha256']:
        raise ValueError('Installer worker changed')
    return root, value


class PackageInstaller:
    def __init__(self, root, *, launcher=None, liveness=process_liveness, timeout=900):
        self.root = Path(root).resolve()
        self.directory = self.root / 'work/control-center/updates/installations'
        self.launcher = launcher or self._launch
        self.liveness, self.timeout = liveness, timeout

    def _launch(self, request):
        return subprocess.Popen([sys.executable, '-u', str(Path(__file__).with_name('install_worker.py')), str(request)],
            cwd=self.root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))

    def run(self, transaction, package, package_sha256, *, bind):
        job_id = identifier(transaction['transaction_id'])
        path = self.directory / job_id / 'request.json'
        try:
            path.parent.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            raise UpdateError('installer_already_requested', '이 업데이트의 설치 요청이 이미 있습니다. 기존 결과부터 확인합니다.') from None
        request = dict(version=1, transaction_id=job_id, nonce=uuid4().hex + uuid4().hex,
            package_path=str(Path(package).resolve(strict=True)), package_sha256=package_sha256,
            worker_sha256=digest(Path(__file__).with_name('install_worker.py')),
            installed_before={k: transaction['installed_before'][k] for k in IDENTITY_FIELDS if k in transaction['installed_before']},
            target={k: transaction['target'][k] for k in IDENTITY_FIELDS if k in transaction['target']})
        atomic_json(path, request)
        read_request(path, verify_worker=True)
        reference = dict(version=1, job_id=job_id, request_sha256=digest(path), package_sha256=package_sha256)
        bind(reference)  # Persist intent before a worker can dispatch installation.
        process = self.launcher(path)
        try:
            process.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            # Popen.wait does not kill the child. Its receipt can arrive later.
            raise UpdateError('installer_running', 'Windows 설치가 아직 진행 중일 수 있습니다. 기존 설치 결과를 다시 확인해 주세요.') from None
        proof = self.inspect({**transaction, 'installer': reference})
        if proof.get('installer_settled') and proof.get('installer_phase') == 'succeeded':
            return
        if proof.get('installer_settled') and proof.get('installer_phase') == 'rejected_before_install':
            raise UpdateError('installer_preflight_failed', '설치 전 패키지 확인에 실패했습니다. 설치 명령은 실행되지 않았습니다.')
        raise UpdateError('installer_result_unknown', 'Windows 설치 결과를 확인해야 합니다. 설치를 반복하지 않았습니다.')

    def inspect(self, transaction):
        reference = transaction.get('installer')
        if not isinstance(reference, dict):
            return {}
        result = {'installer_settled': False}
        try:
            job_id = identifier(transaction['transaction_id'])
            if reference.get('version') != 1 or reference.get('job_id') != job_id:
                return result
            path = self.directory / job_id / 'request.json'
            root, request = read_request(path)
            if (root != self.root or digest(path) != reference.get('request_sha256')
                    or request['package_sha256'] != reference.get('package_sha256')
                    or any(request[field] != {k: transaction[field][k] for k in IDENTITY_FIELDS if k in transaction[field]}
                           for field in ('installed_before', 'target'))):
                return result
            receipt = read_record(path.with_name('receipt.json'))
            if (receipt.get('version') != 1 or receipt.get('transaction_id') != job_id
                    or receipt.get('nonce') != request['nonce']
                    or receipt.get('request_sha256') != reference['request_sha256']
                    or receipt.get('package_sha256') != reference['package_sha256']
                    or type(receipt.get('pid')) is not int or receipt['pid'] <= 0
                    or type(receipt.get('process_created')) is not int or receipt['process_created'] <= 0):
                return result
            state = self.liveness({'pid': receipt['pid'], 'created': receipt['process_created']})
            phase = receipt.get('phase')
            if phase not in ('validating', 'dispatching', 'succeeded', 'rejected_before_install', 'failed'):
                return result
            result.update(installer_phase=phase, installer_process_state=state)
            if state in ('exited', 'reused') and phase in ('succeeded', 'rejected_before_install'):
                result['installer_settled'] = True
        except (ValueError, RuntimeError, OSError, KeyError, TypeError, IndexError):
            pass
        return result
