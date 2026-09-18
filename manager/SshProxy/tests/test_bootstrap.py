"""Headless bootstrap fixtures. No Codex GUI, real SSH, account files or credentials."""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[3]
EXE = ROOT / "manager" / "SshProxy" / "bin" / "Release" / "net10.0-windows" / "ssh.exe"
FIXTURE = r'''
import json, os, pathlib, subprocess, sys, time
args = sys.argv[1:]
mode = args[1] if len(args) > 1 else ""
if mode == "echo-stdin":
    sys.stdout.buffer.write(sys.stdin.buffer.read())
elif mode == "half-close":
    data = sys.stdin.buffer.read()
    sys.stdout.buffer.write(data + b"\x00after-eof\xff")
    sys.stdout.buffer.flush()
    time.sleep(0.15)
    sys.stderr.buffer.write(b"\xfflate-stderr\x00")
elif mode == "exit-37":
    sys.exit(37)
elif mode == "tree":
    directory = pathlib.Path(args[2])
    child = subprocess.Popen([sys.executable, __file__, "--", "descendant", str(directory)])
    temporary = directory / "parent.json.tmp"
    temporary.write_text(json.dumps({"python": os.getpid(), "child": child.pid}))
    temporary.replace(directory / "parent.json")
    child.wait()
elif mode == "descendant":
    directory = pathlib.Path(args[2])
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    temporary = directory / "child.json.tmp"
    temporary.write_text(json.dumps({"child": os.getpid(), "grandchild": child.pid}))
    temporary.replace(directory / "child.json")
    child.wait()
else:
    print(json.dumps({"args": args, "manifest": os.getenv("CODEX_MANAGER_SSH_BINDINGS")}, ensure_ascii=False))
'''


