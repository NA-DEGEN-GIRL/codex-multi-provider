"""Hidden, bounded Windows pseudo-console for Claude's local /usage command.

No arbitrary prompts, credentials, or saved conversation are accepted. Raw
terminal bytes live only in memory and are never returned by the public probe.
"""
import ctypes
from ctypes import wintypes as w
import os
from pathlib import Path
import re
import subprocess
import threading
import time


_ANSI = re.compile(r'\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1bP[^\x1b]*\x1b\\')


def parse_screen(raw):
    """Return just displayed quota fields. Never return surrounding terminal text."""
    if not isinstance(raw, str) or len(raw) > 262144:
        return None
    text = _ANSI.sub('', raw)
    windows = []
    for key, header in (('five_hour', r'Current session'),
                        ('seven_day', r'Current week(?: \(all models\))?')):
        matches = list(re.finditer(header + r'[ \t]*\r?\n', text))
        for match in reversed(matches):
            # Never confuse a scoped model/extra-usage row with an aggregate row.
            section = text[match.end():match.end()+400]
            section = re.split(r'\bCurrent (?:session|week)\b|\bExtra usage\b', section)[0]
            percent = re.search(r'(?<![\d.+-])(\d{1,3}(?:\.\d+)?)%\s*used\b', section)
            if not percent:
                continue
            used = float(percent.group(1))
            if not 0 <= used <= 100:
                continue
            item = dict(key=key, used_percent=used, resets_at=None)
            reset = re.search(r'\bResets[ \t]+([^\r\n]{1,96})', section)
            if reset:
                label = reset.group(1).strip()
                # Rendered time is less precise than an epoch; retain the actual
                # provider text instead of inventing a timezone/date conversion.
                if re.fullmatch(r'[A-Za-z0-9 ,:()./+_\-]+', label) and any(c.isdigit() for c in label):
                    item['reset_text'] = 'Resets ' + label
            windows.append(item)
            break
    return windows or None


