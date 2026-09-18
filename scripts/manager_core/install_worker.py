"""Run one verified installer request, retaining a receipt if its parent exits."""
from pathlib import Path
import os
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from manager_core.instances import process_identity
from manager_core.package_install import digest, read_request
from manager_core.store import atomic_json
from manager_core.updates import UpdateManager, _lock_file, _unlock_file, _powershell, _ps_quote


def perform_install(root, request, dispatching):
    package = Path(request['package_path'])
    UpdateManager(root)._validate_download(package, request['package_sha256'], request['installed_before'], request['target'])
    dispatching()
    # Keep the final byte check in the same PowerShell process that invokes the
    # official cmdlet. No force-close, downgrade, unsigned or deferred options.
    _powershell('$p=' + _ps_quote(str(package)) + '; '
        'if((Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash.ToLower() -ne '
        + _ps_quote(request['package_sha256']) + "){throw 'Package changed'}; "
        'Add-AppxPackage -Path $p -ErrorAction Stop', timeout=None)


def run_job(path, *, execute=perform_install):
    path = Path(path).resolve(strict=True)
    root, request = read_request(path, verify_worker=True)
    claim = _lock_file(path.with_name('worker.lock'))
    try:
        receipt_path = path.with_name('receipt.json')
        # A started request is never replayed, even after a worker crash.
        if receipt_path.exists():
            return 125
        identity = process_identity(os.getpid())
        if not identity or not identity.get('process_created'):
            return 125
        receipt = dict(version=1, transaction_id=request['transaction_id'], nonce=request['nonce'],
            request_sha256=digest(path), package_sha256=request['package_sha256'],
            pid=os.getpid(), process_created=identity['process_created'], phase='validating')
        atomic_json(receipt_path, receipt)
        dispatched = False
        def dispatching():
            nonlocal dispatched
            receipt['phase'] = 'dispatching'
            atomic_json(receipt_path, receipt)
            dispatched = True
        try:
            execute(root, request, dispatching)
            if not dispatched:
                raise RuntimeError('Installer dispatch was not recorded')
            receipt['phase'] = 'succeeded'
            atomic_json(receipt_path, receipt)
            return 0
        except Exception:
            # Cmdlet errors after dispatch do not prove deployment service exit.
            receipt['phase'] = 'failed' if dispatched else 'rejected_before_install'
            atomic_json(receipt_path, receipt)
            return 1
    finally:
        _unlock_file(claim)


if __name__ == '__main__':
    try:
        sys.exit(run_job(sys.argv[1]))
    except Exception:
        sys.exit(125)
