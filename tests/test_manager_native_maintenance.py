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


if __name__ == "__main__":
    unittest.main()
