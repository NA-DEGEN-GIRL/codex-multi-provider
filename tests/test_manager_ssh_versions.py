"""Default CLI inspection and explicitly confirmed update journal, no live updates."""
from contextlib import nullcontext
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from manager_core import ssh_versions
from manager_core.remote import RemoteError

spec = importlib.util.spec_from_file_location('stock_versions_test', ROOT / 'scripts/remote_helpers/stock_versions.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
TOKEN = 'a' * 64


class StockHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        for replacement in (patch.object(helper, 'base', return_value=self.directory),
                            patch.object(helper.os, 'getuid', return_value=0, create=True)):
            replacement.start()
            self.addCleanup(replacement.stop)

    def test_environment_keeps_home_separate_and_does_not_forward_profile_keys(self):
        with patch.dict(os.environ, {'CODEX_HOME': 'managed-profile', 'CODEX_API_KEY': 'private',
                                     'OPENAI_API_KEY': 'private', 'PATH': 'tools'}, clear=True), \
             patch.object(Path, 'home', return_value=self.directory):
            value = helper.environment()
        self.assertEqual(str(self.directory / '.codex'), value['CODEX_HOME'])
        self.assertNotIn('OPENAI_API_KEY', value)
        self.assertNotIn('CODEX_API_KEY', value)
        self.assertEqual('tools', value['PATH'])

    def test_probe_distinguishes_cli_daemon_and_preserves_host_metadata_without_cli(self):
        with patch.object(helper, 'public_job', return_value=None), \
             patch.object(Path, 'read_text', return_value='machine'), \
             patch.object(helper.shutil, 'which', return_value=None):
            result = helper.probe()
        self.assertFalse(result['safe_auto_update'])
        self.assertFalse(result['update_supported'])
        self.assertEqual(64, len(result['host_identity']))
        self.assertNotEqual(result['host_identity'], result['managed_host_identity'])

    def test_current_does_not_claim_latest_upstream_or_automatic_restart(self):
        binary = self.directory / 'codex'
        binary.write_text('fixture')
        info = types.SimpleNamespace(st_mode=0o100755, st_uid=0, st_mtime_ns=123, st_size=7)
        with patch.object(helper, 'public_job', return_value=None), \
             patch.object(helper.shutil, 'which', return_value=str(binary)), \
             patch.object(Path, 'stat', return_value=info), \
             patch.object(Path, 'read_text', return_value='machine'), \
             patch.object(helper, 'command', side_effect=['codex-cli 0.155.1',
                          json.dumps(dict(status='running', appServerVersion='0.155.1')),
                          'codex app-server daemon update']):
            result = helper.probe()
        self.assertEqual('current', result['state'])
        self.assertIn('업데이터가 확인', result['message'])
        self.assertFalse(result['safe_auto_update'])
        self.assertEqual(64, len(result['observation_id']))

    def test_requires_confirmation_and_observation_before_any_mutation(self):
        with patch.object(helper, 'update_lock') as lock, patch.object(helper, 'probe') as probe:
            for request in ({}, {'confirmed': True}, {'confirmed': 1, 'observation_id': TOKEN}):
                with self.assertRaises(ValueError):
                    helper.start(request, '')
        lock.assert_not_called()
        probe.assert_not_called()

    def test_active_or_uncertain_job_is_never_replayed(self):
        for state in ('starting', 'applying', 'attention'):
            with self.subTest(state=state), \
                 patch.object(helper, 'public_job', return_value=dict(state=state)), \
                 patch.object(helper, 'probe', return_value=dict(update_job=dict(state=state))), \
                 patch.object(helper, 'update_lock') as lock, \
                 patch.object(helper.subprocess, 'Popen') as spawn:
                self.assertEqual(state, helper.start(dict(confirmed=True, observation_id=TOKEN), '')['update_job']['state'])
                lock.assert_not_called()
                spawn.assert_not_called()

    def test_stale_observation_cannot_start(self):
        with patch.object(helper, 'public_job', return_value=None), \
             patch.object(helper, 'update_lock', return_value=nullcontext()), \
             patch.object(helper, 'probe', return_value=dict(update_supported=True, observation_id='b' * 64)), \
             patch.object(helper.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(ValueError, 'changed_refresh'):
                helper.start(dict(confirmed=True, observation_id=TOKEN), '')
            spawn.assert_not_called()

    def test_journal_is_committed_before_detached_worker(self):
        writes = []
        observed = dict(update_supported=True, observation_id=TOKEN, version_fingerprint=TOKEN)
        def spawn(*args, **kwargs):
            self.assertEqual(['starting', 'pointer'], writes)
            self.assertTrue(kwargs['start_new_session'])
            self.assertEqual(subprocess.DEVNULL, kwargs['stderr'])
        def atomic(path, value):
            writes.append(value.get('state', 'pointer'))
        with patch.object(helper, 'public_job', side_effect=[None, dict(state='starting')]), \
             patch.object(helper, 'update_lock', return_value=nullcontext()), \
             patch.object(helper, 'probe', return_value=observed), \
             patch.object(helper, 'owned_directory', side_effect=lambda path: path.mkdir(exist_ok=True)), \
             patch.object(helper, 'atomic', side_effect=atomic), \
             patch.object(helper.subprocess, 'Popen', side_effect=spawn) as process:
            helper.start(dict(confirmed=True, observation_id=TOKEN), '# fixture')
            process.assert_called_once()

    def test_lost_worker_or_stale_start_requires_attention(self):
        identity = '00000000-0000-4000-8000-000000000001'
        for job in (dict(id=identity, state='starting', at=time.time() - 35),
                    dict(id=identity, state='applying', pid=999999999)):
            with patch.object(helper, 'read_json', side_effect=[dict(id=identity), job]), \
                 patch.object(helper, 'worker_alive', return_value=False):
                self.assertEqual('attention', helper.public_job()['state'])

    def test_worker_waits_for_parent_lock_and_marks_probe_failure_attention(self):
        job = dict(id='id', state='starting', observation_id=TOKEN, version_fingerprint=TOKEN)
        path = self.directory / 'jobs/id.json'
        with patch.object(helper, 'update_lock', return_value=nullcontext()) as lock, \
             patch.object(helper, 'read_json', return_value=job), \
             patch.object(helper, 'probe', side_effect=ValueError('probe lost')), \
             patch.object(helper, 'atomic') as write, \
             patch.object(helper.subprocess, 'run') as run:
            helper.run_worker(path)
            lock.assert_called_once_with(wait=True)
            run.assert_not_called()
            self.assertEqual('attention', write.call_args.args[1]['state'])

    def test_worker_revalidates_before_official_command(self):
        with patch.object(helper, 'update_lock', return_value=nullcontext()), \
             patch.object(helper, 'read_json', return_value=dict(state='starting', version_fingerprint=TOKEN)), \
             patch.object(helper, 'probe', return_value=dict(version_fingerprint='b' * 64)), \
             patch.object(helper, 'atomic') as write, \
             patch.object(helper.subprocess, 'run') as run:
            helper.run_worker(self.directory / 'jobs/id.json')
            run.assert_not_called()
            self.assertEqual('attention', write.call_args.args[1]['state'])

    def test_successful_command_can_be_verified_later_without_replay(self):
        observed = dict(state='current', cli_version='0.155.1', daemon_version='0.155.1',
                        daemon_state='running', update_job=dict(id='id', state='attention'))
        record = dict(id='id', state='attention', command_succeeded=True)
        with patch.object(helper, 'read_json', return_value=record), \
             patch.object(helper, 'worker_alive', return_value=False), \
             patch.object(helper, 'atomic') as write, \
             patch.object(helper, 'public_job', return_value=dict(id='id', state='complete')), \
             patch.object(helper.subprocess, 'run') as run:
            helper.reconcile_completed_command(observed)
            self.assertEqual('complete', observed['update_job']['state'])
            self.assertEqual('complete', write.call_args.args[1]['state'])
            run.assert_not_called()

    def test_standalone_update_can_advance_independently_of_npm(self):
        observed = dict(cli_version='0.155.1', daemon_version='0.156.0',
                        managed_installation_version='0.156.0', daemon_state='running')
        self.assertTrue(helper.service_verified(observed))
        observed['daemon_version'] = '0.154.0'
        self.assertFalse(helper.service_verified(observed))


class StockTransportTests(unittest.TestCase):
    def remote(self, response):
        remote = Mock()
        remote._alias.side_effect = lambda alias: alias
        remote._run.return_value = types.SimpleNamespace(returncode=0, stdout=response)
        return remote

    def test_probe_uses_login_shell_and_only_bounded_json_reply(self):
        remote = self.remote(b'login banner\n{"ok":true,"result":{"safe_auto_update":false}}\n')
        self.assertFalse(ssh_versions.probe(ROOT, remote, 'test-host')['safe_auto_update'])
        arguments = remote._run.call_args
        self.assertIn('python3', arguments.args[1])
        self.assertIn('inspect', arguments.kwargs['input'].decode())
        self.assertLessEqual(len(arguments.kwargs['input']), 131072)

    def test_transport_rejects_unconfirmed_updates_and_unsafe_claims(self):
        remote = self.remote(b'{"ok":true,"result":{"safe_auto_update":true}}')
        with self.assertRaises(ValueError):
            ssh_versions.update(ROOT, remote, 'test-host', TOKEN, False)
        remote._run.assert_not_called()
        with self.assertRaises(RemoteError):
            ssh_versions.probe(ROOT, remote, 'test-host')

    def test_transport_does_not_expose_remote_error_details(self):
        remote = self.remote(b'{"ok":false,"code":"sensitive-provider-response"}')
        with self.assertRaises(RemoteError) as raised:
            ssh_versions.probe(ROOT, remote, 'test-host')
        self.assertNotIn('sensitive-provider', str(raised.exception))

    def test_transport_rejects_ambiguous_or_oversized_output(self):
        for response in (b'{"ok":false}\n{"ok":true}\n', b'x' * 65537):
            with self.subTest(size=len(response)), self.assertRaises(RemoteError):
                ssh_versions.probe(ROOT, self.remote(response), 'test-host')


if __name__ == '__main__':
    unittest.main()
