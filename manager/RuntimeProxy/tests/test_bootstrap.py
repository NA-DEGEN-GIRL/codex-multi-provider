"""Fixture-only tests for the native bootstrap; never launch Codex or read auth."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
EXE = ROOT / "manager" / "RuntimeProxy" / "bin" / "Release" / "net10.0-windows" / "Codex.ControlCenter.RuntimeProxy.exe"
FIXTURE = r'''
import json, os, sys
args = sys.argv[1:]
if args[0:2] == ["--", "echo-stdin"]:
    sys.stdout.buffer.write(sys.stdin.buffer.read())
elif args[0:2] == ["--", "exit-37"]:
    sys.exit(37)
else:
    print(json.dumps({"args": args, "profile": os.getenv("CODEX_MANAGER_PROFILE_ID")}, ensure_ascii=False))
'''


class BootstrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not EXE.is_file():
            raise RuntimeError("Build manager/RuntimeProxy in Release before running this fixture suite.")
        cls.work = (ROOT / "work" / "runtime-bootstrap-tests").resolve()
        cls.work.mkdir(parents=True, exist_ok=True)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fixture # 한글 ", dir=self.work)
        self.fixture_root = Path(self.temp.name).resolve()
        self.assertTrue(self.fixture_root.is_relative_to(self.work))
        script = self.fixture_root / "scripts" / "manager_core" / "runtime_proxy.py"
        script.parent.mkdir(parents=True)
        script.write_text(FIXTURE, encoding="utf-8")
        self.environment = dict(os.environ)
        self.environment.update(
            CODEX_MANAGER_ROOT=str(self.fixture_root),
            CODEX_MANAGER_PYTHON=sys.executable,
            CODEX_MANAGER_PROFILE_ID="fixture-profile",
        )

    def tearDown(self):
        # Verify the resolved deletion target remains under this suite's dedicated workspace.
        self.assertTrue(self.fixture_root.is_relative_to(self.work))
        self.temp.cleanup()

    def run_proxy(self, *arguments, input=None, environment=None):
        return subprocess.run(
            [str(EXE), *arguments], input=input, capture_output=True,
            env=environment or self.environment, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    def test_original_argv_preserved_without_shell(self):
        arguments = ["app-server", "--config", 'a="space and quote"', "한글 # 값", "$(not_a_shell)", "a&b|c", "", "line\nbreak", "C:\\space path\\", 'a\\\\"b']
        completed = self.run_proxy(*arguments)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["args"], ["--", *arguments])
        self.assertEqual(result["profile"], "fixture-profile")
        self.assertEqual(completed.stderr, b"")

    def test_inherited_stdin_stdout_preserve_bytes(self):
        payload = b'{"id":1,"command":"test"}\n' + "한글\n".encode() + b"\x00\xff"
        completed = self.run_proxy("echo-stdin", input=payload)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, payload)

    def test_exit_code_preserved(self):
        completed = self.run_proxy("exit-37")
        self.assertEqual(completed.returncode, 37)
        self.assertEqual(completed.stdout, b"")

    def test_version_is_forwarded(self):
        completed = self.run_proxy("--version")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["args"], ["--", "--version"])

    def test_invalid_interpreter_is_safe_error(self):
        environment = dict(self.environment, CODEX_MANAGER_PYTHON="not-an-executable secret-fixture-value")
        completed = self.run_proxy("--version", environment=environment)
        self.assertEqual(completed.returncode, 127)
        self.assertEqual(completed.stdout, b"")
        self.assertNotIn(b"secret-fixture-value", completed.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
