"""Read-only Windows process lifetime checks; missing evidence stays unknown."""
import ctypes
from ctypes import wintypes
import os


def process_liveness(expected):
    """Prove exit or PID reuse without treating access failure as termination.

    Old records may lack a creation time. A signaled handle or absent PID still
    proves exit, but a running PID without its recorded birth remains unknown.
    """
    if os.name != 'nt' or type(expected.get('pid')) is not int or not 0 < expected['pid'] <= 0xffffffff:
        return 'unknown'
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
    handle = kernel.OpenProcess(0x101000, False, expected['pid'])
    if not handle:
        return 'exited' if ctypes.get_last_error() == 87 else 'unknown'
    try:
        state = kernel.WaitForSingleObject(handle, 0)
        if state == 0:
            return 'exited'
        if state != 258 or type(expected.get('created')) is not int or expected['created'] <= 0:
            return 'unknown'
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *[ctypes.byref(item) for item in times]):
            return 'unknown'
        created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        return 'alive' if created == expected['created'] else 'reused'
    finally:
        kernel.CloseHandle(handle)
