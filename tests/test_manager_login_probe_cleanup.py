"""Disposable quota-probe homes are removed without following links or live homes."""
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import warnings
from unittest.mock import Mock, call, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core import login_probe


REPLIES = ({'id': 1, 'result': {}}, {'id': 2, 'result': {}},
           {'id': 3, 'result': {'account': {'type': 'chatgpt'}}},
           {'id': 4, 'result': {'rateLimits': {'primary': {'usedPercent': 10, 'windowDurationMins': 300}}}})


class FakeAppServer:
    """Answers the probe's four requests and leaves app-server-like state behind."""
    def __init__(self, home, *, exits=True, auth_file=False):
        (home / 'state_5.sqlite').write_bytes(b'db')
        (home / 'state_5.sqlite-wal').write_bytes(b'wal')
        pack = home / '.tmp/plugins-clone-fixture/.git/objects/pack/tmp_pack_fixture'
        pack.parent.mkdir(parents=True)
        pack.write_bytes(b'pack')
        os.chmod(pack, stat.S_IREAD)  # git leaves pack files read-only
        if auth_file:
            (home / 'auth.json').write_text('{}', encoding='utf-8')
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(b''.join(json.dumps(reply).encode() + b'\n' for reply in REPLIES))
        self.exits, self.returncode = exits, None

    def wait(self, timeout=None):
        if not self.exits:
            raise subprocess.TimeoutExpired('fixture', timeout)
        self.returncode = 0
        return 0

    def poll(self):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


def make_junction(link, target):
    if os.name != 'nt':
        raise unittest.SkipTest('junctions are Windows-only')
    import _winapi
    _winapi.CreateJunction(str(target), str(link))


class ProbeCleanupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.base = self.root / 'work/control-center/auth-probes'
        self.tokens = SimpleNamespace(login_params=lambda: {'type': 'fixture'})

    def run_probe(self, **server):
        servers = []
        def start(command, *, cwd, env, **_):
            self.assertEqual(Path(env['CODEX_HOME']), Path(cwd))
            servers.append(FakeAppServer(Path(cwd), **server))
            return servers[-1]
        with patch.object(login_probe.subprocess, 'Popen', side_effect=start):
            return login_probe.verify(self.root, 'codex.exe', self.tokens, timeout=5)

    def probe_homes(self):
        return list(self.base.iterdir()) if self.base.exists() else []

    def test_exited_probe_home_is_removed_including_read_only_files(self):
        result = self.run_probe()
        self.assertTrue(result['quota_read'])
        self.assertEqual(result['usage']['windows'][0]['remaining_percent'], 90)
        self.assertEqual(self.probe_homes(), [])

    def test_auth_file_check_runs_before_the_home_is_removed(self):
        with self.assertRaisesRegex(RuntimeError, '파일로 저장'):
            self.run_probe(auth_file=True)
        self.assertEqual(self.probe_homes(), [])

    def test_home_of_a_process_that_did_not_exit_is_left_for_the_purge(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_probe(exits=False)
        homes = self.probe_homes()
        self.assertEqual(len(homes), 1)
        self.assertTrue((homes[0] / 'state_5.sqlite').is_file())
        for path in homes[0].rglob('*'):  # let the temporary directory clean up
            os.chmod(path, stat.S_IWRITE)

    def test_failed_start_removes_the_new_home(self):
        with patch.object(login_probe.subprocess, 'Popen', side_effect=OSError('fixture')):
            with self.assertRaisesRegex(OSError, 'fixture'):
                login_probe.verify(self.root, 'missing.exe', self.tokens)
        self.assertEqual(self.probe_homes(), [])

    def test_only_uuid_children_of_the_probe_area_are_removed(self):
        outside = self.root / 'work/control-center' / str(uuid4())
        rejected = [self.base / 'not-a-probe', self.base / str(uuid4()).upper(),
                    self.base / str(uuid4()) / str(uuid4()), outside]
        for path in rejected:
            path.mkdir(parents=True)
            (path / 'keep').write_bytes(b'keep')
            self.assertFalse(login_probe._discard(self.root, path))
            self.assertTrue((path / 'keep').is_file())
        plain = self.base / str(uuid4())
        plain.write_bytes(b'not a directory')
        self.assertFalse(login_probe._discard(self.root, plain))
        self.assertTrue(plain.is_file())

    def test_linked_probe_home_is_refused_and_inner_junction_is_not_followed(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'keep.txt').write_bytes(b'keep')
        self.base.mkdir(parents=True)
        linked = self.base / str(uuid4())
        make_junction(linked, outside)
        self.assertFalse(login_probe._discard(self.root, linked))
        self.assertTrue(linked.exists())
        home = self.base / str(uuid4())
        home.mkdir()
        make_junction(home / 'inner', outside)
        self.assertTrue(login_probe._discard(self.root, home))
        self.assertFalse(home.exists())
        self.assertEqual((outside / 'keep.txt').read_bytes(), b'keep')

    def test_briefly_locked_home_is_retried_then_left(self):
        home = self.base / str(uuid4())
        home.mkdir(parents=True)
        with patch.object(login_probe.time, 'sleep') as sleep, \
             patch.object(login_probe.shutil, 'rmtree',
                          side_effect=[PermissionError('locked'), PermissionError('locked'), None]) as remove:
            self.assertTrue(login_probe._discard(self.root, home))
            self.assertEqual(remove.call_count, 3)
            self.assertEqual(sleep.call_count, 2)
        with patch.object(login_probe.time, 'sleep'), \
             patch.object(login_probe.shutil, 'rmtree', side_effect=PermissionError('locked')):
            self.assertFalse(login_probe._discard(self.root, home))
        self.assertTrue(home.is_dir())

    def test_unexpected_removal_error_never_replaces_the_probe_result(self):
        with patch.object(login_probe.time, 'sleep') as sleep, \
             patch.object(login_probe.shutil, 'rmtree', side_effect=TypeError('onexc')) as remove:
            result = self.run_probe()
        self.assertTrue(result['quota_read'])
        self.assertEqual((remove.call_count, sleep.call_count), (1, 0))  # not transient, not retried
        homes = self.probe_homes()
        self.assertEqual(len(homes), 1)
        for path in homes[0].rglob('*'):  # let the temporary directory clean up
            os.chmod(path, stat.S_IWRITE)

    def test_python_311_rmtree_uses_onerror_and_still_clears_read_only_files(self):
        real = shutil.rmtree
        def rmtree_311(path, **options):
            if 'onexc' in options:
                raise TypeError("rmtree() got an unexpected keyword argument 'onexc'")
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', DeprecationWarning)  # onerror is deprecated on 3.12+
                return real(path, **options)
        with patch.object(login_probe, '_ONEXC', False), \
             patch.object(login_probe.shutil, 'rmtree', side_effect=rmtree_311) as remove:
            result = self.run_probe()
        self.assertTrue(result['quota_read'])
        self.assertIn('onerror', remove.call_args.kwargs)
        self.assertEqual(self.probe_homes(), [])


class StaleProbePurgeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.base = self.root / 'work/control-center/auth-probes'
        self.base.mkdir(parents=True)

    def clock(self, offset):
        now = time.time() + offset
        return patch.object(login_probe, 'time', Mock(time=Mock(return_value=now), sleep=Mock()))

    def test_only_old_uuid_homes_are_removed_with_a_pause_after_each(self):
        stale = [self.base / str(uuid4()) for _ in range(2)]
        for home in stale:
            (home / 'logs').mkdir(parents=True)
            (home / 'logs/logs_2.sqlite').write_bytes(b'db')
        os.chmod(stale[0] / 'logs/logs_2.sqlite', stat.S_IREAD)
        notes = self.base / 'notes'
        notes.mkdir()
        named_file = self.base / str(uuid4())
        named_file.write_bytes(b'file')
        with self.clock(0) as clock:
            self.assertEqual(login_probe.purge_stale_probes(self.root), 0)
        clock.sleep.assert_not_called()
        self.assertTrue(all(home.is_dir() for home in stale))
        with self.clock(7200) as clock:
            self.assertEqual(login_probe.purge_stale_probes(self.root), 2)
        self.assertEqual(clock.sleep.call_args_list, [call(.05), call(.05)])
        self.assertFalse(any(home.exists() for home in stale))
        self.assertTrue(notes.is_dir())
        self.assertTrue(named_file.is_file())

    def test_linked_uuid_entry_is_not_entered(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'keep.txt').write_bytes(b'keep')
        make_junction(self.base / str(uuid4()), outside)
        with self.clock(7200):
            self.assertEqual(login_probe.purge_stale_probes(self.root), 0)
        self.assertEqual((outside / 'keep.txt').read_bytes(), b'keep')

    def test_missing_probe_area_is_a_no_op(self):
        self.base.rmdir()
        self.assertEqual(login_probe.purge_stale_probes(self.root), 0)

    def test_child_purge_runs_the_module_without_manager_credentials(self):
        process = Mock()
        process.wait.return_value = 0
        with patch.dict(os.environ, {'CODEX_MANAGER_BROKER_TOKEN': 'secret', 'codex_manager_service_pipe': 'p'}), \
             patch.object(login_probe.subprocess, 'Popen', return_value=process) as popen:
            self.assertEqual(login_probe.purge_in_child(self.root), 0)
        argv, options = popen.call_args.args[0], popen.call_args.kwargs
        self.assertEqual(argv[1:], ['-X', 'utf8', '-m', 'manager_core.login_probe', '--purge', str(self.root.resolve())])
        self.assertEqual(options['cwd'], Path(login_probe.__file__).resolve().parents[1])
        self.assertFalse([key for key in options['env'] if key.upper().startswith('CODEX_MANAGER_')])
        self.assertEqual(options['stdin'], subprocess.PIPE)  # its EOF stops an orphaned child
        process.stdin.close.assert_called_once()

    def test_child_purge_process_starts_and_keeps_recent_homes(self):
        recent = self.base / str(uuid4())
        (recent / 'logs').mkdir(parents=True)
        self.assertEqual(login_probe.purge_in_child(self.root, timeout=60), 0)
        self.assertTrue(recent.is_dir())

    @unittest.skipUnless(os.name == 'nt', 'thread priority is read through kernel32')
    def test_purge_keeps_the_normal_thread_priority_while_it_holds_the_gil(self):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentThread.restype = wintypes.HANDLE
        kernel.GetThreadPriority.argtypes = [wintypes.HANDLE]
        kernel.GetThreadPriority.restype = ctypes.c_int
        priority = lambda: kernel.GetThreadPriority(kernel.GetCurrentThread())
        (self.base / str(uuid4())).mkdir()
        seen = []
        def run():
            before = priority()
            with self.clock(7200),                  patch.object(login_probe, '_discard', side_effect=lambda *_, **__: seen.append(priority()) or True):
                self.assertEqual(login_probe.purge_stale_probes(self.root), 1)
            seen.append(priority())
            seen.insert(0, before)
        worker = threading.Thread(target=run)
        worker.start()
        worker.join(5)
        self.assertEqual(len(seen), 3)
        self.assertEqual(len(set(seen)), 1, seen)  # no THREAD_MODE_BACKGROUND_BEGIN


if __name__ == '__main__':
    unittest.main()
