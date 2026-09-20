"""Exercise the actual Explorer entry points without agent-only PATH entries or API calls."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == 'nt', 'Windows desktop launcher tests')
class ExplorerLauncherTests(unittest.TestCase):
    def setUp(self):
        self.env = {key: value for key, value in os.environ.items() if not key.upper().startswith('CODEX_')}
        self.env['PATH'] = str(Path(os.environ['SystemRoot']) / 'System32')
        self.cmd = str(Path(os.environ['SystemRoot']) / 'System32/cmd.exe')

    def run_entry(self, entry, *arguments, timeout=45):
        # Explorer does not promise the project directory as the starting directory.
        with tempfile.TemporaryDirectory(prefix='codex launcher ') as directory:
            result = subprocess.run([self.cmd, '/d', '/c', str(ROOT / entry), *arguments],
                                    cwd=directory, env=self.env, capture_output=True,
                                    text=True, encoding='utf-8', errors='replace', timeout=timeout,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_both_entry_points_find_dependencies_without_path(self):
        for entry, target in [('Open-Lab.cmd', 'Lab'), ('Open-Experimental-Codex.cmd', 'Desktop')]:
            with self.subTest(entry=entry):
                status = json.loads(self.run_entry(entry, '-Check').stdout)
                self.assertEqual(status['target'], target)
                for name in ('powershell', 'python', 'pythonw', 'git'):
                    self.assertTrue(Path(status[name]).is_absolute())
                    self.assertTrue(Path(status[name]).is_file(), status[name])
                # The app's own read-only check must also work with the propagated PATH.
                if target == 'Desktop':
                    child_env = dict(self.env)
                    child_env['PATH'] = ';'.join(str(Path(status[name]).parent) for name in ('powershell', 'python', 'git')) + ';' + child_env['PATH']
                    result = subprocess.run([status['python'], str(ROOT / 'scripts/desktop_launch.py'), '--check'],
                                            cwd=ROOT, env=child_env, capture_output=True,
                                            text=True, encoding='utf-8', timeout=30,
                                            creationflags=subprocess.CREATE_NO_WINDOW)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(json.loads(result.stdout)['errors'], [])

    def test_lab_entry_reaches_live_gui_and_python_child(self):
        evidence = ROOT / 'work/manager-stream-observed.json'
        started = time.time()
        # Existing report discovery and GUI bootstrap can exceed 45 seconds.
        # The live-child observation and stream-completion assertions stay strict.
        self.run_entry('Open-Lab.cmd', '-SmokeScenario', 'stream', '-Wait', timeout=120)
        self.assertGreaterEqual(evidence.stat().st_mtime, started - 1)
        self.assertTrue(json.loads(evidence.read_text())['observed_before_exit'])
        log = (ROOT / 'work/manager-smoke-stream.log').read_text(encoding='utf-8')
        for marker in ('STREAM_OUTPUT_FIRST', 'STREAM_STDERR_FIRST', 'STREAM_OUTPUT_LAST', '한글'):
            self.assertIn(marker, log)


if __name__ == '__main__':
    unittest.main()
