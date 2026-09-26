"""Focused checks for the explicit shared-mode graceful drain helper.

Fixtures only: no Codex runtime, no SSH host, no real profile and no signal to
any process outside this test. The helper modules are Linux deployment
artifacts, so they are loaded by path with a minimal fcntl stand-in when the
suite runs on Windows.
"""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / "scripts" / "remote_helpers"
REVISION = "0" * 64
THREAD_ID = "1b4e28ba-2fa1-11d2-883f-0016d3cca427"


def _load(name):
    if str(HELPERS) not in sys.path:
        sys.path.insert(0, str(HELPERS))
    try:
        import fcntl  # noqa: F401
    except ImportError:
        stub = types.ModuleType("fcntl")
        stub.LOCK_EX, stub.LOCK_NB = 2, 4
        stub.flock = lambda *args, **kwargs: None
        sys.modules["fcntl"] = stub
    return importlib.import_module(name)


NATIVE = _load("native_controller")
MAINTENANCE = _load("maintenance")


def _write(path, data):
    """Create/truncate a fixture file without Python's buffered text writes."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    handle = os.open(str(path), os.O_CREAT | os.O_RDWR | os.O_TRUNC, 0o600)
    try:
        os.write(handle, data)
    finally:
        os.close(handle)


def _process(pid=4242, start="12345"):
    return {"pid": pid, "process_start": start, "boot_id": "boot", "revision": REVISION,
            "socket": "/tmp/codex-control-1000/deadbeef.sock"}


class DrainMarkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "codex"

    def tearDown(self):
        self.temp.cleanup()

    def test_all_markers_found_across_chunk_boundary(self):
        body = b"\x00" * (1024 * 1024) + b"".join(NATIVE.DRAIN_MARKERS)
        _write(self.path, body)
        self.assertEqual(NATIVE.drain_markers(self.path), set(NATIVE.DRAIN_MARKERS))

    def test_unrelated_binary_reports_no_markers(self):
        _write(self.path, b"\x7fELF" + b"stock build without managed drain" * 64)
        self.assertEqual(NATIVE.drain_markers(self.path), set())

    def test_scan_bound_is_respected(self):
        _write(self.path, b"\x00" * 8192 + NATIVE.DRAIN_MARKERS[0])
        with mock.patch.object(NATIVE, "DRAIN_SCAN_BYTES", 4096):
            self.assertEqual(NATIVE.drain_markers(self.path), set())

    def test_signal_mask_parsing(self):
        text = "Name:\tcodex\nSigCgt:\t0000000000000001\nSigBlk:\t0000000000000000\n"
        self.assertEqual(NATIVE.signal_mask_from_status(text), 1)
        self.assertEqual(NATIVE.signal_mask_from_status("Name:\tcodex\n"), None)
        self.assertEqual(NATIVE.signal_mask_from_status("SigCgt:\tnot-hex\n"), None)


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux /proc signal state")
class LiveSignalStateTests(unittest.TestCase):
    def test_own_process_is_inspectable(self):
        # Python does not install a SIGHUP handler by default, so this asserts
        # the reader, not a particular mask.
        self.assertIn(NATIVE.catches_hangup(os.getpid()), (True, False))


@unittest.skipUnless(sys.platform.startswith("linux") and hasattr(os, "pidfd_open"),
                     "Linux pidfd")
class PidfdTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.marker = self.dir / "hup.txt"
        self.ready = self.dir / "ready.txt"
        self.script = self.dir / "child.py"
        _write(self.script,
            "import pathlib, signal, sys, time\n"
            "marker, ready = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])\n"
            "signal.signal(signal.SIGHUP, lambda *a: (marker.write_text('hup'), sys.exit(0)))\n"
            "ready.write_text(str(pathlib.Path('/proc', str(__import__('os').getpid()), 'stat').read_text()))\n"
            "while True: time.sleep(0.05)\n")

    def tearDown(self):
        self.temp.cleanup()

    def _start(self):
        import subprocess
        child = subprocess.Popen([sys.executable, str(self.script), str(self.marker), str(self.ready)])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        deadline = __import__("time").monotonic() + 10
        while not self.ready.exists() and __import__("time").monotonic() < deadline:
            __import__("time").sleep(0.02)
        self.assertTrue(self.ready.exists(), "fixture child did not start")
        return child

    def test_pidfd_delivers_graceful_hangup_and_child_exits(self):
        child = self._start()
        self.assertIs(NATIVE.catches_hangup(child.pid), True)
        handle = NATIVE._drain_handle(child.pid)
        try:
            NATIVE._drain_send(handle)
        finally:
            os.close(handle)
        self.assertEqual(child.wait(timeout=10), 0)
        self.assertEqual(self.marker.read_text(encoding="utf-8"), "hup")

    def test_pidfd_refuses_a_process_that_is_gone(self):
        child = self._start()
        child.kill()
        child.wait(timeout=10)
        with self.assertRaises(NATIVE.RemoteDrainError) as raised:
            NATIVE._drain_handle(child.pid)
        self.assertEqual(raised.exception.code, "remote_drain_identity_changed")


class DrainFlowTests(unittest.TestCase):
    """Whole-flow checks with patched process evidence; no signals are sent."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.profile = Path(self.temp.name).resolve()
        _write(self.profile / "native-start.lock", b"")
        _write(self.profile / "instance.lock", b"")
        (self.profile / "runtime").mkdir()
        binary = self.profile / "runtime" / "codex"
        _write(binary, b"".join(NATIVE.DRAIN_MARKERS))
        self.executable = binary
        self.digest = NATIVE._drain_digest(binary)
        self.observed = _process()
        self.signals = []
        self.state = dict(gone=False, lock_released=False, alive=True, old_gone=True, audited=True)

        def process_gone(process):
            if process == self.observed:
                return self.state["gone"]
            return self.state["old_gone"]

        audited = mock.patch.object(NATIVE, "DRAIN_AUDITED_SHA256",
                                    frozenset({self.digest, "0" * 64}))
        audited.start()
        self.addCleanup(audited.stop)
        patches = [
            mock.patch.object(NATIVE, "_descriptor",
                              lambda profile, revision: {"runtime": str(self.profile / "runtime"),
                                                         "revision": revision,
                                                         "profile_id": profile.name}),
            mock.patch.object(NATIVE, "socket_path", lambda profile: Path(self.observed["socket"])),
            mock.patch.object(NATIVE, "_drain_identity",
                              lambda profile, revision: None if self.state["gone"] else self.observed),
            mock.patch.object(NATIVE, "_running",
                              lambda profile, revision: None if self.state["gone"] else self.observed),
            mock.patch.object(NATIVE, "catches_hangup", lambda pid: self.state["alive"]),
            mock.patch.object(NATIVE, "_process_start",
                              lambda pid: None if self.state["gone"] else self.observed["process_start"]),
            mock.patch.object(NATIVE, "_instance_lock_released", lambda profile: self.state["lock_released"]),
            mock.patch.object(NATIVE, "_drain_handle", lambda pid: "handle:" + str(pid)),
            mock.patch.object(NATIVE, "_drain_close", lambda handle: None),
            mock.patch.object(NATIVE, "_process_gone", process_gone),
            # The live link is the fixture binary here; the real /proc read and
            # dev/inode comparison are covered by ExecutableIdentityTests.
            mock.patch.object(NATIVE, "_drain_executable",
                              lambda descriptor, pid: self.profile / "runtime" / "codex"),
            # Windows has no pidfd API; the guard is exercised for real in the
            # Linux-only suites, so the logic tests present it as available.
            mock.patch.object(NATIVE.signal, "pidfd_send_signal", lambda *args: None,
                              create=True),
            mock.patch.object(NATIVE, "DRAIN", 1),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.send_patch = mock.patch.object(NATIVE, "_drain_send", self._send)
        self.send_patch.start()
        self.addCleanup(self.send_patch.stop)

    def _send(self, handle):
        record = json.loads((self.profile / NATIVE.DRAIN_FILE).read_text(encoding="utf-8"))
        self.assertEqual(record["process"], self.observed)
        self.assertEqual(record["state"], "drain_requested")
        self.signals.append(handle)

    def test_request_is_persisted_before_one_signal(self):
        result = NATIVE.drain(self.profile, REVISION, expected_process=self.observed, wait_seconds=0)
        self.assertEqual(self.signals, ["handle:" + str(self.observed["pid"])])
        self.assertEqual(result["state"], "draining")
        self.assertTrue(result["drain_requested"])
        self.assertFalse(result["exited"])
        self.assertFalse(result["idle"])
        self.assertEqual(result["process"], self.observed)
        self.assertEqual(result["revision"], REVISION)
        self.assertEqual(result["signal"], "SIGHUP")
        record = json.loads((self.profile / NATIVE.DRAIN_FILE).read_text(encoding="utf-8"))
        self.assertEqual(record["state"], "signalled")
        self.assertEqual(record["signal"], "SIGHUP")

    def test_repeat_call_never_signals_again(self):
        NATIVE.drain(self.profile, REVISION, expected_process=self.observed, wait_seconds=0)
        second = NATIVE.drain(self.profile, REVISION, expected_process=self.observed, wait_seconds=0)
        self.assertEqual(len(self.signals), 1)
        self.assertEqual(second["state"], "draining")
        self.assertTrue(second["drain_requested"])
        self.assertFalse(second["signal_sent"])
        self.assertEqual(second["process"], self.observed)
        self.assertFalse(second["idle"])

    def test_exit_without_lock_release_is_not_success(self):
        NATIVE.drain(self.profile, REVISION, expected_process=self.observed, wait_seconds=0)
        self.state["gone"] = True
        pending = NATIVE.drain(self.profile, REVISION, expected_process=self.observed, wait_seconds=0)
        self.assertFalse(pending["exited"])
        self.assertFalse(pending["idle"])
        self.assertEqual(pending["state"], "draining")
        self.assertEqual(pending["process"], self.observed)
        self.assertEqual(len(self.signals), 1)
        self.state["lock_released"] = True
        final = NATIVE.drain(self.profile, REVISION, expected_process=self.observed, wait_seconds=0)
        self.assertTrue(final["exited"])
        self.assertEqual(final["state"], "exited")
        self.assertIsNone(final["process"])
        self.assertTrue(final["idle"])
        self.assertTrue(final["lock_released"])
        self.assertEqual(len(self.signals), 1)
        record = json.loads((self.profile / NATIVE.DRAIN_FILE).read_text(encoding="utf-8"))
        self.assertEqual(record["state"], "drained")

    def test_expected_process_mismatch_refuses(self):
        with self.assertRaises(NATIVE.RemoteDrainError) as raised:
            NATIVE.drain(self.profile, REVISION, expected_process=_process(pid=77), wait_seconds=0)
        self.assertEqual(raised.exception.code, "remote_drain_identity_changed")
        self.assertEqual(self.signals, [])

    def test_unknown_binary_is_refused_without_signal(self):
        # The digest allowlist is checked first: an unaudited artifact never
        # reaches the marker or caught-signal checks.
        _write(self.profile / "runtime" / "codex", b"stock build")
        stock = NATIVE._drain_digest(self.profile / "runtime" / "codex")
        with mock.patch.object(NATIVE, "DRAIN_AUDITED_SHA256", frozenset({stock})):
            with self.assertRaises(NATIVE.RemoteDrainError) as raised:
                NATIVE.drain(self.profile, REVISION, expected_process=self.observed, wait_seconds=0)
        self.assertEqual(raised.exception.code, "remote_drain_unsupported")
        self.assertEqual(self.signals, [])

    def test_unaudited_binary_is_refused_without_signal(self):
        _write(self.profile / "runtime" / "codex", b"audited-looking build")
        with self.assertRaises(NATIVE.RemoteDrainError) as raised:
            NATIVE.drain(self.profile, REVISION, expected_process=self.observed, wait_seconds=0)
        self.assertEqual(raised.exception.code, "remote_drain_unaudited")
        self.assertEqual(self.signals, [])

    def test_missing_pidfd_api_refuses_before_any_journal_write(self):
        with mock.patch.object(NATIVE, "DRAIN", None):
            with self.assertRaises(NATIVE.RemoteDrainError) as raised:
                NATIVE.drain(self.profile, REVISION, expected_process=self.observed, wait_seconds=0)
        self.assertEqual(raised.exception.code, "remote_drain_unsupported")
        self.assertFalse((self.profile / NATIVE.DRAIN_FILE).exists())
        self.assertEqual(self.signals, [])

    def test_process_without_hangup_handler_is_refused(self):
        self.state["alive"] = False
        with self.assertRaises(NATIVE.RemoteDrainError) as raised:
            NATIVE.drain(self.profile, REVISION, expected_process=self.observed, wait_seconds=0)
        self.assertEqual(raised.exception.code, "remote_drain_unsupported")
        self.assertEqual(self.signals, [])

    def test_gone_process_without_expected_identity_is_reported(self):
        self.state["gone"] = True
        self.state["lock_released"] = True
        result = NATIVE.drain(self.profile, REVISION, wait_seconds=0)
        self.assertTrue(result["exited"])
        self.assertTrue(result["idle"])
        self.assertIsNone(result["process"])
        self.assertFalse(result["drain_requested"])
        self.assertEqual(self.signals, [])

    def test_unaudited_digest_is_refused(self):
        with mock.patch.object(NATIVE, "DRAIN_AUDITED_SHA256", frozenset()):
            with self.assertRaises(NATIVE.RemoteDrainError) as raised:
                NATIVE.drain(self.profile, REVISION, expected_process=self.observed, wait_seconds=0)
        self.assertEqual(raised.exception.code, "remote_drain_unaudited")
        self.assertEqual(self.signals, [])

    def test_manager_digest_must_match_the_installed_artifact(self):
        with self.assertRaises(NATIVE.RemoteDrainError) as raised:
            NATIVE.drain(self.profile, REVISION, expected_process=self.observed, wait_seconds=0,
                         trusted_sha256="f" * 64)
        self.assertEqual(raised.exception.code, "remote_drain_unaudited")
        self.assertEqual(self.signals, [])

    def test_markers_are_json_serializable_text(self):
        result = NATIVE._drain_result("draining", self.observed, REVISION, drain_requested=True,
                                      exited=False, lock_released=False, idle=False,
                                      markers=NATIVE.DRAIN_MARKERS)
        encoded = json.dumps(result)
        self.assertEqual(json.loads(encoded)["markers"],
                         sorted(marker.decode() for marker in NATIVE.DRAIN_MARKERS))

    def test_unsent_request_is_retried_exactly_once(self):
        NATIVE._drain_write(self.profile, {"schema": NATIVE.DRAIN_SCHEMA,
                                           "state": "drain_requested", "process": self.observed})
        result = NATIVE.drain(self.profile, REVISION, expected_process=self.observed, wait_seconds=0)
        self.assertEqual(len(self.signals), 1)
        self.assertTrue(result["signal_sent"])
        record = json.loads((self.profile / NATIVE.DRAIN_FILE).read_text(encoding="utf-8"))
        self.assertEqual(record["state"], "signalled")

    def test_signalled_request_is_never_sent_again(self):
        NATIVE._drain_write(self.profile, {"schema": NATIVE.DRAIN_SCHEMA, "state": "signalled",
                                           "process": self.observed, "revision": REVISION,
                                           "runtime_sha256": self.digest})
        result = NATIVE.drain(self.profile, REVISION, expected_process=self.observed, wait_seconds=0)
        self.assertEqual(self.signals, [])
        self.assertFalse(result["signal_sent"])
        self.assertEqual(result["record_state"], "signalled")
        self.assertTrue(result["drain_requested"])
        self.assertFalse(result["idle"])

    def test_signalled_poll_rechecks_capability_only_when_sending(self):
        calls = []
        original = NATIVE._drain_capability

        def counted(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        with mock.patch.object(NATIVE, "_drain_capability", counted):
            first = NATIVE.drain(self.profile, REVISION, expected_process=self.observed, wait_seconds=0)
            # A poll of an already signalled process must stay cheap: no digest
            # and no marker scan (the dev/inode stat stays, it is microsecond
            # work and is how the exact process is validated).
            with mock.patch.object(NATIVE, "drain_markers",
                                   side_effect=AssertionError("marker scan on poll")), \
                    mock.patch.object(NATIVE, "_drain_digest",
                                      side_effect=AssertionError("digest on poll")):
                second = NATIVE.drain(self.profile, REVISION, expected_process=self.observed,
                                      wait_seconds=0)
                third = NATIVE.drain(self.profile, REVISION, expected_process=self.observed,
                                     wait_seconds=0)
        self.assertEqual(len(calls), 1)
        self.assertTrue(first["signal_sent"])
        self.assertFalse(second["signal_sent"])
        self.assertFalse(third["signal_sent"])
        self.assertEqual(len(self.signals), 1)

    def test_poll_of_a_record_outside_the_allowlist_is_refused(self):
        NATIVE._drain_write(self.profile, {"schema": NATIVE.DRAIN_SCHEMA, "state": "signalled",
                                           "process": self.observed, "revision": REVISION,
                                           "runtime_sha256": "a" * 64})
        with self.assertRaises(NATIVE.RemoteDrainError) as raised:
            NATIVE.drain(self.profile, REVISION, expected_process=self.observed, wait_seconds=0)
        self.assertEqual(raised.exception.code, "remote_drain_unverified")
        self.assertEqual(self.signals, [])

    def test_completed_record_is_retired_for_a_new_process(self):
        old = _process(pid=999)
        NATIVE._drain_write(self.profile, {"schema": NATIVE.DRAIN_SCHEMA, "state": "drained",
                                           "process": old, "lock_released": True})
        result = NATIVE.drain(self.profile, REVISION, expected_process=self.observed, wait_seconds=0)
        self.assertEqual(len(self.signals), 1)
        self.assertTrue(result["signal_sent"])
        record = json.loads((self.profile / NATIVE.DRAIN_FILE).read_text(encoding="utf-8"))
        self.assertEqual(record["process"], self.observed)
        self.assertEqual(record["state"], "signalled")
        self.assertEqual(record["runtime_sha256"], self.digest)

    def test_record_for_a_live_other_process_still_refuses(self):
        self.state["old_gone"] = False
        NATIVE._drain_write(self.profile, {"schema": NATIVE.DRAIN_SCHEMA, "state": "drained",
                                           "process": _process(pid=999)})
        with self.assertRaises(NATIVE.RemoteDrainError) as raised:
            NATIVE.drain(self.profile, REVISION, expected_process=self.observed, wait_seconds=0)
        self.assertEqual(raised.exception.code, "remote_drain_identity_changed")
        self.assertEqual(self.signals, [])


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux SIGHUP")
class DrainSendTests(unittest.TestCase):
    def test_send_uses_the_signal_module_pidfd_api(self):
        calls = []
        with mock.patch.object(NATIVE.signal, "pidfd_send_signal",
                               side_effect=lambda *args: calls.append(args), create=True):
            NATIVE._drain_send(7)
        self.assertEqual(calls, [(7, NATIVE.signal.SIGHUP, None, 0)])


class ExecutableIdentityTests(unittest.TestCase):
    """A replaced path with identical bytes must not inherit the old audit."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.first = self.dir / "first"
        self.second = self.dir / "second"
        _write(self.first, b"identical bytes")
        _write(self.second, b"identical bytes")

    def test_replaced_path_with_identical_bytes_is_a_different_inode(self):
        self.assertFalse(NATIVE._same_file_stat(self.first, self.second))
        self.assertTrue(NATIVE._same_file_stat(self.first, self.first))

    def test_missing_live_path_is_never_a_match(self):
        self.assertFalse(NATIVE._same_file_stat(self.dir / "absent", self.first))

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux /proc/self/exe")
    def test_running_executable_link_must_share_the_inode(self):
        import shutil
        live = Path("/proc/self/exe")
        runtime = self.dir / "runtime"
        runtime.mkdir()
        descriptor = {"runtime": str(runtime)}
        # Same bytes, new inode: the descriptor path no longer describes the
        # running process and the helper must refuse before any signal.
        shutil.copyfile(live.resolve(), runtime / "codex")
        self.assertEqual(NATIVE._same_file_stat(live, runtime / "codex"), False)
        with self.assertRaises(NATIVE.RemoteDrainError) as raised:
            NATIVE._drain_executable(descriptor, os.getpid())
        self.assertEqual(raised.exception.code, "remote_drain_identity_changed")
        # The matching direction needs a process started from the descriptor
        # path; BootstrapLifecycleTests covers it with a real child, so the
        # positive control here stays with the same inode twice.
        self.assertTrue(NATIVE._same_file_stat(runtime / "codex", runtime / "codex"))


@unittest.skipUnless(sys.platform.startswith("linux") and hasattr(__import__("signal"), "pidfd_send_signal"),
                     "Linux pidfd")
class BootstrapLifecycleTests(unittest.TestCase):
    """Real in-memory bootstrap round trip with two successive lifecycles.

    The fixture runtime is a copy of this interpreter with the drain markers
    appended, so the child process it starts is the only process ever signalled.
    The bootstrap path keeps the manager's real profile layout, so the fixture
    profile is one new, uniquely named directory under the profiles root and is
    removed again by this test.
    """

    def setUp(self):
        import shutil
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.profile_id = "6b1f2c7d-35a0-4c8e-b1f4-9d2e6a7c8b90"
        profiles_root = (Path.home() / ".local/share/codex-control-center/profiles").resolve()
        self.profile = profiles_root / self.profile_id
        if self.profile.exists():
            self.skipTest("fixture profile path already exists")
        self.addCleanup(lambda: shutil.rmtree(self.profile, ignore_errors=True))
        (self.profile / "definitions").mkdir(parents=True)
        (self.root / "runtime").mkdir()
        self.binary = self.root / "runtime" / "codex"
        shutil.copyfile(os.readlink("/proc/self/exe"), self.binary)
        os.chmod(self.binary, 0o700)
        with open(self.binary, "ab") as stream:
            stream.write(b"".join(NATIVE.DRAIN_MARKERS))
        self.digest = NATIVE._drain_digest(self.binary)
        _write(self.profile / "native-start.lock", b"")
        _write(self.profile / "instance.lock", b"")
        self.descriptor = {"revision": REVISION, "profile_id": self.profile_id,
                           "host_identity": "fixture", "runtime": str(self.root / "runtime"),
                           "definition": str(self.profile / "definitions" / REVISION)}
        _write(self.profile / "definitions" / (REVISION + ".json"), json.dumps(self.descriptor))

    def _spawn(self):
        import subprocess
        script = self.root / "child.py"
        _write(script, "import pathlib, signal, sys, time\n"
                       "marker = pathlib.Path(sys.argv[1])\n"
                       "signal.signal(signal.SIGHUP, lambda *a: (marker.write_text('hup'), sys.exit(0)))\n"
                       "while True: time.sleep(0.05)\n")
        environment = dict(os.environ, PYTHONHOME=sys.base_prefix)
        child = subprocess.Popen([str(self.binary), str(script), str(self.root / "hup.txt")],
                                 env=environment)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        # The fixture is ready when this exact process has installed its
        # handler, which is also the capability the helper requires.
        deadline = __import__("time").monotonic() + 10
        while child.poll() is None and NATIVE.catches_hangup(child.pid) is not True:
            self.assertLess(__import__("time").monotonic(), deadline, "fixture never handled SIGHUP")
            __import__("time").sleep(0.02)
        _write(self.profile / "native-instance.json",
               json.dumps({"pid": child.pid, "process_start": NATIVE._process_start(child.pid),
                           "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                           "revision": REVISION,
                           "socket": str(NATIVE.socket_path(self.profile))}))
        return child

    def _bootstrap(self, wait_seconds=15):
        import subprocess
        sys.path.insert(0, str(ROOT / "scripts"))
        from manager_core import remote_maintenance as rm
        source = (ROOT / "scripts" / "remote_helpers" / "native_controller.py").read_text(encoding="utf-8")
        audited = next(iter(NATIVE.DRAIN_AUDITED_SHA256))
        source = source.replace('"' + audited + '"', '"' + self.digest + '"')
        # The real bootstrap runs from the deployment layout; the fixture
        # descriptor keeps the profile path check intact and only replaces the
        # immutable runtime binding with this temp binary.
        source += ("\n\ndef _descriptor(profile, revision):\n"
                   "    if revision != " + repr(REVISION) + " or profile.name != "
                   + repr(self.profile_id) + ":\n        raise ValueError('fixture descriptor')\n"
                   "    return " + repr(self.descriptor) + "\n")
        modules = {name: (ROOT / "scripts" / "remote_helpers" / (name + ".py")).read_text(encoding="utf-8")
                   for name in ("launch", "ws_client", "native_controller", "maintenance")}
        modules["native_controller"] = source
        observed = json.loads((self.profile / "native-instance.json").read_text(encoding="utf-8"))
        payload = {"modules": modules,
                   "request": {"binding": {"profile_id": self.profile_id,
                                           "remote_launcher": str(self.profile / "launch.py"),
                                           "revision": REVISION},
                               "operation": "drain", "expected_process": observed,
                               "wait_seconds": wait_seconds,
                               "expected_runtime_sha256": self.digest}}
        completed = subprocess.run([sys.executable, "-c", rm.BOOTSTRAP],
                                   input=json.dumps(payload).encode(), capture_output=True, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stdout[:400])
        reply = json.loads(completed.stdout.decode())
        self.assertTrue(reply["ok"], reply)
        return reply["result"]

    def test_bootstrap_round_trip_reports_a_complete_drain(self):
        self._spawn()
        result = self._bootstrap()
        self.assertEqual(result["state"], "exited")
        self.assertTrue(result["idle"])
        self.assertTrue(result["exited"])
        self.assertIsNone(result["process"])
        self.assertEqual(result["runtime_sha256"], self.digest)
        self.assertTrue(all(isinstance(marker, str) for marker in result["markers"]))
        self.assertEqual((self.root / "hup.txt").read_text(encoding="utf-8"), "hup")

    def test_two_successive_lifecycles_with_real_processes(self):
        with mock.patch.object(NATIVE, "_descriptor", lambda profile, revision: self.descriptor), \
                mock.patch.object(NATIVE, "DRAIN_AUDITED_SHA256", frozenset({self.digest})):
            first_child = self._spawn()
            first = NATIVE.drain(self.profile, REVISION, expected_process=json.loads(
                (self.profile / "native-instance.json").read_text(encoding="utf-8")), wait_seconds=15)
            self.assertEqual(first["state"], "exited")
            self.assertTrue(first["idle"])
            self.assertIsNone(first["process"])
            self.assertEqual(first_child.wait(timeout=10), 0)
            second_child = self._spawn()
            second = NATIVE.drain(self.profile, REVISION, expected_process=json.loads(
                (self.profile / "native-instance.json").read_text(encoding="utf-8")), wait_seconds=15)
        self.assertEqual(second["state"], "exited")
        self.assertTrue(second["idle"])
        self.assertEqual(second_child.wait(timeout=10), 0)
        record = json.loads((self.profile / NATIVE.DRAIN_FILE).read_text(encoding="utf-8"))
        self.assertEqual(record["state"], "drained")
        self.assertEqual(record["process"]["pid"], second_child.pid)


class MaintenanceDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.profile_id = "3f1c0a54-6d59-4f3f-9a2b-0f7a1b2c3d4e"
        self.launcher = (Path.home() / ".local/share/codex-control-center/profiles"
                         / self.profile_id / "launch.py")
        self.binding = {"profile_id": self.profile_id, "remote_launcher": str(self.launcher),
                        "revision": REVISION}
        self.fake = mock.Mock()
        self.fake.drain.return_value = {"state": "draining", "drain_requested": True}
        self.patch = mock.patch.object(MAINTENANCE, "native", self.fake)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_drain_requires_an_observed_process(self):
        with self.assertRaises(ValueError):
            MAINTENANCE.dispatch({"binding": self.binding, "operation": "drain"})
        self.fake.drain.assert_not_called()

    def test_drain_is_forwarded_with_the_exact_process(self):
        observed = _process()
        result = MAINTENANCE.dispatch({"binding": self.binding, "operation": "drain",
                                       "expected_process": observed, "wait_seconds": 3})
        self.assertEqual(result, {"state": "draining", "drain_requested": True})
        profile, revision = self.launcher.parent, REVISION
        self.fake.drain.assert_called_once_with(profile, revision, expected_process=observed,
                                                wait_seconds=3, trusted_sha256=None)

    def test_unsupported_lifecycle_operation_still_fails(self):
        with self.assertRaises(ValueError):
            MAINTENANCE.dispatch({"binding": self.binding, "operation": "restart"})


if __name__ == "__main__":
    unittest.main()
