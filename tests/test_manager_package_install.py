"""Real private worker lifetimes; Windows package installation is never executed."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from manager_core import install_worker
from manager_core.package_install import PackageInstaller, digest, read_record
from manager_core.process_state import process_liveness
from manager_core.store import Store, atomic_json
from manager_core.update_hooks import UpdateHooks
from manager_core.updates import UpdateError, UpdateManager
from test_manager_updates import package


WORKER = r'''
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from manager_core.install_worker import run_job
path, mode = Path(sys.argv[2]), sys.argv[3]
def wait_release():
    deadline = time.monotonic() + 12
    while not path.with_name('release').exists():
        if time.monotonic() > deadline:
            raise RuntimeError('Fixture release deadline')
        time.sleep(.02)
def execute(root, request, dispatching):
    if mode == 'reject':
        raise ValueError('Fixture rejection before dispatch')
    dispatching()
    with path.with_name('install-count.txt').open('a') as stream:
        stream.write('fixture install\n')
    if mode == 'wait':
        wait_release()
    if mode == 'fail':
        raise ValueError('Fixture failure after dispatch')
result = run_job(path, execute=execute)
if mode == 'hold_after':
    wait_release()
sys.exit(result)
'''


@unittest.skipUnless(os.name == 'nt', 'Windows process identities')
class PackageInstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.package = self.root / 'work/control-center/updates/fixture.msix'
        self.package.parent.mkdir(parents=True)
        # Invalid ZIP forces the production worker to reject before any Windows
        # signature/installation command; other tests inject only package work.
        self.package.write_bytes(b'Not a package; never install this fixture.')
        self.transaction = dict(transaction_id=str(uuid4()), installed_before=package('26.903.9818.0'),
                                target=package('26.904.1.0'))
        self.children = []
        self.addCleanup(self.cleanup_workers)

    @property
    def request(self):
        return self.package.parent / 'installations' / self.transaction['transaction_id'] / 'request.json'

    def cleanup_workers(self):
        if self.request.parent.exists():
            self.request.with_name('release').touch()
        for process in self.children:
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                # Only Popen handles created by this test, never user processes.
                process.terminate()
                process.wait(timeout=5)

    def launch(self, path, mode='success'):
        process = subprocess.Popen([sys.executable, '-X', 'utf8', '-c', WORKER,
            str(SCRIPTS), str(path), mode], cwd=self.root, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
        self.children.append(process)
        return process

    def bind(self, reference):
        self.assertTrue(self.request.is_file())
        self.assertFalse(self.request.with_name('receipt.json').exists())
        self.transaction['installer'] = reference

    def run_installer(self, *, mode='success', timeout=10, production=False):
        installer = PackageInstaller(self.root, launcher=None if production else lambda p: self.launch(p, mode),
                                     timeout=timeout)
        installer.run(self.transaction, self.package, digest(self.package), bind=self.bind)
        return installer

    def wait_phase(self, phase, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            proof = PackageInstaller(self.root).inspect(self.transaction)
            if proof.get('installer_phase') == phase:
                return proof
            time.sleep(.02)
        self.fail(f'Worker did not reach {phase}: {proof}')

    def test_completed_worker_receipt_is_read_only_and_cannot_repeat_install(self):
        installer = self.run_installer()
        self.assertEqual(installer.inspect(self.transaction), dict(installer_settled=True,
            installer_phase='succeeded', installer_process_state='exited'))
        files = {p.name: p.read_bytes() for p in self.request.parent.iterdir()}
        for _ in range(3):
            self.assertTrue(PackageInstaller(self.root).inspect(self.transaction)['installer_settled'])
        self.assertEqual(files, {p.name: p.read_bytes() for p in self.request.parent.iterdir()})
        with self.assertRaisesRegex(UpdateError, '이미'):
            self.run_installer()
        duplicate = self.launch(self.request)
        self.assertEqual(duplicate.wait(timeout=10), 125)
        self.assertEqual(self.request.with_name('install-count.txt').read_text().splitlines(), ['fixture install'])

    def test_wait_timeout_keeps_worker_alive_and_late_receipt_is_recoverable(self):
        with self.assertRaises(UpdateError) as raised:
            self.run_installer(mode='wait', timeout=.03)
        self.assertEqual(raised.exception.code, 'installer_running')
        proof = self.wait_phase('dispatching')
        self.assertEqual(proof['installer_process_state'], 'alive')
        self.assertFalse(proof['installer_settled'])
        self.assertIsNone(self.children[-1].poll())
        self.request.with_name('release').touch()
        self.assertEqual(self.children[-1].wait(timeout=10), 0)
        self.assertTrue(PackageInstaller(self.root).inspect(self.transaction)['installer_settled'])

    def test_success_receipt_does_not_settle_until_worker_really_exits(self):
        with self.assertRaises(UpdateError):
            self.run_installer(mode='hold_after', timeout=.03)
        self.assertFalse(self.wait_phase('succeeded')['installer_settled'])
        self.request.with_name('release').touch()
        self.children[-1].wait(timeout=10)
        self.assertTrue(PackageInstaller(self.root).inspect(self.transaction)['installer_settled'])

    def test_default_worker_rejects_invalid_package_before_install(self):
        with self.assertRaises(UpdateError) as raised:
            self.run_installer(production=True)
        self.assertEqual(raised.exception.code, 'installer_preflight_failed')
        proof = PackageInstaller(self.root).inspect(self.transaction)
        self.assertTrue(proof['installer_settled'])
        self.assertEqual(proof['installer_phase'], 'rejected_before_install')

    def test_dispatched_failure_does_not_prove_windows_deployment_is_settled(self):
        with self.assertRaises(UpdateError) as raised:
            self.run_installer(mode='fail')
        self.assertEqual(raised.exception.code, 'installer_result_unknown')
        proof = PackageInstaller(self.root).inspect(self.transaction)
        self.assertEqual(proof['installer_process_state'], 'exited')
        self.assertEqual(proof['installer_phase'], 'failed')
        self.assertFalse(proof['installer_settled'])

    def test_mismatched_or_corrupt_records_never_certify_another_request(self):
        installer = self.run_installer()
        receipt_path = self.request.with_name('receipt.json')
        receipt = read_record(receipt_path)
        for changes in ({'transaction_id': str(uuid4())}, {'nonce': '0' * 64},
                        {'request_sha256': '0' * 64}, {'package_sha256': '0' * 64},
                        {'pid': True}, {'process_created': None}, {'phase': 'unknown'}):
            with self.subTest(changes=changes):
                atomic_json(receipt_path, {**receipt, **changes})
                self.assertFalse(installer.inspect(self.transaction)['installer_settled'])
        atomic_json(receipt_path, receipt)
        for field in ('request_sha256', 'package_sha256', 'job_id'):
            transaction = copy.deepcopy(self.transaction)
            transaction['installer'][field] = '0' * 64
            self.assertFalse(installer.inspect(transaction)['installer_settled'])
        transaction = copy.deepcopy(self.transaction)
        transaction['target']['version'] = '26.904.2.0'
        self.assertFalse(installer.inspect(transaction)['installer_settled'])
        receipt_path.write_text('{broken', encoding='utf-8')
        self.assertFalse(installer.inspect(self.transaction)['installer_settled'])
        receipt_path.unlink()
        self.assertFalse(installer.inspect(self.transaction)['installer_settled'])
        self.assertEqual(installer.inspect({}), {})

    def test_pid_reuse_is_exited_but_inaccessible_process_is_unknown(self):
        self.run_installer()
        self.assertTrue(PackageInstaller(self.root, liveness=lambda _: 'reused').inspect(
            self.transaction)['installer_settled'])
        self.assertFalse(PackageInstaller(self.root, liveness=lambda _: 'unknown').inspect(
            self.transaction)['installer_settled'])

    def test_reference_is_persisted_before_launch_and_binding_failure_never_launches(self):
        launches = []
        def failed_bind(reference):
            self.assertTrue(self.request.is_file())
            raise OSError('Fixture journal write failed')
        installer = PackageInstaller(self.root, launcher=lambda p: launches.append(p))
        with self.assertRaises(OSError):
            installer.run(self.transaction, self.package, digest(self.package), bind=failed_bind)
        self.assertFalse(launches)
        self.assertFalse(self.request.with_name('receipt.json').exists())

    def test_recovery_uses_late_receipt_and_restores_once_without_reinstallation(self):
        with self.assertRaises(UpdateError):
            self.run_installer(mode='wait', timeout=.03)
        self.wait_phase('dispatching')
        restored = []
        profile_id = str(uuid4())
        self.transaction.update(install_outcome='unknown', recovery_required=True, maintenance_state='released',
            closed_profiles=[profile_id], restored_profiles=[], close_intents=[],
            restore_manifest=[dict(profile_id=profile_id)])
        hooks = UpdateHooks(self.root, Store(self.root), object())
        manager = UpdateManager(self.root, inventory=lambda: self.transaction['target'], processes=lambda _: [],
            verify_compatibility=lambda _: {'compatible': True}, verify_recovery=hooks.verify_recovery,
            restore_instance=lambda entry: restored.append(entry['profile_id']) or {'verified': True})
        manager._journal(self.transaction, 'failed_install', 'Fixture wait timeout')
        with patch.object(manager, '_install', side_effect=AssertionError('Installation must never repeat')):
            self.assertEqual(manager.recover()['status'], 'recovery_required')
            self.assertFalse(restored)
            self.request.with_name('release').touch()
            self.children[-1].wait(timeout=10)
            result = manager.recover()
            self.assertEqual(result['status'], 'complete', result)
            self.assertEqual(result['install_outcome'], 'settled_after_recovery')
            self.assertEqual(restored, [profile_id])
            self.assertEqual(manager.recover()['status'], 'complete')
            self.assertEqual(restored, [profile_id])
        self.assertEqual(self.request.with_name('install-count.txt').read_text().splitlines(), ['fixture install'])

    def test_worker_survives_ordinary_parent_exit_and_new_controller_reads_result(self):
        wrapper = self.root / 'fixture-worker.py'
        wrapper.write_text(WORKER, encoding='utf-8')
        transaction_path = self.root / 'fixture-transaction.json'
        atomic_json(transaction_path, self.transaction)
        parent_code = r'''
import json, os, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from manager_core.package_install import PackageInstaller, digest
from manager_core.store import atomic_json
from manager_core.updates import UpdateError
root, package, wrapper, transaction_path = map(Path, sys.argv[2:])
transaction = json.loads(transaction_path.read_text(encoding='utf-8'))
def bind(reference):
    transaction['installer'] = reference
    atomic_json(transaction_path, transaction)
def launch(request):
    return subprocess.Popen([sys.executable, '-X', 'utf8', str(wrapper), sys.argv[1], str(request), 'wait'],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
try:
    PackageInstaller(root, launcher=launch, timeout=.03).run(transaction, package, digest(package), bind=bind)
except UpdateError as error:
    if error.code != 'installer_running':
        raise
marker = package.parent / 'installations' / transaction['transaction_id'] / 'install-count.txt'
deadline = time.monotonic() + 8
while not marker.exists():
    if time.monotonic() > deadline:
        sys.exit(2)
    time.sleep(.02)
os._exit(0)
'''
        parent = subprocess.Popen([sys.executable, '-X', 'utf8', '-c', parent_code, str(SCRIPTS),
            str(self.root), str(self.package), str(wrapper), str(transaction_path)], cwd=self.root,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        self.children.append(parent)
        def finish_orphan():
            if self.request.parent.exists():
                self.request.with_name('release').touch()
                try:
                    receipt = read_record(self.request.with_name('receipt.json'))
                except (OSError, ValueError):
                    return
                identity = {'pid': receipt['pid'], 'created': receipt['process_created']}
                deadline = time.monotonic() + 15
                while process_liveness(identity) == 'alive' and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertIn(process_liveness(identity), ('exited', 'reused'))
        self.addCleanup(finish_orphan)
        self.assertEqual(parent.wait(timeout=10), 0)
        self.transaction = json.loads(transaction_path.read_text(encoding='utf-8'))
        proof = self.wait_phase('dispatching')
        self.assertEqual(proof['installer_process_state'], 'alive')
        self.assertFalse(proof['installer_settled'])
        finish_orphan()
        self.assertTrue(PackageInstaller(self.root).inspect(self.transaction)['installer_settled'])

    def test_package_command_rechecks_bytes_and_has_no_termination_timeout(self):
        request = dict(package_path=str(self.package), package_sha256=digest(self.package),
                       installed_before=self.transaction['installed_before'], target=self.transaction['target'])
        order = []
        with patch.object(install_worker.UpdateManager, '_validate_download', side_effect=lambda *a: order.append('validate')), \
             patch.object(install_worker, '_powershell', side_effect=lambda *a, **k: order.append(('shell', a, k))):
            install_worker.perform_install(self.root, request, lambda: order.append('dispatch'))
        self.assertEqual(order[:2], ['validate', 'dispatch'])
        command, kwargs = order[2][1][0], order[2][2]
        self.assertIn('Get-FileHash -LiteralPath $p', command)
        self.assertIn('Add-AppxPackage -Path $p -ErrorAction Stop', command)
        self.assertEqual(kwargs, {'timeout': None})
        for flag in ('Force', 'AllowUnsigned', 'Defer', 'AllowDowngrade'):
            self.assertNotIn(flag, command)


if __name__ == '__main__':
    unittest.main()
