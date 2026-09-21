"""Maintenance RPC framing and exact-instance proof; no live app or SSH."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/remote_helpers"))
SPEC = importlib.util.spec_from_file_location("native_maintenance_test", ROOT / "scripts/remote_helpers/native_controller.py")
NATIVE = importlib.util.module_from_spec(SPEC)
if sys.platform == "win32":
    with patch.dict(sys.modules, {"fcntl": types.SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=MagicMock())}):
        SPEC.loader.exec_module(NATIVE)
else:
    SPEC.loader.exec_module(NATIVE)


class NativeMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.profile = Path(self.temporary.name)
        self.revision = "a" * 64
        self.record = dict(pid=13579, process_start="1234", boot_id="fixture-boot",
                           revision=self.revision, socket=str(self.profile / "control.sock"))
        self.connection = MagicMock()
        self.connection.__enter__.return_value = self.connection
        self.pipe = MagicMock()
        if not hasattr(NATIVE.socket, "AF_UNIX"):
            self.enterContext(patch.object(NATIVE.socket, "AF_UNIX", 1, create=True))
        self.enterContext(patch.object(NATIVE, "WebSocketPipe", return_value=self.pipe))

    def stop_patches(self, responses=None):
        patches = [
            patch.object(NATIVE, "_descriptor", return_value={"runtime":str(self.profile / "runtime")}),
            patch.object(NATIVE, "_running", return_value=self.record),
            patch.object(NATIVE, "socket_path", return_value=self.profile / "control.sock"),
            patch.object(NATIVE.socket, "socket", return_value=self.connection),
            patch.object(NATIVE, "_control_request", side_effect=responses or [
                {}, {"processId":13579,"shutdownRequested":True,"writerReleaseVerified":True}]),
            patch.object(NATIVE, "_process_start", return_value=None),
            patch.object(NATIVE, "_instance_lock_released", return_value=True),
        ]
        values = [item.start() for item in patches]
        for item in patches:
            self.addCleanup(item.stop)
        return values

    def test_reply_ignores_notifications_and_correlates_the_request(self):
        pipe = MagicMock()
        pipe.receive_json.side_effect = [
            {"method":"thread/closed","params":{"threadId":"fixture"}},
            {"id":99,"result":{}},
            {"id":2,"result":{"processId":13579}},
        ]
        reply = NATIVE._control_request(pipe, 2, "server/managedShutdown", {"processId":13579}, time.monotonic()+5)
        self.assertEqual(reply, {"processId":13579})
        self.assertEqual(pipe.send_json.call_args.args[0], {
            "id":2,"method":"server/managedShutdown","params":{"processId":13579}})

    def start_patches(self, existing):
        patches = [
            patch.object(NATIVE, '_descriptor', return_value={}),
            patch.object(NATIVE, '_running', return_value=existing),
            patch.object(NATIVE, 'socket_path', return_value=self.profile / 'control.sock'),
            patch.object(NATIVE, '_stop_locked'),
            patch.object(NATIVE, '_forward_agent', return_value='fixture-agent'),
            patch.object(NATIVE.subprocess, 'Popen'),
            patch.object(NATIVE, '_ready', return_value=True),
        ]
        values = [item.start() for item in patches]
        for item in patches:
            self.addCleanup(item.stop)
        return values

    def test_changed_revision_requires_journaled_maintenance_before_reconnect(self):
        previous = dict(self.record, revision='b' * 64)
        mocks = self.start_patches(previous)
        mocks[1].side_effect = RuntimeError('A different revision is still running')
        with self.assertRaisesRegex(RuntimeError, 'different revision'):
            NATIVE.start(self.profile, self.revision)
        mocks[1].assert_called_once_with(self.profile, self.revision)
        mocks[3].assert_not_called()
        mocks[5].assert_not_called()

    def test_same_revision_reconnect_reuses_listener_without_shutdown(self):
        mocks = self.start_patches(self.record)
        self.assertEqual(NATIVE.start(self.profile, self.revision), 0)
        mocks[3].assert_not_called()
        mocks[5].assert_not_called()

    def test_failed_child_reports_safe_config_conflict_without_waiting_twenty_seconds(self):
        mocks = self.start_patches(None)
        mocks[6].return_value = False
        mocks[5].return_value.poll.return_value = 1
        def spawn(*args, **kwargs):
            kwargs['stdout'].write(b'Codex manager remote launcher error: remote_configuration_changed\n')
            return mocks[5].return_value
        mocks[5].side_effect = spawn
        with patch.object(NATIVE.time, 'sleep') as sleep:
            with self.assertRaises(NATIVE.RemoteStartError) as raised:
                NATIVE.start(self.profile, self.revision)
        self.assertEqual(raised.exception.code, 'remote_configuration_changed')
        sleep.assert_not_called()

    def test_child_diagnostics_ignore_old_log_errors_and_arbitrary_private_output(self):
        logfile = self.profile / 'native-runtime.log'
        old = b'Codex manager remote launcher error: remote_configuration_changed\n'
        logfile.write_bytes(old + b'private arbitrary output must not escape\n')
        self.assertEqual(NATIVE._start_failure(logfile, len(old)), 'remote_runtime_exited')

    def test_expected_process_is_checked_inside_the_start_lock_before_any_rpc(self):
        mocks = self.stop_patches()
        stale = dict(self.record, process_start='previous-process')
        with self.assertRaisesRegex(RuntimeError, 'identity changed'):
            NATIVE.stop(self.profile, self.revision, expected_process=stale)
        mocks[4].assert_not_called()

    def test_old_runtime_error_and_client_input_defer_without_answering(self):
        for message in ({"id":2,"error":{"code":-32601}},
                        {"id":9,"method":"item/commandExecution/requestApproval","params":{}}):
            with self.subTest(message=message):
                pipe = MagicMock()
                pipe.receive_json.return_value = message
                with self.assertRaises(RuntimeError):
                    NATIVE._control_request(pipe, 2, "server/managedShutdown", {}, time.monotonic()+5)
                self.assertEqual(pipe.send_json.call_count, 1)

    def test_missing_immutable_root_binding_is_typed_without_leaking_rpc_text(self):
        pipe = MagicMock()
        pipe.receive_json.return_value = {'id': 2, 'error': {
            'code': -32600, 'message': 'root actor has no immutable managed source binding'}}
        with self.assertRaises(NATIVE.RemoteMaintenanceError) as raised:
            NATIVE._control_request(pipe, 2, 'thread/managedIdleStatus', {}, time.monotonic() + 5)
        self.assertEqual(raised.exception.code, 'remote_idle_binding_missing')
        pipe.receive_json.return_value['error']['message'] = 'untrusted private response'
        with self.assertRaises(RuntimeError) as raised:
            NATIVE._control_request(pipe, 2, 'thread/managedIdleStatus', {}, time.monotonic() + 5)
        self.assertNotIn('private response', str(raised.exception))

    def test_shared_catalog_listener_reports_typed_idle_unavailability(self):
        pipe = MagicMock()
        pipe.receive_json.return_value = {'id': 2, 'error': {
            'code': -32600, 'message': 'managed idle status is not enabled for this instance'}}
        with self.assertRaises(NATIVE.RemoteMaintenanceError) as raised:
            NATIVE._control_request(pipe, 2, 'thread/managedIdleStatus', {}, time.monotonic() + 5)
        self.assertEqual(raised.exception.code, 'remote_idle_status_unavailable')
        # The same text on another method is not an idle statement and must stay
        # unclassified instead of becoming a proof of anything.
        with self.assertRaises(RuntimeError) as other:
            NATIVE._control_request(pipe, 2, 'server/managedShutdown', {}, time.monotonic() + 5)
        self.assertNotIsInstance(other.exception, NATIVE.RemoteMaintenanceError)

    def test_managed_shutdown_without_managed_sources_is_typed_unavailable(self):
        pipe = MagicMock()
        pipe.receive_json.return_value = {'id': 2, 'error': {
            'code': -32600, 'message': 'managed shutdown requires the exact managed process'}}
        with self.assertRaises(NATIVE.RemoteMaintenanceError) as raised:
            NATIVE._control_request(pipe, 2, 'server/managedShutdown', {'processId': 1},
                                    time.monotonic() + 5)
        self.assertEqual(raised.exception.code, 'remote_shutdown_unavailable')

    def test_live_other_revision_is_typed_instead_of_a_generic_failure(self):
        previous = dict(self.record, revision='b' * 64)
        (self.profile / 'native-instance.json').write_text(json.dumps(previous), encoding='utf-8')
        original = Path.read_text
        def read_text(path, *args, **kwargs):
            if str(path).replace('\\', '/').endswith('/proc/sys/kernel/random/boot_id'):
                return 'fixture-boot'
            return original(path, *args, **kwargs)
        with patch.object(NATIVE, '_process_start', return_value='1234'), \
                patch.object(Path, 'read_text', read_text):
            with self.assertRaises(NATIVE.RemoteRevisionConflict) as raised:
                NATIVE._running(self.profile, self.revision)
            self.assertEqual(raised.exception.code, 'remote_revision_conflict')
            self.assertEqual(NATIVE._running(self.profile, self.revision, allow_other_revision=True), previous)

    def test_reused_listener_without_a_socket_is_not_reported_as_a_start_timeout(self):
        mocks = self.start_patches(self.record)
        mocks[6].return_value = False
        with patch.object(NATIVE.time, 'monotonic', side_effect=[0, 21]), \
                patch.object(NATIVE.time, 'sleep'):
            with self.assertRaises(NATIVE.RemoteStartError) as raised:
                NATIVE.start(self.profile, self.revision)
        self.assertEqual(raised.exception.code, 'remote_listener_unavailable')
        mocks[5].assert_not_called()

    def test_proxy_without_a_ready_listener_reports_listener_unavailable(self):
        with patch.object(NATIVE, '_descriptor', return_value={'runtime': str(self.profile / 'runtime')}), \
                patch.object(NATIVE, 'socket_path', return_value=self.profile / 'control.sock'), \
                patch.object(NATIVE, '_ready', return_value=False):
            with self.assertRaises(NATIVE.RemoteStartError) as raised:
                NATIVE.main(self.profile, self.revision, 'native-proxy')
        self.assertEqual(raised.exception.code, 'remote_listener_unavailable')

    def test_unverified_transport_reply_preserves_the_process(self):
        for error in (EOFError("truncated"), ValueError("oversized"), TimeoutError("late")):
            with self.subTest(error=error):
                mocks = self.stop_patches([{}, error])
                with self.assertRaises(type(error)):
                    NATIVE.stop(self.profile, self.revision)
                mocks[5].assert_not_called()
        self.connection.shutdown.assert_called_with(NATIVE.socket.SHUT_RDWR)

    def test_stop_requires_acknowledged_shutdown_exit_and_released_lock(self):
        mocks = self.stop_patches()
        self.assertEqual(NATIVE.stop(self.profile, self.revision), 0)
        self.assertEqual(mocks[4].call_args.args[1:4], (2, "server/managedShutdown", {"processId":13579}))
        mocks[5].assert_called_once_with(13579)
        mocks[6].assert_called_once_with(self.profile)
        receipt = json.loads((self.profile / 'native-shutdown.json').read_text())
        self.assertEqual(receipt['process'], self.record)
        self.assertTrue(receipt['shutdown_requested'])
        self.assertTrue(receipt['writer_release_verified'])

    def test_changed_identity_prevents_the_shutdown_request(self):
        mocks = self.stop_patches()
        mocks[1].side_effect = [self.record, {**self.record,"process_start":"5678"}]
        with self.assertRaisesRegex(RuntimeError, "identity changed"):
            NATIVE.stop(self.profile, self.revision)
        self.assertEqual(mocks[4].call_count, 1)
        mocks[5].assert_not_called()

    def test_missing_writer_proof_or_wrong_process_never_confirms_stop(self):
        for response in ({"processId":13579,"shutdownRequested":True},
                         {"processId":24680,"shutdownRequested":True,"writerReleaseVerified":True}):
            with self.subTest(response=response):
                mocks = self.stop_patches([{}, response])
                with self.assertRaisesRegex(RuntimeError, "shutdown proof"):
                    NATIVE.stop(self.profile, self.revision)
                mocks[5].assert_not_called()
                self.assertFalse((self.profile / 'native-shutdown.json').exists())

    def test_exited_process_with_owned_profile_is_not_ready_for_update(self):
        mocks = self.stop_patches()
        mocks[6].return_value = False
        with patch.object(NATIVE.time, "monotonic", side_effect=[0, 0, 91]), patch.object(NATIVE.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "remain pending"):
                NATIVE.stop(self.profile, self.revision)

    def test_exited_process_waits_for_delayed_lock_release(self):
        mocks = self.stop_patches()
        mocks[6].side_effect = [False, True]
        with patch.object(NATIVE.time, "sleep") as pause:
            self.assertEqual(NATIVE.stop(self.profile, self.revision), 0)
        self.assertEqual(mocks[6].call_count, 2)
        pause.assert_called_once_with(0.1)

    def test_timeout_leaves_settings_pending(self):
        self.stop_patches()
        with patch.object(NATIVE.time, "monotonic", side_effect=[0, 91]):
            with self.assertRaisesRegex(RuntimeError, "remain pending"):
                NATIVE.stop(self.profile, self.revision)
        self.assertTrue((self.profile / 'native-shutdown.json').is_file())

    def test_missing_instance_lock_is_not_fabricated_as_release_evidence(self):
        with self.assertRaisesRegex(RuntimeError, "lock is unavailable"):
            NATIVE._instance_lock_released(self.profile)
        self.assertFalse((self.profile / "instance.lock").exists())


class RemoteHelperMaintenanceTests(unittest.TestCase):
    """Read-only reuse of a listener that can answer neither idle nor shutdown."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.profile_id = '3f677cd8-12ce-4721-b090-66d1a5111598'
        self.revision = 'a' * 64
        self.process = dict(pid=4242, process_start='77', boot_id='fixture',
                            revision=self.revision, socket=str(self.root / 'control.sock'))

    @staticmethod
    def maintenance_error(code):
        class MaintenanceError(RuntimeError):
            pass
        MaintenanceError.code = code
        return MaintenanceError

    def fixture(self, native):
        """Load the remote helper against a fixture native controller."""
        spec = importlib.util.spec_from_file_location(
            'remote_maintenance_helper_test', ROOT / 'scripts/remote_helpers/maintenance.py')
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'native_controller': native,
                'ws_client': types.SimpleNamespace(WebSocketPipe=MagicMock()),
                'fcntl': types.SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=MagicMock())}):
            spec.loader.exec_module(module)
        return module

    def runtime_identity(self, module, runtime='runtime'):
        """Resolve /proc/<pid>/exe to a fixture bundle without a Linux host."""
        binary = self.root / runtime / 'codex'
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b'fixture')
        original = Path.resolve
        def resolved(path, *args, **kwargs):
            if str(path).replace('\\', '/') == '/proc/4242/exe':
                return binary
            return original(path, *args, **kwargs)
        self.enterContext(patch.object(module.Path, 'resolve', resolved))

    def helper(self, code, *, process='default', runtime='runtime'):
        error = self.maintenance_error(code)
        native = types.SimpleNamespace(RemoteMaintenanceError=error, start=MagicMock(),
            _running=MagicMock(return_value=self.process if process == 'default' else process),
            _descriptor=MagicMock(return_value={'runtime': str(self.root / runtime)}))
        module = self.fixture(native)
        self.runtime_identity(module, runtime)
        return module, native, error

    def test_idle_status_unavailable_observes_the_process_without_idle(self):
        module, _, error = self.helper('remote_idle_status_unavailable')
        with patch.object(module, 'inspect', side_effect=error()):
            result = module.observe(self.root, self.revision)
        self.assertEqual(result, dict(process=self.process, idle=False, exited=False,
            revision=self.revision, requested_revision=self.revision,
            observation_code='remote_idle_status_unavailable'))

    def test_unknown_failures_are_never_downgraded_to_an_observation(self):
        for code in ('remote_maintenance_unverified', 'remote_configuration_changed',
                     'remote_shutdown_unavailable'):
            with self.subTest(code=code):
                module, native, error = self.helper(code)
                with patch.object(module, 'inspect', side_effect=error()):
                    with self.assertRaises(error):
                        module.observe(self.root, self.revision)
                native._running.assert_not_called()

    def test_start_reuses_the_exact_listener_without_claiming_idle(self):
        module, native, error = self.helper('remote_idle_status_unavailable')
        directory = self.root / '.local/share/codex-control-center/profiles' / self.profile_id
        directory.mkdir(parents=True, exist_ok=True)
        binding = dict(alias='fixture-host', profile_id=self.profile_id, revision=self.revision,
                       remote_python='/usr/bin/python3', remote_launcher=str(directory / 'launch.py'))
        with patch.object(module.Path, 'home', return_value=self.root), \
                patch.object(module, 'inspect', side_effect=error()):
            result = module.dispatch(dict(binding=binding, operation='start'))
        native.start.assert_called_once_with(directory, self.revision)
        self.assertEqual(result, dict(process=self.process, idle=False, exited=False, revision=self.revision))
        self.assertNotIn('observation_code', result)

    def test_start_remains_unverified_when_the_exact_process_is_gone(self):
        module, _, error = self.helper('remote_idle_status_unavailable', process=None)
        directory = self.root / '.local/share/codex-control-center/profiles' / self.profile_id
        directory.mkdir(parents=True, exist_ok=True)
        binding = dict(alias='fixture-host', profile_id=self.profile_id, revision=self.revision,
                       remote_python='/usr/bin/python3', remote_launcher=str(directory / 'launch.py'))
        with patch.object(module.Path, 'home', return_value=self.root), \
                patch.object(module, 'inspect', side_effect=error()):
            with self.assertRaisesRegex(RuntimeError, 'unverified'):
                module.dispatch(dict(binding=binding, operation='start'))


if __name__ == "__main__":
    unittest.main()