@unittest.skipUnless(os.name == "nt", "Windows native bootstrap")
class BootstrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not EXE.is_file():
            raise RuntimeError("Build manager/SshProxy in Release before running these fixtures.")
        cls.work = (ROOT / "work" / "ssh-bootstrap-tests").resolve()
        cls.work.mkdir(parents=True, exist_ok=True)
        cls.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        cls.kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        cls.kernel32.OpenProcess.restype = ctypes.c_void_p
        cls.kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        cls.kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        cls.kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        cls.kernel32.CloseHandle.restype = ctypes.c_int

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fixture # 한글 ", dir=self.work)
        self.fixture_root = Path(self.temp.name).resolve()
        self.assertTrue(self.fixture_root.is_relative_to(self.work))
        self.script = self.fixture_root / "ssh fixture.py"
        self.script.write_text(FIXTURE, encoding="utf-8")
        self.environment = dict(os.environ)
        self.environment.pop("CODEX_MANAGER_PYTHON", None)
        self.environment.update(
            CODEX_MANAGER_SSH_PYTHON=sys.executable,
            CODEX_MANAGER_SSH_SCRIPT=str(self.script),
            CODEX_MANAGER_SSH_BINDINGS="fixture-manifest-only",
            PYTHONUTF8="1",
        )

    def tearDown(self):
        # Validate the resolved recursive cleanup target belongs to this dedicated workspace.
        self.assertTrue(self.fixture_root.is_relative_to(self.work))
        self.temp.cleanup()

    def run_proxy(self, *arguments, input=None, environment=None):
        return subprocess.run(
            [str(EXE), *arguments], input=input, capture_output=True,
            env=environment or self.environment, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    def test_original_argv_preserved_without_shell(self):
        arguments = ["-T", "-o", "BatchMode=yes", "remote-dev", 'a="space and quote"',
                     "한글 # 값", "$(not_a_shell)", "a&b|c", "", "line\nbreak",
                     "C:\\space path\\", 'a\\\\"b']
        result = self.run_proxy(*arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        decoded = json.loads(result.stdout)
        self.assertEqual(decoded["args"], ["--", *arguments])
        self.assertEqual(decoded["manifest"], "fixture-manifest-only")
        self.assertEqual(result.stderr, b"")

    def test_inherited_stdin_stdout_preserve_large_binary_stream(self):
        payload = bytes(range(256)) * 8192 + "한글\n".encode()
        result = self.run_proxy("echo-stdin", input=payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, payload)
        self.assertEqual(result.stderr, b"")

    def test_native_probe_ignored_stdin_handle_is_supported(self):
        # The original GUI's n.Hn native probes use stdin='ignore' (Windows NUL),
        # unlike its WebSocket proxy which has a piped input stream.
        result = subprocess.run([str(EXE), 'echo-stdin'], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=self.environment, timeout=15,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b'')
        self.assertEqual(result.stderr, b'')

    def test_stdin_eof_is_half_close_and_keeps_late_output(self):
        result = self.run_proxy("half-close", input=b"\x00input\xfe")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"\x00input\xfe\x00after-eof\xff")
        self.assertEqual(result.stderr, b"\xfflate-stderr\x00")

    def test_exit_code_preserved(self):
        result = self.run_proxy("exit-37")
        self.assertEqual(result.returncode, 37)
        self.assertEqual(result.stdout, b"")

    def test_generic_python_fallback(self):
        environment = dict(self.environment, CODEX_MANAGER_PYTHON=sys.executable)
        environment.pop("CODEX_MANAGER_SSH_PYTHON")
        result = self.run_proxy("--version", environment=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["args"], ["--", "--version"])

    def test_invalid_interpreter_never_falls_back_or_leaks(self):
        environment = dict(self.environment,
                           CODEX_MANAGER_SSH_PYTHON="relative secret-fixture-value.exe",
                           CODEX_MANAGER_PYTHON=sys.executable)
        result = self.run_proxy("secret-command-value", environment=environment)
        self.assertEqual(result.returncode, 127)
        self.assertEqual(result.stdout, b"")
        self.assertNotIn(b"secret-fixture-value", result.stderr)
        self.assertNotIn(b"secret-command-value", result.stderr)

    def test_relative_script_is_rejected_without_exposing_path(self):
        environment = dict(self.environment, CODEX_MANAGER_SSH_SCRIPT="secret-relative-script.py")
        result = self.run_proxy("--version", environment=environment)
        self.assertEqual(result.returncode, 127)
        self.assertEqual(result.stdout, b"")
        self.assertNotIn(b"secret-relative-script", result.stderr)

    def test_parent_forced_kill_terminates_python_and_all_descendants(self):
        process = subprocess.Popen(
            [str(EXE), "tree", str(self.fixture_root)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self.environment, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        handles = []
        try:
            deadline = time.monotonic() + 10
            parent_file, child_file = self.fixture_root / "parent.json", self.fixture_root / "child.json"
            while not (parent_file.exists() and child_file.exists()):
                if process.poll() is not None:
                    self.fail("Bootstrap exited before the fixture tree became ready.")
                if time.monotonic() >= deadline:
                    self.fail("Fixture tree did not become ready.")
                time.sleep(0.02)
            parent, child = json.loads(parent_file.read_text()), json.loads(child_file.read_text())
            self.assertEqual(parent["child"], child["child"])
            for pid in [parent["python"], parent["child"], child["grandchild"]]:
                handle = self.kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
                self.assertTrue(handle, "Fixture process must still exist before its owner is killed.")
                handles.append(handle)
                self.assertEqual(self.kernel32.WaitForSingleObject(handle, 0), 258)
            # Exactly what Electron's child.kill does on Windows: kill the shim only.
            process.kill()
            process.wait(timeout=5)
            for handle in handles:
                self.assertEqual(self.kernel32.WaitForSingleObject(handle, 5000), 0,
                                 "The transport job left a descendant running.")
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(stdout, b"")
            self.assertEqual(stderr, b"")
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
            for handle in handles:
                self.kernel32.CloseHandle(handle)


if __name__ == "__main__":
    unittest.main(verbosity=2)
