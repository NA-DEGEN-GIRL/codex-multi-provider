import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

from manager_core.ssh_inventory import SshInventory
from manager_core.process_state import process_liveness
from manager_core.remote_maintenance import RemoteMaintenance
from manager_core.store import Store
from manager_core.update_hooks import UpdateHooks
from manager_core.updates import UpdateError


class InventoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root)
        self.profile = self.store.add_profile('selected')
        self.profile['generation'] = str(uuid4())
        self.inventory = SshInventory(self.root)
        self.inventory.prepare(self.profile['id'], self.profile['generation'])

    def execute(self, operation='native-version', alias='remote-dev'):
        return self.inventory.execution(self.profile['id'], self.profile['generation'],
                                        dict(operation=operation, alias=alias))

    def test_active_command_blocks_coverage_and_normal_completion_releases_it(self):
        self.assertTrue(self.inventory.coverage(self.profile)['complete'])
        with self.execute():
            self.assertFalse(self.inventory.coverage(self.profile)['complete'])
        self.assertTrue(self.inventory.coverage(self.profile)['complete'])

    def test_disconnected_proxy_and_new_local_generation_do_not_prove_remote_exit(self):
        with self.execute('native-proxy'):
            pass
        self.profile['generation'] = str(uuid4())
        self.inventory.prepare(self.profile['id'], self.profile['generation'])
        coverage = self.inventory.coverage(self.profile)
        self.assertEqual(coverage['hosts'], ['local', 'remote-dev'])
        hooks = UpdateHooks(self.root, self.store, None, host_inventory=self.inventory.coverage)
        self.assertFalse(hooks._host_coverage(self.profile))

    def test_stale_generation_and_unmanaged_login_window_cannot_certify_coverage(self):
        stale = dict(self.profile, generation=str(uuid4()))
        self.assertFalse(self.inventory.coverage(stale)['complete'])
        self.assertFalse(self.inventory.coverage(dict(self.profile, runtime_channel='packaged'))['complete'])
        with self.assertRaises(UpdateError):
            with self.inventory.execution(stale['id'], stale['generation'], {'operation': 'native-start'}):
                self.fail('stale generation must not execute')

    def test_maintenance_blocks_target_ssh_but_leaves_peer_available(self):
        peer = self.store.add_profile('peer')
        peer['generation'] = str(uuid4())
        self.inventory.prepare(peer['id'], peer['generation'])
        hooks = UpdateHooks(self.root, self.store, None)
        hooks._begin_global(str(uuid4()), [self.profile['id']])
        with self.assertRaises(UpdateError):
            with self.execute():
                self.fail('target must not execute')
        with self.inventory.execution(peer['id'], peer['generation'], {'operation': 'native-version'}):
            self.assertFalse(self.inventory.coverage(peer)['complete'])
        self.assertTrue(self.inventory.coverage(peer)['complete'])

    def test_ssh_enrollment_does_not_wait_for_an_unrelated_desktop_launch(self):
        self.inventory.identity = lambda _: {}
        peer = self.store.add_profile('launching desktop')
        hooks = UpdateHooks(self.root, self.store, None)
        finished = threading.Event()
        errors, admitted = [], []

        def connect():
            try:
                for operation in ('native-version', 'native-start', 'native-proxy'):
                    with self.execute(operation):
                        admitted.append(operation)
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()

        worker = threading.Thread(target=connect)
        try:
            # This real file lock remains owned throughout the assertion. No
            # app or SSH process is started by the fixture enrollment contexts.
            with hooks.launch_admission(peer['id']):
                worker.start()
                self.assertTrue(finished.wait(2), 'SSH enrollment waited for desktop launch admission')
        finally:
            if worker.ident is not None:
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(admitted, ['native-version', 'native-start', 'native-proxy'])
        self.assertEqual(self.inventory.coverage(self.profile)['hosts'], ['local', 'remote-dev'])

    def test_enrollment_rechecks_gates_and_generation_after_waiting_for_state(self):
        self.inventory.identity = lambda _: {}
        for transition in ('global', 'profile', 'ssh', 'generation'):
            with self.subTest(transition=transition):
                def reset(data):
                    for key in ('update_maintenance', 'profile_maintenance', 'ssh_maintenance'):
                        data.pop(key, None)
                self.store.mutate(reset)
                self.inventory.prepare(self.profile['id'], self.profile['generation'])
                attempted = threading.Event()
                errors, admitted = [], []
                mutate = self.inventory.store.mutate

                def observe_attempt(operation):
                    attempted.set()
                    return mutate(operation)

                def connect():
                    try:
                        with self.execute('native-start'):
                            admitted.append(True)
                    except Exception as error:
                        errors.append(error)

                def change(data):
                    gate = dict(state='held', transaction_id=str(uuid4()))
                    if transition == 'global':
                        data['update_maintenance'] = gate
                    elif transition == 'profile':
                        data['profile_maintenance'] = {self.profile['id']: gate}
                    elif transition == 'ssh':
                        data['ssh_maintenance'] = {self.profile['id']: gate}
                    else:
                        data['ssh_inventory'][self.profile['id']]['generation'] = str(uuid4())

                worker = threading.Thread(target=connect)
                try:
                    with patch.object(self.inventory.store, 'mutate', side_effect=observe_attempt):
                        # Separate Store objects contend on the actual state
                        # file lock, so the child cannot enroll from stale data.
                        with self.store.locked():
                            worker.start()
                            self.assertTrue(attempted.wait(2))
                            self.store.mutate(change)
                        worker.join(3)
                finally:
                    if worker.ident is not None:
                        worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(admitted, [])
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], UpdateError)
                self.assertEqual(errors[0].code, {
                    'global': 'profile_restarting', 'profile': 'profile_restarting',
                    'ssh': 'ssh_settings_pending', 'generation': 'ssh_generation_changed',
                }[transition])
                inventory = self.store.read()['ssh_inventory'][self.profile['id']]
                self.assertEqual(inventory['hosts'], [])
                self.assertEqual(inventory['operations'], {})

    def test_ssh_only_gate_blocks_a_late_native_start_without_blocking_local_launch(self):
        hooks=UpdateHooks(self.root,self.store,None)
        for state in ('held','attention'):
            with self.subTest(state=state):
                self.store.mutate(lambda data:data.setdefault('ssh_maintenance',{}).update({
                    self.profile['id']:{'state':state,'generation':self.profile['generation']}}))
                hooks.guard_launch(self.profile['id'])
                with self.assertRaises(UpdateError):
                    with self.execute('native-start'):
                        self.fail('A connection checked before the SSH gate must still be fenced at enrollment')
        self.store.mutate(lambda data:data['ssh_maintenance'][self.profile['id']].update(state='released'))
        with self.execute('native-start'):
            pass

    def test_unclassified_commands_never_disappear_from_coverage_on_local_exit(self):
        with self.execute('passthrough'):
            pass
        self.assertFalse(self.inventory.coverage(self.profile)['complete'])

    def record_operation(self, **changes):
        operation_id = str(uuid4())
        operation = dict(operation='native-proxy', alias='remote-dev', revision='a' * 64,
                         pid=23456, process_created=123456, generation=self.profile['generation'])
        operation.update(changes)
        def write(data):
            record = data['ssh_inventory'][self.profile['id']]
            record['hosts'] = ['remote-dev']
            record['operations'][operation_id] = operation
        self.store.mutate(write)
        return operation_id, operation

    def test_only_proven_exit_or_pid_reuse_retires_a_connection(self):
        for state in ('alive', 'unknown', 'exited', 'reused'):
            with self.subTest(state=state):
                operation_id, _ = self.record_operation()
                self.inventory.liveness = lambda _: state
                coverage = self.inventory.coverage(self.profile)
                stored = self.store.read()['ssh_inventory'][self.profile['id']]
                self.assertEqual(operation_id not in stored['operations'], state in ('exited', 'reused'))
                self.assertEqual(coverage['hosts'], ['local', 'remote-dev'])

    def test_query_failure_keeps_the_operation_and_remote_host(self):
        _, recorded = self.record_operation()
        with patch.object(self.inventory, 'liveness', side_effect=OSError('fixture access failure')):
            coverage = self.inventory.coverage(self.profile)
        self.assertEqual(coverage['operations'], [recorded])
        self.assertFalse(coverage['complete'])
        self.assertEqual(coverage['hosts'], ['local', 'remote-dev'])

    def test_concurrent_record_change_is_not_removed_using_old_evidence(self):
        operation_id, operation = self.record_operation()
        updated = dict(operation, process_created=999999)
        def observe(_):
            self.store.mutate(lambda data: data['ssh_inventory'][self.profile['id']]['operations'].update(
                {operation_id: updated}))
            return 'exited'
        self.inventory.liveness = observe
        self.assertEqual(self.inventory.coverage(self.profile)['operations'], [updated])

    def test_old_generation_is_retired_but_remote_inspection_stays_required(self):
        self.record_operation(operation='native-start')
        self.profile['generation'] = str(uuid4())
        self.inventory.prepare(self.profile['id'], self.profile['generation'])
        self.inventory.liveness = lambda _: 'exited'
        coverage = self.inventory.coverage(self.profile)
        self.assertTrue(coverage['maintenance_complete'])
        self.assertEqual(coverage['operations'], [])
        self.assertEqual(coverage['hosts'], ['local', 'remote-dev'])
        # The actual controller must still require the remote binding/proof.
        with self.assertRaises((OSError, UpdateError)):
            RemoteMaintenance(self.root, self.store, None).bindings(self.profile, coverage)

    def test_crashed_unclassified_command_cannot_certify_remote_coverage(self):
        self.record_operation(operation='passthrough')
        self.store.mutate(lambda data: data['ssh_inventory'][self.profile['id']].update(unclassified=True))
        self.inventory.liveness = lambda _: 'exited'
        coverage = self.inventory.coverage(self.profile)
        self.assertEqual(coverage['operations'], [])
        self.assertFalse(coverage['complete'] or coverage['maintenance_complete'])

    @unittest.skipUnless(os.name == 'nt', 'Windows process identity')
    def test_real_python_crash_skips_finally_and_cleanup_preserves_active_peer(self):
        code = '''import os,sys
from manager_core.ssh_inventory import SshInventory
with SshInventory(sys.argv[1]).execution(sys.argv[2],sys.argv[3],
        {'operation':'native-start','alias':'remote-dev','revision':'a'*64}):
    os._exit(23)
'''
        peer = self.store.add_profile('peer')
        peer['generation'] = str(uuid4())
        self.inventory.prepare(peer['id'], peer['generation'])
        with self.inventory.execution(peer['id'], peer['generation'], {'operation': 'native-version'}):
            result = subprocess.run([sys.executable, '-c', code, str(self.root),
                                     self.profile['id'], self.profile['generation']],
                                    env=self.child_environment(), capture_output=True, timeout=15,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 23, result.stderr)
            recorded = self.store.read()['ssh_inventory'][self.profile['id']]['operations']
            self.assertEqual(len(recorded), 1, 'os._exit must bypass the context manager finally')
            self.assertGreater(next(iter(recorded.values()))['process_created'], 0)
            coverage = self.inventory.coverage(self.profile)
            self.assertEqual(coverage['operations'], [])
            self.assertEqual(coverage['hosts'], ['local', 'remote-dev'])
            self.assertFalse(self.inventory.coverage(peer)['complete'])

    def child_environment(self):
        return dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'scripts'), PYTHONUTF8='1')

    @unittest.skipUnless(os.name == 'nt', 'Windows native transport job')
    def test_native_bootstrap_job_exit_does_not_leave_an_old_generation_blocker(self):
        exe = Path(__file__).resolve().parents[1] / 'manager/SshProxy/bin/Release/net10.0-windows/ssh.exe'
        if not exe.is_file():
            self.skipTest('Build the native SSH bootstrap to exercise its job teardown')
        script = self.root / 'ssh-inventory-child.py'
        script.write_text('''import json,sys,threading
from pathlib import Path
from manager_core.ssh_inventory import SshInventory
root,profile,generation=sys.argv[-3:]
inventory=SshInventory(root)
with inventory.execution(profile,generation,{'operation':'native-proxy','alias':'remote-dev','revision':'a'*64}):
    Path(root,'ready.json').write_text(json.dumps(inventory.coverage({'id':profile,'generation':generation})))
    # An EOF can otherwise exit cleanly before the Windows job kills us,
    # making the stale-operation assertion depend on teardown timing.
    threading.Event().wait()
''', encoding='utf-8')
        environment = dict(self.child_environment(), CODEX_MANAGER_SSH_PYTHON=sys.executable,
                           CODEX_MANAGER_SSH_SCRIPT=str(script))
        child = subprocess.Popen([str(exe), str(self.root), self.profile['id'], self.profile['generation']],
                                 env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            ready = self.root / 'ready.json'
            deadline = time.monotonic() + 10
            while not ready.exists():
                self.assertIsNone(child.poll(), 'fixture bootstrap exited before enrollment')
                self.assertLess(time.monotonic(), deadline, 'fixture enrollment timed out')
                time.sleep(.02)
            recorded = self.inventory.coverage(self.profile)['operations']
            self.assertEqual(len(recorded), 1)
            identity = {'pid': recorded[0]['pid'], 'created': recorded[0]['process_created']}
            self.assertEqual(process_liveness(identity), 'alive')
            # Only this fixture-owned shim is killed, matching Electron teardown.
            child.kill()
            child.communicate(timeout=10)
            while process_liveness(identity) == 'alive':
                self.assertLess(time.monotonic(), deadline, 'job-owned fixture Python is still alive')
                time.sleep(.02)
            self.assertIn(process_liveness(identity), ('exited', 'reused'))
            self.assertEqual(len(self.store.read()['ssh_inventory'][self.profile['id']]['operations']), 1)
            self.profile['generation'] = str(uuid4())
            self.inventory.prepare(self.profile['id'], self.profile['generation'])
            coverage = self.inventory.coverage(self.profile)
            self.assertTrue(coverage['maintenance_complete'])
            self.assertEqual(coverage['operations'], [])
            self.assertEqual(coverage['hosts'], ['local', 'remote-dev'])
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)
