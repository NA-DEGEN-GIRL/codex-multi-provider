"""Isolated package updates with synthetic desktops; no real installer or SSH."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from manager_core import managed_package_update as separate
from manager_core import updates
from manager_core.update_jobs import UpdateJobs
import test_manager_updates as fixtures


class IsolatedPackageTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.UpdateFixtures()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.patched_execution()
        self.manager = self.case.manager
        self.identity = dict(revision=22, patch='fixture', adapters={'adapter': 'fixture'})
        adapter = patch.object(separate.desktop_bundle, 'adapter_identity', return_value=self.identity)
        adapter.start()
        self.addCleanup(adapter.stop)
        self.desktop = self.make_copy('current', '26.917.1.0', self.identity)
        self.case.live = [self.process(self.desktop, 100)]
        self.original_live = deepcopy(self.case.live)
        for field in ('snapshot_instances', 'close_instance', 'restore_instance',
                      'acquire_maintenance', 'release_maintenance', 'remote_snapshot',
                      'verify_compatibility', 'verify_recovery'):
            setattr(self.manager, field, self.forbidden)

    def forbidden(self, *args, **kwargs):
        self.fail('Isolated package installation contacted profile/SSH maintenance')

    def make_copy(self, directory, version, identity):
        target = self.case.root / 'artifacts/managed-desktop' / directory
        (target / 'resources').mkdir(parents=True)
        files = ('ChatGPT.exe', 'chrome.dll', 'resources/app.asar')
        for name in files:
            (target / name).write_bytes(('fixture ' + name).encode())
        marker = dict(source={**deepcopy(identity), 'version': version}, files={}, hashes={})
        for name in files:
            path = target / name
            stat = path.stat()
            marker['files'][name] = dict(size=stat.st_size, modified=stat.st_mtime_ns)
            marker['hashes'][name] = hashlib.sha256(path.read_bytes()).hexdigest()
        (target / separate.MARKER).write_text(json.dumps(marker), encoding='utf-8')
        return target

    @staticmethod
    def process(directory, pid):
        return dict(process_id=pid, parent_process_id=0,
                    executable=str(directory / 'ChatGPT.exe'), created_at='2026-10-04T00:00:00Z')

    def test_install_preserves_running_profiles_without_idle_or_new_ui_compatibility(self):
        original = {p: p.read_bytes() for p in self.desktop.rglob('*') if p.is_file()}
        plan = self.manager.plan()
        self.assertEqual(plan['mode'], separate.MODE)
        self.assertEqual(plan['status'], 'ready', plan)
        self.assertEqual(plan['restore_manifest'], [])
        result = self.manager.apply(plan)
        self.assertEqual(result['status'], 'complete', result)
        self.assertTrue(result['profiles_preserved'])
        self.assertEqual(result['managed_versions'], ['26.917.1.0'])
        self.assertIn('별도 호환 지원', result['message'])
        self.assertEqual(self.case.live, self.original_live)
        self.assertEqual(len(self.case.installs), 1)
        self.assertFalse(self.case.closed or self.case.restored)
        self.assertEqual(original, {p: p.read_bytes() for p in original})
        self.assertEqual(self.manager.apply(plan)['code'], 'plan_not_ready')

    def test_old_adapter_copy_can_continue_while_current_fallback_is_ready(self):
        old = self.make_copy('old', '26.915.1.0', dict(revision=21, patch='old', adapters={}))
        self.case.live.append(self.process(old, 101))
        plan = self.manager.plan()
        self.assertEqual(plan['status'], 'ready', plan)
        self.assertEqual(plan['managed_versions'], ['26.915.1.0', '26.917.1.0'])
        self.assertEqual(self.manager.apply(plan)['status'], 'complete')

    def test_original_package_window_blocks_without_closing_it(self):
        self.case.live.append(dict(process_id=200, executable=str(
            Path(self.case.installed['install_location']) / 'app/ChatGPT.exe')))
        plan = self.manager.plan()
        self.assertEqual(plan['status'], 'blocked')
        self.assertEqual(plan['blockers'][0]['code'], 'package_in_use')
        self.assertEqual(self.manager.apply(plan)['code'], 'plan_not_ready')
        self.assertFalse(self.case.installs or self.case.closed)

    def test_another_accounts_process_does_not_block_and_is_never_closed(self):
        # Even the limited image query was denied: another account's codex.exe.
        # The installer never closes applications, so it only defers if busy.
        self.case.live.append(dict(process_id=200, executable=None))
        plan = self.manager.plan()
        self.assertEqual(plan['status'], 'ready', plan)
        self.assertFalse(self.case.closed)

    def test_publication_scan_race_returns_a_blocker_without_contacting_ssh(self):
        with patch.object(separate.desktop_publication, '_fallback', side_effect=OSError('file changed')):
            plan = self.manager.plan()
        self.assertEqual(plan['status'], 'blocked')
        self.assertEqual(plan['blockers'][0]['code'], 'managed_copy_unverified')

    def test_foreign_desktop_is_not_treated_as_a_verified_managed_copy(self):
        foreign = self.case.root / 'unmanaged'
        self.case.live.append(self.process(foreign, 200))
        self.assertEqual(self.manager.plan()['blockers'][0]['code'], 'unmanaged_instance')

    def test_corrupt_live_old_copy_blocks_even_with_valid_current_fallback(self):
        old = self.make_copy('old', '26.915.1.0', dict(revision=21, patch='old', adapters={}))
        (old / 'resources/app.asar').write_bytes(b'bad')
        self.case.live.append(self.process(old, 200))
        self.assertEqual(self.manager.plan()['blockers'][0]['code'], 'managed_copy_unverified')

    def test_copy_change_after_plan_blocks_installation(self):
        plan = self.manager.plan()
        marker = self.desktop / separate.MARKER
        value = json.loads(marker.read_text())
        value['diagnostic'] = 'changed after planning'
        marker.write_text(json.dumps(value), encoding='utf-8')
        result = self.manager.apply(plan)
        self.assertEqual(result['status'], 'blocked', result)
        self.assertEqual(result['blockers'][0]['code'], 'managed_copy_changed')
        self.assertFalse(self.case.installs)

    def test_original_package_start_during_download_blocks_installation(self):
        download = self.manager._download
        def opened(latest):
            result = download(latest)
            self.case.live.append(dict(process_id=200, executable=str(
                Path(self.case.installed['install_location']) / 'app/ChatGPT.exe')))
            return result
        with patch.object(self.manager, '_download', opened):
            result = self.manager.apply(self.manager.plan())
        self.assertEqual(result['status'], 'blocked', result)
        self.assertEqual(result['blockers'][0]['code'], 'package_in_use')
        self.assertFalse(self.case.installs)

    def test_package_change_during_download_blocks_installation(self):
        download = self.manager._download
        def changed(latest):
            result = download(latest)
            self.case.installed['version'] = '26.904.0.0'
            return result
        with patch.object(self.manager, '_download', changed):
            result = self.manager.apply(self.manager.plan())
        self.assertEqual(result['status'], 'blocked', result)
        self.assertFalse(self.case.installs)

    def test_signature_failure_never_reaches_installer(self):
        with patch.object(self.manager, '_validate_download', side_effect=updates.UpdateError('untrusted_signature', 'bad signature')):
            result = self.manager.apply(self.manager.plan())
        self.assertEqual(result['code'], 'untrusted_signature')
        self.assertEqual(result['status'], 'failed_before_install')
        self.assertFalse(self.case.installs or self.case.closed)

    def test_unknown_install_is_reconciled_without_reinstall_or_compatibility_rpc(self):
        def uncertain(*args):
            self.case.installed = deepcopy(self.case.latest)
            raise updates.UpdateError('installer_result_unknown', 'unknown')
        with patch.object(self.manager, '_install', uncertain), patch.object(self.manager.installer, 'inspect', return_value={}):
            result = self.manager.apply(self.manager.plan())
        self.assertEqual(result['status'], 'recovery_required')
        self.assertTrue(result['recovery_required'])
        with patch.object(self.manager.installer, 'inspect', return_value={}):
            self.assertEqual(self.manager.recover()['status'], 'recovery_required')
        with patch.object(self.manager.installer, 'inspect', return_value={'installer_settled': True}):
            recovered = self.manager.recover()
        self.assertEqual(recovered['status'], 'complete', recovered)
        self.assertFalse(recovered['install_retried'])
        self.assertFalse(self.case.installs or self.case.closed or self.case.restored)

    def test_successful_install_with_failed_preservation_check_is_not_repeated(self):
        install = self.manager._install
        def changed(*args):
            install(*args)
            (self.desktop / 'resources/app.asar').write_bytes(b'changed during installation')
        plan = self.manager.plan()
        with patch.object(self.manager, '_install', changed):
            result = self.manager.apply(plan)
        self.assertEqual(result['status'], 'recovery_required', result)
        self.assertTrue(result['recovery_required'])
        self.assertEqual(self.manager.apply(plan)['code'], 'plan_not_ready')
        self.assertEqual(len(self.case.installs), 1)
        self.assertFalse(self.case.closed or self.case.restored)

    def test_failed_installer_old_version_can_be_reconciled_without_retry(self):
        with patch.object(self.manager, '_install', side_effect=updates.UpdateError('installer_result_unknown', 'unknown')):
            result = self.manager.apply(self.manager.plan())
        self.assertTrue(result['recovery_required'])
        with patch.object(self.manager.installer, 'inspect', return_value={'installer_settled': True}):
            recovered = self.manager.recover()
        self.assertEqual(recovered['status'], 'failed_install', recovered)
        self.assertFalse(recovered['recovery_required'])
        self.assertFalse(recovered['install_retried'])

    def test_worker_does_not_snapshot_remote_jobs_before_planning(self):
        pending = []
        jobs = UpdateJobs(self.manager, spawn=pending.append)
        self.addCleanup(jobs.shutdown)
        jobs.schedule()
        pending.pop()()
        result = jobs.status()
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(len(self.case.installs), 1)

    def test_deferred_registration_is_not_reported_as_complete_or_installed_again(self):
        def defer(*args):
            self.case.installs.append(True)
        with patch.object(self.manager, '_install', defer):
            plan = self.manager.plan()
            result = self.manager.apply(plan)
            self.assertEqual(result['status'], 'registration_pending', result)
            self.assertEqual(result['observed_installed']['version'], self.case.installed['version'])
            self.assertEqual(self.manager.apply(plan)['code'], 'plan_not_ready')
            jobs = UpdateJobs(self.manager)
            self.assertEqual(jobs.check()['status'], 'registration_pending')
            self.case.installed = deepcopy(self.case.latest)
            self.assertEqual(jobs.check()['status'], 'complete')
        self.assertEqual(len(self.case.installs), 1)

    def test_typed_busy_rejection_is_settled_without_reinstalling(self):
        with patch.object(self.manager, '_install', side_effect=updates.UpdateError('package_in_use', 'busy')), \
             patch.object(self.manager.installer, 'inspect', return_value={'installer_settled': True, 'installer_result_code': 'package_in_use'}):
            self.manager.apply(self.manager.plan())
            recovered = self.manager.recover()
        self.assertEqual(recovered['status'], 'failed_install', recovered)
        self.assertFalse(recovered['recovery_required'])
        self.assertFalse(self.case.installs)

    def prepare_pending(self):
        with patch.object(self.manager, '_install'):
            result = self.manager.apply(self.manager.plan())
        self.assertEqual(result['status'], 'registration_pending')
        return result

    def test_explicit_finalize_uses_new_receipt_and_finishes_without_original_app(self):
        pending = self.prepare_pending()
        install = self.manager._install
        contexts = []
        def record(*args):
            contexts.append(deepcopy(self.manager._install_transaction))
            install(*args)
        with patch.object(self.manager, '_install', record):
            result = separate.finalize(self.manager)
        self.assertEqual(result['status'], 'complete', result)
        self.assertNotEqual(result['transaction_id'], pending['transaction_id'])
        self.assertEqual(contexts[0]['parent_transaction_id'], pending['transaction_id'])
        self.assertFalse(contexts[0]['defer_registration'])
        self.assertEqual(self.case.live, self.original_live)
        self.assertFalse(self.case.closed or self.case.restored)

    def test_finalize_rejected_by_windows_remains_actionable_without_retry_loop(self):
        self.prepare_pending()
        with patch.object(self.manager, '_install', side_effect=updates.UpdateError('package_in_use', 'busy')) as install, \
             patch.object(self.manager.installer, 'inspect', return_value={
                 'installer_settled': True, 'installer_result_code': 'package_in_use'}):
            result = separate.finalize(self.manager)
            self.assertEqual(result['status'], 'registration_pending', result)
            self.assertEqual(self.manager.recover()['status'], 'registration_pending')
            self.assertEqual(install.call_count, 1)

    def test_finalize_never_repeats_uncertain_install(self):
        self.prepare_pending()
        with patch.object(self.manager, '_install', side_effect=updates.UpdateError('installer_result_unknown', 'unknown')) as install, \
             patch.object(self.manager.installer, 'inspect', return_value={}):
            self.assertEqual(separate.finalize(self.manager)['status'], 'recovery_required')
            self.assertEqual(separate.finalize(self.manager)['status'], 'recovery_required')
            self.assertEqual(install.call_count, 1)

    def test_pending_job_button_finalizes_but_read_only_check_does_not(self):
        self.prepare_pending()
        pending = []
        jobs = UpdateJobs(self.manager, spawn=pending.append)
        self.assertEqual(jobs.check()['status'], 'registration_pending')
        self.assertEqual(self.case.installs, [])
        jobs.schedule()
        pending.pop()()
        self.assertEqual(jobs.status()['status'], 'complete')
        self.assertEqual(len(self.case.installs), 1)

    def test_process_inventory_includes_older_official_package_instances(self):
        old = dict(process_id=400, executable=r'C:\Program Files\WindowsApps\OpenAI.Codex_1.2.3.4_x64__2p2nqsd0c76g0\app\ChatGPT.exe')
        with patch.object(updates, '_powershell', return_value=json.dumps([old])):
            self.assertEqual(updates.package_processes(self.case.installed), [old])

    def test_windows_powershell_does_not_inherit_another_editions_module_path(self):
        inherited = {'PSModulePath': r'C:\Program Files\PowerShell\7\Modules',
                     'TASK_ENV': 'preserved'}
        with patch.dict(os.environ, inherited, clear=True), \
             patch.object(updates.shutil, 'which', return_value='powershell.exe'), \
             patch.object(updates.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='ok')) as run:
            self.assertEqual(updates._powershell('Get-AuthenticodeSignature'), 'ok')
            environment = run.call_args.kwargs['env']
            self.assertEqual(environment, {'TASK_ENV': 'preserved'})
            self.assertEqual(os.environ['PSModulePath'], inherited['PSModulePath'])
            self.assertIn('-NoProfile', run.call_args.args[0])

    def test_signature_verification_allows_time_for_windows_certificate_checks(self):
        path, digest = self.case.fake_download(self.case.latest)
        # Call the real class method, not the fixture's replaced validator.
        with patch.object(updates, '_powershell', return_value=json.dumps({'status': 'Valid', 'signer': 'fixture'})) as ps:
            updates.UpdateManager._validate_download(self.manager, path, digest, self.case.installed, self.case.latest)
        self.assertEqual(ps.call_args.kwargs['timeout'], 120)


if __name__ == '__main__':
    unittest.main()
