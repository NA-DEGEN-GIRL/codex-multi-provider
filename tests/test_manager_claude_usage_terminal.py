"""The usage terminal accepts a fixed local command, never arbitrary prompts."""
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.claude_usage_terminal import _Terminal, parse_screen, query


SCREEN = '''Current session
25% used
Resets 11:42am (UTC)
Current week (all models)
66% used
Resets Oct 3, 9am (UTC)
Current week (Sonnet only)
99% used
'''


class ScreenTests(unittest.TestCase):
    def test_aggregate_rows_and_displayed_time_only(self):
        windows = parse_screen(SCREEN)
        self.assertEqual(['five_hour', 'seven_day'], [w['key'] for w in windows])
        self.assertEqual([25, 66], [w['used_percent'] for w in windows])
        self.assertEqual('Resets 11:42am (UTC)', windows[0]['reset_text'])
        self.assertIsNone(windows[0]['resets_at'])

    def test_ansi_labels_and_actual_zero(self):
        windows = parse_screen('\x1b[2JCurrent session\r\n\x1b[32m0% used\x1b[m\r\n')
        self.assertEqual(0, windows[0]['used_percent'])

    def test_missing_row_unknown_not_zero(self):
        self.assertIsNone(parse_screen('Current session\nLoading...\n'))
        self.assertIsNone(parse_screen('Current week (Sonnet only)\n24% used\n'))
        self.assertIsNone(parse_screen('Current session\n200% used\n'))
        self.assertIsNone(parse_screen('Current session\n-20% used\n'))
        self.assertIsNone(parse_screen('a'*262145))

    def test_unrelated_text_never_escapes(self):
        windows = parse_screen('private@example.invalid\n'+SCREEN+'secret-token\n')
        self.assertNotIn('private', repr(windows))
        self.assertNotIn('secret', repr(windows))
        windows = parse_screen('Current session\n25% used\nResets private@example.invalid\n')
        self.assertNotIn('reset_text', windows[0])


@unittest.skipUnless(os.name == 'nt', 'Windows ConPTY adapter')
class TerminalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.instance = None

    def fake(self, text):
        owner = self
        class Terminal:
            overflow = False
            def __init__(self, command, environment, cwd):
                owner.instance = self
                self.command, self.environment, self.cwd = command, environment, cwd
                self.closed, self.writes = False, []
            def text(self): return text
            def close(self): self.closed = True
            def write(self, value): self.writes.append(value)
        return Terminal

    def run_query(self, screen, timeout=.1):
        with patch('manager_core.claude_usage_terminal._Terminal', self.fake(screen)), \
                patch('manager_core.claude_auth.config_dir', return_value=self.directory), \
                patch('manager_core.claude_auth.discover_cli', return_value=Path('official-claude.exe')):
            return query('00000000-0000-0000-0000-000000000000', timeout=timeout,
                         environ={'ANTHROPIC_API_KEY': 'forbidden', 'OPENAI_API_KEY': 'forbidden'})

    def test_fixed_local_command_scrub_and_cleanup(self):
        windows, error = self.run_query(SCREEN)
        self.assertEqual(2, len(windows))
        self.assertIsNone(error)
        self.assertTrue(self.instance.closed)
        self.assertEqual('/usage', self.instance.command[-1])
        self.assertIn('--safe-mode', self.instance.command)
        self.assertEqual('0', self.instance.command[self.instance.command.index('--max-turns')+1])
        self.assertNotIn('ANTHROPIC_API_KEY', self.instance.environment)
        self.assertTrue(Path(self.instance.cwd).is_dir())
        self.assertEqual([], list(Path(self.instance.cwd).iterdir()))

    def test_onboarding_never_initiates_login(self):
        for screen in ('Choose the text style', '1. Claude account with subscription', 'Please sign in'):
            windows, error = self.run_query(screen)
            self.assertIsNone(windows)
            self.assertEqual('onboarding_required', error)
            self.assertEqual([], self.instance.writes)
            self.assertTrue(self.instance.closed)

    def test_timeout_cleanup(self):
        windows, error = self.run_query('Loading...')
        self.assertIsNone(windows)
        self.assertEqual('refresh_timeout', error)
        self.assertTrue(self.instance.closed)

    def test_native_conpty_captures_output_and_closes_child(self):
        # No Claude process or account is involved in this native handle test.
        terminal = _Terminal(['cmd.exe', '/c', 'echo quota-adapter-probe'], dict(os.environ), self.directory)
        try:
            deadline = time.monotonic()+5
            while 'quota-adapter-probe' not in terminal.text() and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertIn('quota-adapter-probe', terminal.text())
        finally:
            terminal.close()
        self.assertIsNone(terminal.process.hProcess)
        self.assertFalse(terminal.console)

    def test_setup_is_only_visible_official_cli_and_uses_same_profile(self):
        from manager_core.claude_auth import launch_setup
        with patch('manager_core.claude_auth.config_dir', return_value=self.directory), \
                patch('manager_core.claude_auth.discover_cli', return_value=Path('official-claude.exe')), \
                patch('manager_core.claude_auth.cli_version', return_value='2.1.282'), \
                patch('manager_core.claude_auth.subprocess.Popen') as spawn:
            spawn.return_value.pid = 1234
            result = launch_setup('00000000-0000-0000-0000-000000000000',
                                  environ={'ANTHROPIC_API_KEY': 'not-inherit', 'CLAUDE_CONFIG_DIR': 'wrong'})
        command = spawn.call_args.args[0]
        options = spawn.call_args.kwargs
        self.assertEqual('/usage', command[-1])
        self.assertNotIn('auth', command)
        self.assertEqual(str(self.directory), options['env']['CLAUDE_CONFIG_DIR'])
        self.assertNotIn('ANTHROPIC_API_KEY', options['env'])
        self.assertEqual(16, options['creationflags'])
        self.assertNotIn('startupinfo', options)
        self.assertEqual('setup_started', result['status'])

    def test_probe_directory_never_trusts_user_files(self):
        from manager_core.claude_auth import ClaudeError, usage_directory
        cwd = self.directory / 'manager-usage'
        cwd.mkdir()
        (cwd / 'user-file.txt').write_text('leave this alone', encoding='utf-8')
        with patch('manager_core.claude_auth.config_dir', return_value=self.directory):
            with self.assertRaises(ClaudeError):
                usage_directory('00000000-0000-0000-0000-000000000000')
        self.assertEqual('leave this alone', (cwd / 'user-file.txt').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
