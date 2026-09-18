"""Explicit recovery of one frozen, isolated Windows profile; no window APIs.

The caller selects a specific launch generation. Open process handles bind every
termination to that lifetime, including descendants, rather than reusable PIDs.
This deliberately does not launch a GUI, touch credentials, or delete history.
"""
from contextlib import ExitStack, nullcontext
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import time

from .common import _assert_owned_path
from .store import atomic_json, identifier, now

# A WSL host can outlive the app that first started it and serve other projects.
# Never treat Windows/WSL infrastructure as an app-owned helper, even if Windows
# retains the originating client PID in its parent field.
_SHARED_HOSTS = frozenset({'wslhost.exe', 'wslservice.exe', 'vmmemwsl.exe', 'svchost.exe'})


def same_path(first, second):
    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(os.path.abspath(second))


def select_descendants(rows, root_pid):
    selected = {root_pid} if isinstance(root_pid, int) else set(root_pid)
    while True:
        added = {pid for pid, parent in rows.items() if parent in selected} - selected
        if not added:
            return selected
        selected.update(added)


class WindowsProcesses:
    """Kernel process operations only. No HWND enumeration or input automation."""
    def __init__(self):
        if os.name != 'nt':
            raise RuntimeError('Windows 프로필에서만 멈춤 복구를 사용할 수 있습니다.')
        self.k = ctypes.WinDLL('kernel32', use_last_error=True)
        self.k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.k.OpenProcess.restype = wintypes.HANDLE
        self.k.CloseHandle.argtypes = [wintypes.HANDLE]
        self.k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.k.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
        self.k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.k.WaitForSingleObject.restype = wintypes.DWORD
        self.k.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        self.k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE

    def snapshot(self):
        class Entry(ctypes.Structure):
            _fields_ = [('size', wintypes.DWORD), ('usage', wintypes.DWORD), ('pid', wintypes.DWORD),
                        ('heap', ctypes.c_size_t), ('module', wintypes.DWORD), ('threads', wintypes.DWORD),
                        ('parent', wintypes.DWORD), ('priority', wintypes.LONG), ('flags', wintypes.DWORD),
                        ('exe', wintypes.WCHAR * 260)]
        self.k.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
        self.k.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
        handle = self.k.CreateToolhelp32Snapshot(2, 0)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            row = Entry(size=ctypes.sizeof(Entry))
            if not self.k.Process32FirstW(handle, ctypes.byref(row)):
                raise ctypes.WinError(ctypes.get_last_error())
            result = {}
            while True:
                result[row.pid] = row.parent
                if not self.k.Process32NextW(handle, ctypes.byref(row)):
                    if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                        raise ctypes.WinError(ctypes.get_last_error())
                    return result
        finally:
            self.k.CloseHandle(handle)

    def open(self, pid):
        handle = self.k.OpenProcess(0x1000 | 0x100000 | 1, False, pid)
        if not handle:
            if ctypes.get_last_error() in (87, 1168):
                return None
            raise ctypes.WinError(ctypes.get_last_error())
        return WindowsProcess(self.k, handle, pid)


