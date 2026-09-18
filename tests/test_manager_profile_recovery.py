import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.instances import Instances, process_identity
from manager_core.profile_recovery import stop_profile, WindowsProcesses, select_descendants
from manager_core.store import Store


class FakeProcess:
    def __init__(self, pid, created, executable, arguments):
        self.pid, self.created, self.executable = pid, created, executable
        self.args = arguments
        self.running, self.closed, self.kills = True, False, 0
        self.on_kill = None

    def arguments(self): return self.args
    def alive(self): return self.running
    def close(self): self.closed = True
    def terminate(self):
        if self.running:
            self.kills += 1
            self.running = False
            if self.on_kill: self.on_kill()


class FakeProcesses:
    def __init__(self, home):
        self.items = {10: FakeProcess(10, 100, sys.executable, [sys.executable, '--user-data-dir=' + str(home)]),
                      11: FakeProcess(11, 101, sys.executable, [])}
        self.parents = {10: 1, 11: 10}

    def snapshot(self):
        return {pid: parent for pid, parent in self.parents.items() if self.items.get(pid) is None or self.items[pid].running}

    def open(self, pid): return self.items.get(pid)


class ProfileRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        self.profile = self.store.add_profile('test')
        self.generation = str(uuid4())
        self.store.mutate(lambda d: self.store.profile(self.profile['id'], d).update(
            generation=self.generation, process_id=10, process_created=100, executable_path=sys.executable))
        self.profile = self.store.profile(self.profile['id'])
        for field in ('home', 'ui_home'): Path(self.profile[field]).mkdir(parents=True)
        self.instances = Instances(Path(self.temp.name), self.store, None)
        self.processes = FakeProcesses(Path(self.profile['ui_home']))

    def stop(self, **args):
        return stop_profile(self.store, self.instances, self.profile['id'],
                            expected_generation=args.pop('expected_generation', self.generation),
                            interrupt_running_work=args.pop('interrupt_running_work', True),
                            processes=self.processes, **args)

    def assert_not_killed(self):
        self.assertTrue(all(p.kills == 0 for p in self.processes.items.values()))

    def test_one_profile_tree_stops_without_editing_auth_or_history(self):
        history = Path(self.profile['home']) / 'history.jsonl'
        history.write_bytes(b'preserved fixture')
        before = self.store.read()
        result = self.stop()
        self.assertEqual(result['state'], 'stopped')
        self.assertTrue(result['all_selected_processes_exited'])
        self.assertEqual({p['pid'] for p in result['processes']}, {10, 11})
        self.assertTrue(all(p.closed for p in self.processes.items.values()))
        self.assertEqual(history.read_bytes(), b'preserved fixture')
        self.assertEqual(self.store.read(), before)
        self.assertNotIn('--user-data-dir', json.dumps(result))

    def test_late_child_of_exited_parent_is_also_bound_and_stopped(self):
        def spawn():
            self.processes.items[12] = FakeProcess(12, 102, sys.executable, [])
            self.processes.parents[12] = 11
        self.processes.items[11].on_kill = spawn
        self.assertEqual(len(self.stop()['processes']), 3)
        self.assertFalse(self.processes.items[12].running)

    def test_selected_generation_must_still_match(self):
        with self.assertRaises(ValueError): self.stop(expected_generation=str(uuid4()))
        self.assert_not_killed()

    def test_shared_wsl_host_and_its_children_are_preserved(self):
        self.processes.items[11].executable = str(Path(self.temp.name) / 'wslhost.exe')
        self.processes.items[12] = FakeProcess(12, 102, sys.executable, [])
        self.processes.parents[12] = 11
        result = self.stop()
        self.assertEqual({p['pid'] for p in result['processes']}, {10})
        self.assertEqual({p['pid'] for p in result['preserved_infrastructure']}, {11, 12})
        self.assertTrue(self.processes.items[11].running)
        self.assertTrue(self.processes.items[12].running)

    def test_explicit_interrupt_is_required(self):
        with self.assertRaises(ValueError): self.stop(interrupt_running_work=False)
        self.assert_not_killed()

    def test_writer_drained_callback_allows_automatic_exit_and_runs_before_termination(self):
        checks = []
        def quiescent():
            self.assert_not_killed()
            checks.append(True)
            return True
        result = self.stop(interrupt_running_work=False, quiescence_check=quiescent)
        self.assertEqual(result['state'], 'stopped')
        self.assertEqual(len(checks), 2)

    def test_lost_quiescence_proof_preserves_process_tree(self):
        checks = iter((True, False))
        with self.assertRaises(RuntimeError):
            self.stop(interrupt_running_work=False, quiescence_check=lambda: next(checks))
        self.assert_not_killed()

    def test_initial_quiescence_failure_never_terminates(self):
        with self.assertRaises(RuntimeError):
            self.stop(interrupt_running_work=False, quiescence_check=lambda: False)
        self.assert_not_killed()

    def test_automatic_exit_still_requires_owned_user_data_directory(self):
        self.processes.items[10].args = []
        with self.assertRaises(RuntimeError):
            self.stop(interrupt_running_work=False, quiescence_check=lambda: True)
        self.assert_not_killed()

    def test_pid_reuse_is_rejected_before_any_termination(self):
        self.processes.items[10].created += 1
        with self.assertRaises(RuntimeError): self.stop()
        self.assert_not_killed()

    def test_executable_mismatch_is_rejected(self):
        self.processes.items[10].executable = str(Path(self.temp.name) / 'different.exe')
        with self.assertRaises(RuntimeError): self.stop()
        self.assert_not_killed()

    def test_original_app_or_partial_ui_directory_match_is_rejected(self):
        for args in ([], ['--user-data-dir=' + self.profile['ui_home'] + '-other']):
            self.processes.items[10].args = args
            with self.assertRaises(RuntimeError): self.stop()
            self.assert_not_killed()

    def test_parent_pid_reuse_does_not_capture_older_process(self):
        self.processes.items[11].created = 99
        with self.assertRaises(RuntimeError): self.stop()
        self.assert_not_killed()

    def test_calling_session_descendant_is_protected(self):
        self.processes.parents[os.getpid()] = 11
        with self.assertRaises(RuntimeError): self.stop()
        self.assert_not_killed()

    def test_other_registered_account_is_protected(self):
        other = self.store.add_profile('other')
        self.store.mutate(lambda d: self.store.profile(other['id'], d).update(process_id=11))
        with self.assertRaises(RuntimeError): self.stop()
        self.assert_not_killed()

    def test_unverifiable_child_prevents_partial_termination(self):
        original = self.processes.open
        def denied(pid):
            if pid == 11: raise PermissionError('fixture')
            return original(pid)
        self.processes.open = denied
        with self.assertRaises(PermissionError): self.stop()
        self.assert_not_killed()

    def test_gone_root_is_idempotent_without_touching_other_processes(self):
        self.processes.parents.pop(10)
        self.assertEqual(self.stop()['state'], 'already_stopped')
        self.assert_not_killed()

    def test_profile_home_outside_manager_is_rejected(self):
        self.store.mutate(lambda d: self.store.profile(self.profile['id'], d).update(home=self.temp.name))
        with self.assertRaises(ValueError): self.stop()
        self.assert_not_killed()

    @unittest.skipUnless(os.name == 'nt', 'Windows kernel integration fixture')
    def test_real_headless_tree_terminates_but_unrelated_process_survives(self):
        child_file = Path(self.temp.name) / 'child.pid'
        program = ('import subprocess,sys,time,pathlib; '
                   'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]); '
                   'pathlib.Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)')
        with subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                              creationflags=subprocess.CREATE_NO_WINDOW) as unrelated:
            main = subprocess.Popen([sys.executable, '-c', program, str(child_file),
                                     '--user-data-dir=' + self.profile['ui_home']],
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            child_handle = None
            try:
                deadline = time.monotonic() + 5
                while not child_file.exists() and time.monotonic() < deadline: time.sleep(0.02)
                child_pid = int(child_file.read_text())
                child_handle = WindowsProcesses().open(child_pid)
                identity = process_identity(main.pid)
                self.store.mutate(lambda d: self.store.profile(self.profile['id'], d).update(identity))
                expected = select_descendants(WindowsProcesses().snapshot(), main.pid)
                result = stop_profile(self.store, self.instances, self.profile['id'],
                                      expected_generation=self.generation, interrupt_running_work=True)
                self.assertEqual(result['state'], 'stopped')
                self.assertEqual({p['pid'] for p in result['processes']}, expected)
                self.assertIsNotNone(main.poll())
                self.assertIsNone(process_identity(main.pid))
                self.assertFalse(child_handle.alive())
                self.assertIsNone(unrelated.poll())
            finally:
                if main.poll() is None: main.kill()
                main.wait(timeout=5)
                if child_handle:
                    child_handle.terminate()
                    child_handle.close()
                unrelated.kill()
                unrelated.wait(timeout=5)


if __name__ == '__main__': unittest.main()