def query(profile_id, *, timeout=18, cli_path=None, environ=None):
    """Run only /usage, stop on onboarding/login, and always close the process tree."""
    from .claude_auth import config_dir, discover_cli, scrub_environment, usage_command, usage_directory
    if os.name != 'nt':
        return None, 'interactive_usage_required'
    directory = config_dir(profile_id, environ)
    directory.mkdir(parents=True, exist_ok=True)
    environment = scrub_environment(directory, environ)
    environment.update(CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1', TZ='UTC')
    cli = discover_cli(cli_path, environ)
    # No saved session is resumed. Safe mode disables user/project hooks,
    # plugins, commands and instructions; the command is a fixed local builtin.
    command = usage_command(cli)
    cwd = usage_directory(profile_id, environ)
    terminal = _Terminal(command, environment, cwd)
    try:
        started, answered, trusted = time.monotonic(), set(), False
        previous_windows, stable_since = None, None
        while time.monotonic()-started < timeout:
            raw = terminal.text()
            if terminal.overflow:
                return None, 'refresh_failed'
            screen = _ANSI.sub('', raw)
            windows = parse_screen(raw)
            if windows:
                if windows != previous_windows:
                    previous_windows, stable_since = windows, time.monotonic()
                # Screen readers write rows separately. Allow the weekly row to
                # arrive rather than returning the first row while it is drawn.
                if len(windows) == 2 or time.monotonic()-stable_since >= .5:
                    return windows, None
            if ('Choose the text style' in screen or 'Claude account with subscription' in screen
                    or 'Please sign in' in screen or 'Log in to Claude' in screen):
                # Onboarding is a user action. Never initiate OAuth or answer
                # an account/billing/consent dialog in a usage probe.
                return None, 'onboarding_required'
            if not trusted and 'Yes, I trust this folder' in screen:
                # Only our dedicated, still-empty cwd may be acknowledged.
                # All customizations/tools are disabled as additional bounds.
                if any(Path(cwd).iterdir()) or Path(cwd).resolve().parent != directory:
                    return None, 'refresh_failed'
                terminal.write('1\r\n')
                trusted = True
            for request, response in (('\x1b[6n', '\x1b[1;1R'),
                                      ('\x1b[?u', '\x1b[?0u'),
                                      ('\x1b[>0q', '\x1bP>|CodexUsageProbe\x1b\\')):
                if request in raw and request not in answered:
                    terminal.write(response)
                    answered.add(request)
            time.sleep(.1)
        return None, 'refresh_timeout'
    finally:
        terminal.close()


class _Coordinate(ctypes.Structure):
    _fields_ = [('X', ctypes.c_short), ('Y', ctypes.c_short)]


class _Startup(ctypes.Structure):
    _fields_ = [('cb', w.DWORD), ('lpReserved', w.LPWSTR), ('lpDesktop', w.LPWSTR),
                ('lpTitle', w.LPWSTR), ('dwX', w.DWORD), ('dwY', w.DWORD),
                ('dwXSize', w.DWORD), ('dwYSize', w.DWORD), ('dwXCountChars', w.DWORD),
                ('dwYCountChars', w.DWORD), ('dwFillAttribute', w.DWORD), ('dwFlags', w.DWORD),
                ('wShowWindow', w.WORD), ('cbReserved2', w.WORD), ('lpReserved2', ctypes.c_void_p),
                ('hStdInput', w.HANDLE), ('hStdOutput', w.HANDLE), ('hStdError', w.HANDLE)]


class _StartupEx(ctypes.Structure):
    _fields_ = [('StartupInfo', _Startup), ('lpAttributeList', ctypes.c_void_p)]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [('hProcess', w.HANDLE), ('hThread', w.HANDLE),
                ('dwProcessId', w.DWORD), ('dwThreadId', w.DWORD)]


class _Terminal:
    """A fresh job and pseudoconsole, never a visible terminal or existing task."""
    def __init__(self, command, environment, cwd):
        if os.name != 'nt':
            raise OSError('Windows pseudoconsole required')
        self.k = k = ctypes.WinDLL('kernel32', use_last_error=True)
        declarations = {
            'CreatePipe': ([ctypes.POINTER(w.HANDLE), ctypes.POINTER(w.HANDLE), ctypes.c_void_p, w.DWORD], w.BOOL),
            'CreatePseudoConsole': ([_Coordinate, w.HANDLE, w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)], ctypes.c_long),
            'ClosePseudoConsole': ([w.HANDLE], None),
            'InitializeProcThreadAttributeList': ([ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.POINTER(ctypes.c_size_t)], w.BOOL),
            'UpdateProcThreadAttribute': ([ctypes.c_void_p, w.DWORD, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p], w.BOOL),
            'DeleteProcThreadAttributeList': ([ctypes.c_void_p], None),
            'CreateProcessW': ([w.LPCWSTR, w.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, w.BOOL, w.DWORD, ctypes.c_void_p, w.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(_ProcessInfo)], w.BOOL),
            'ReadFile': ([w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p], w.BOOL),
            'WriteFile': ([w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p], w.BOOL),
            'TerminateProcess': ([w.HANDLE, w.UINT], w.BOOL),
            'WaitForSingleObject': ([w.HANDLE, w.DWORD], w.DWORD),
            'ResumeThread': ([w.HANDLE], w.DWORD),
            'CloseHandle': ([w.HANDLE], w.BOOL),
        }
        for name, (args, result) in declarations.items():
            function = getattr(k, name)
            function.argtypes, function.restype = args, result
        self.handles, self.console, self.tree, self.attributes = [], w.HANDLE(), None, None
        self.process, self.buffer, self.guard = _ProcessInfo(), bytearray(), threading.Lock()
        self.overflow, self.reader = False, None
        try:
            input_read, self.input = w.HANDLE(), w.HANDLE()
            self.output, output_write = w.HANDLE(), w.HANDLE()
            self._check(k.CreatePipe(ctypes.byref(input_read), ctypes.byref(self.input), None, 0))
            self.handles.extend([input_read, self.input])
            self._check(k.CreatePipe(ctypes.byref(self.output), ctypes.byref(output_write), None, 0))
            self.handles.extend([self.output, output_write])
            if k.CreatePseudoConsole(_Coordinate(180, 60), input_read, output_write, 0, ctypes.byref(self.console)):
                raise OSError('Could not create pseudoconsole')
            for handle in (input_read, output_write):
                k.CloseHandle(handle)
                self.handles.remove(handle)
            size = ctypes.c_size_t()
            k.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
            self.attributes = ctypes.create_string_buffer(size.value)
            self._check(k.InitializeProcThreadAttributeList(self.attributes, 1, 0, ctypes.byref(size)))
            self._check(k.UpdateProcThreadAttribute(self.attributes, 0, 0x00020016, self.console,
                                                  ctypes.sizeof(w.HANDLE), None, None))
            startup = _StartupEx()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            # Null explicit standard handles let the child acquire its own
            # pseudoconsole handles instead of inheriting the manager's pipes.
            startup.StartupInfo.dwFlags = 0x100
            startup.lpAttributeList = ctypes.cast(self.attributes, ctypes.c_void_p)
            env = ctypes.create_unicode_buffer('\0'.join(k+'='+v for k, v in sorted(environment.items()))+'\0\0')
            args = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
            self._check(k.CreateProcessW(None, args, None, None, False, 0x00080000 | 0x00000400 | 4,
                                         env, str(cwd), ctypes.byref(startup), ctypes.byref(self.process)))
            from .claude_runner import ProcessTree
            self._handle, self.pid = self.process.hProcess, self.process.dwProcessId
            self.tree = ProcessTree(self)
            self.reader = threading.Thread(target=self._read, daemon=True, name='claude-usage-terminal')
            self.reader.start()
            self._check(k.ResumeThread(self.process.hThread) != 0xffffffff)
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _check(value):
        if not value:
            raise OSError('Could not initialize isolated terminal')

    def kill(self):
        if self.process.hProcess:
            self.k.TerminateProcess(self.process.hProcess, 1)

    def _read(self):
        block, count = ctypes.create_string_buffer(8192), w.DWORD()
        while self.k.ReadFile(self.output, block, len(block), ctypes.byref(count), None) and count.value:
            with self.guard:
                if len(self.buffer) + count.value <= 262144:
                    self.buffer.extend(block.raw[:count.value])
                else:
                    self.overflow = True
            # Keep draining after the bound, so pseudoconsole cleanup cannot deadlock.

    def text(self):
        with self.guard:
            return self.buffer.decode('utf-8', errors='replace')

    def write(self, value):
        data, count = value.encode('utf-8'), w.DWORD()
        self._check(self.k.WriteFile(self.input, data, len(data), ctypes.byref(count), None))

    def close(self):
        if self.tree:
            self.tree.close()
            self.tree = None
        self.kill()
        if self.process.hProcess:
            self.k.WaitForSingleObject(self.process.hProcess, 2000)
        if self.console:
            self.k.ClosePseudoConsole(self.console)
            self.console = w.HANDLE()
        if self.reader:
            self.reader.join(2)
        for handle in self.handles:
            self.k.CloseHandle(handle)
        self.handles = []
        for name in ('hThread', 'hProcess'):
            handle = getattr(self.process, name)
            if handle:
                self.k.CloseHandle(handle)
                setattr(self.process, name, None)
        if self.attributes is not None:
            self.k.DeleteProcThreadAttributeList(self.attributes)
            self.attributes = None