class WindowsProcess:
    def __init__(self, kernel, handle, pid):
        self.k, self.handle, self.pid = kernel, handle, pid
        self._termination_requested = False
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not self.k.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
                raise ctypes.WinError(ctypes.get_last_error())
            self.created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not self.k.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            self.executable = buffer.value
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.handle:
            self.k.CloseHandle(self.handle)
            self.handle = None

    def alive(self):
        result = self.k.WaitForSingleObject(self.handle, 0)
        if result not in (0, 258):
            raise ctypes.WinError(ctypes.get_last_error())
        return result == 258

    def terminate(self):
        if self._termination_requested or not self.alive():
            return
        if not self.k.TerminateProcess(self.handle, 1):
            error = ctypes.get_last_error()
            if self.alive():
                raise ctypes.WinError(error)
        self._termination_requested = True

    def arguments(self):
        # Read the selected process command line privately to prove --user-data-dir.
        # Neither this string nor the argument list is returned in a report.
        class UnicodeString(ctypes.Structure):
            _fields_ = [('length', wintypes.USHORT), ('maximum', wintypes.USHORT), ('buffer', ctypes.c_void_p)]
        query = ctypes.WinDLL('ntdll').NtQueryInformationProcess
        query.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
        query.restype = wintypes.LONG
        size = wintypes.ULONG()
        query(self.handle, 60, None, 0, ctypes.byref(size))
        if not ctypes.sizeof(UnicodeString) <= size.value <= 1024 * 1024:
            raise RuntimeError('실행 프로필의 전용 경로를 확인하지 못했습니다.')
        buffer = ctypes.create_string_buffer(size.value)
        if query(self.handle, 60, buffer, size, ctypes.byref(size)) < 0:
            raise RuntimeError('실행 프로필의 전용 경로를 확인하지 못했습니다.')
        value = UnicodeString.from_buffer(buffer)
        start, end = ctypes.addressof(buffer), ctypes.addressof(buffer) + len(buffer)
        if not value.buffer or value.length % 2 or not start <= value.buffer <= value.buffer + value.length <= end:
            raise RuntimeError('프로필 실행 정보의 형식이 올바르지 않습니다.')
        command = ctypes.wstring_at(value.buffer, value.length // 2)
        shell = ctypes.WinDLL('shell32')
        shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
        shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
        self.k.LocalFree.argtypes = [ctypes.c_void_p]
        count = ctypes.c_int()
        argv = shell.CommandLineToArgvW(command, ctypes.byref(count))
        if not argv:
            raise RuntimeError('프로필 실행 인수를 확인하지 못했습니다.')
        try:
            return [argv[i] for i in range(count.value)]
        finally:
            self.k.LocalFree(argv)


def stop_profile(store, instances, profile_id, *, expected_generation, interrupt_running_work=False,
                 processes=None, timeout=12, quiescence_check=None):
    if interrupt_running_work is not True and not callable(quiescence_check):
        raise ValueError('멈춘 프로필을 강제로 다시 시작하는 버튼에서 요청하세요.')
    profile_id = identifier(profile_id)
    admission = getattr(instances, 'launch_admission', None)
    # A maintenance caller already owns the launch barrier. Its callback proves
    # writer release and the held mutation gate again immediately before exit.
    with admission(profile_id) if admission and quiescence_check is None else nullcontext():
        profile = store.profile(profile_id)
        if profile.get('generation') != expected_generation or not expected_generation:
            raise ValueError('프로필이 이미 다시 시작되었습니다. 현재 프로필을 새로 확인하세요.')
        if profile.get('removed_at') or profile.get('view_only'):
            raise ValueError('복구할 계정 프로필을 선택하세요.')
        if quiescence_check is not None and quiescence_check() is not True:
            raise RuntimeError('작업 종료 확인이 변경되어 프로필을 종료하지 않았습니다.')
        home, ui = instances.paths(profile)
        _assert_owned_path(home, store.directory / 'profiles' / profile_id)
        _assert_owned_path(ui, store.directory / 'profiles' / profile_id)
        from . import rust_service
        if processes is None and rust_service.enabled():
            if quiescence_check is not None and quiescence_check() is not True:
                raise RuntimeError('종료 직전 작업 상태가 변경되어 프로필을 유지했습니다.')
            result = rust_service.request('process.stop', profile_id=profile_id, generation=expected_generation)
            instances.handles.pop(profile_id, None)
            instances.processes.pop(profile_id, None)
            return result
        processes = processes or WindowsProcesses()
        rows = processes.snapshot()
        main_pid = profile.get('process_id')
        if not main_pid or main_pid not in rows:
            return dict(state='already_stopped', profile_id=profile_id, generation=expected_generation)
        protected = {os.getpid()} | {p.get('process_id') for p in store.read()['profiles'] if p['id'] != profile_id}
        selected = select_descendants(rows, main_pid)
        if selected & protected:
            raise RuntimeError('현재 관리 서비스 또는 다른 계정이 포함되어 종료하지 않았습니다.')
        with ExitStack() as stack:
            held = {}
            preserved = set()

            def capture(pid, parent=None):
                process = processes.open(pid)
                if process is None:
                    return None
                stack.callback(process.close)
                if parent and process.created < parent.created:
                    raise RuntimeError('하위 프로세스 식별이 변경되어 복구를 중단했습니다.')
                held[pid] = process
                if Path(process.executable).name.lower() in _SHARED_HOSTS or (parent and parent.pid in preserved):
                    preserved.add(pid)
                return process

            main = capture(main_pid)
            if main is None:
                return dict(state='already_stopped', profile_id=profile_id, generation=expected_generation)
            if main.created != profile.get('process_created') or not same_path(main.executable, profile.get('executable_path', '')):
                raise RuntimeError('저장된 프로필과 실행 프로세스가 달라 종료하지 않았습니다.')
            if main_pid in preserved:
                raise RuntimeError('시스템 프로세스는 프로필 복구 대상으로 사용할 수 없습니다.')
            args = main.arguments()
            ui_args = [a.split('=', 1)[1] for a in args if a.startswith('--user-data-dir=')]
            if len(ui_args) != 1 or not same_path(ui_args[0], ui):
                raise RuntimeError('관리 앱 전용 Codex 프로필을 확인하지 못해 종료하지 않았습니다.')

            def capture_children(snapshot):
                pending = select_descendants(snapshot, set(held))
                if pending & protected or len(pending) > 512:
                    raise RuntimeError('종료할 프로세스 범위를 확인하지 못했습니다.')
                while True:
                    ready = [pid for pid in pending - set(held) if snapshot.get(pid) in held]
                    if not ready:
                        break
                    for pid in ready:
                        if capture(pid, held[snapshot[pid]]) is None:
                            pending.remove(pid)
                # A disappearing intermediate parent cannot prove its children.
                if pending - set(held):
                    raise RuntimeError('프로세스 목록이 바뀌었습니다. 복구 버튼으로 다시 확인하세요.')

            capture_children(rows)
            report_path = store.directory / 'instances' / profile_id / 'last-recovery.json'
            report = dict(state='stopping', profile_id=profile_id, generation=expected_generation,
                          main_pid=main_pid, started_at=now(), user_data_directory_verified=True)
            atomic_json(report_path, report)
            deadline = time.monotonic() + timeout
            try:
                # Root first prevents Electron from respawning its renderer. Retain
                # handles while rescanning so old parent PIDs cannot be reused.
                if quiescence_check is not None and quiescence_check() is not True:
                    raise RuntimeError('종료 직전 작업 상태가 변경되어 프로필을 유지했습니다.')
                main.terminate()
                while True:
                    for pid, process in held.items():
                        if pid not in preserved: process.terminate()
                    before = len(held)
                    capture_children(processes.snapshot())
                    if before == len(held) and all(not p.alive() for pid, p in held.items() if pid not in preserved):
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError('일부 프로세스의 종료를 확인하지 못했습니다. 새 창은 열지 않았습니다.')
                    time.sleep(0.05)
                instances.handles.pop(profile_id, None)
                instances.processes.pop(profile_id, None)
                report.update(state='stopped', all_selected_processes_exited=True)
            except Exception:
                report.update(state='attention', all_selected_processes_exited=False)
                raise
            finally:
                report.update(finished_at=now(), processes=[dict(pid=p.pid, created=p.created,
                              name=Path(p.executable).name) for pid, p in held.items() if pid not in preserved],
                              preserved_infrastructure=[dict(pid=p.pid, name=Path(p.executable).name)
                                                        for pid, p in held.items() if pid in preserved])
                atomic_json(report_path, report)
            return report
