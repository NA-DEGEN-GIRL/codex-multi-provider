"""Where the backend spends its first minutes, without an external profiler.

Samples every thread's Python stack for a bounded window after start and keeps
only counts of code locations (file stem, function, line) -- never arguments,
paths from user data or record contents. Waiting threads are skipped so the
summary shows work that competes for the GIL and delays requests.
"""
import json
import os
from pathlib import Path
import sys
import threading
import time

from .store import atomic_json

# Leaf frames that mean the thread is blocked, not working.
_WAITING = frozenset({
    'wait', 'sleep', 'select', 'readline', 'read', 'readinto', 'recv', 'recv_into', 'accept', 'acquire',
    '_wait_for_tstate_lock', 'get', 'join', 'poll', 'communicate', '_communicate', 'wait_for', 'result',
    '_worker', 'serve_forever', 'handle_request', '_readerthread', 'connect', 'peek', 'input',
})
_OWN = ('manager_core', 'control_center', 'remote_helpers', 'desktop_launch')


def _location(frame):
    code = frame.f_code
    return f'{Path(code.co_filename).stem}:{code.co_name}:{frame.f_lineno}'


def _stack(frame, depth=14):
    frames = []
    while frame is not None and len(frames) < depth:
        frames.append(frame)
        frame = frame.f_back
    return frames


def _own(frame):
    return any(part in frame.f_code.co_filename for part in _OWN)


def sample(frames, names, skip, busy, leaves, stacks, threads, weights=None):
    """Fold one snapshot of sys._current_frames() into the counters.

    With `weights` (thread ident -> CPU ms used since the last snapshot) only
    threads that actually ran count, weighted by that CPU time; C-level waits
    such as pipe reads then never look busy. Without it, leaf names decide.
    """
    for ident, frame in frames.items():
        if ident == skip:
            continue
        chain = _stack(frame)
        if weights is None:
            if not chain or chain[0].f_code.co_name in _WAITING:
                continue
            weight = 1
        else:
            weight = weights.get(ident, 0)
            if weight <= 0 or not chain:
                continue
        busy[0] += weight
        name = names.get(ident, 'thread')
        threads[name] = threads.get(name, 0) + weight
        # Attribute to the innermost manager frame; library leaves alone say little.
        own = next((f for f in chain if _own(f)), chain[0])
        leaf = _location(own)
        leaves[leaf] = leaves.get(leaf, 0) + weight
        key = ' <- '.join(_location(f) for f in chain if _own(f))[:600] or _location(chain[0])
        stacks[key] = stacks.get(key, 0) + weight


class _ThreadClock:
    """Per-thread CPU time on Windows (GetThreadTimes); None elsewhere."""
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self._kernel = kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenThread.restype = wintypes.HANDLE
        kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.GetThreadTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self._ctypes, self._wintypes, self._handles, self._last = ctypes, wintypes, {}, {}

    def deltas(self, threads):
        """ident -> CPU ms used since the previous call (first call: 0)."""
        ctypes, FILETIME, result = self._ctypes, self._wintypes.FILETIME, {}
        for thread in threads:
            native = getattr(thread, 'native_id', None)
            if not isinstance(native, int) or native <= 0:
                continue  # A dummy or finishing thread has no queryable id.
            handle = self._handles.get(native)
            if handle is None:
                handle = self._kernel.OpenThread(0x0800, False, native)  # THREAD_QUERY_LIMITED_INFORMATION
                if not handle:
                    continue
                self._handles[native] = handle
            times = [FILETIME() for _ in range(4)]
            if not self._kernel.GetThreadTimes(handle, *(ctypes.byref(t) for t in times)):
                continue
            used = sum((t.dwHighDateTime << 32 | t.dwLowDateTime) for t in times[2:]) / 10_000  # kernel+user, ms
            previous = self._last.get(native)
            self._last[native] = used
            if previous is not None:
                result[thread.ident] = used - previous
        return result

    def close(self):
        for handle in self._handles.values():
            self._kernel.CloseHandle(handle)
        self._handles.clear()


def _run(path, duration, interval):
    # Build the clock first: under a busy GIL its ctypes setup alone can take
    # longer than a short window, which would end sampling before it starts.
    clock = _ThreadClock() if os.name == 'nt' else None
    started, cpu = time.time(), time.process_time()
    busy, leaves, stacks, threads, snapshots = [0], {}, {}, {}, 0
    deadline = time.monotonic() + duration
    me = threading.get_ident()
    try:
        while time.monotonic() < deadline:
            alive = threading.enumerate()
            names = {thread.ident: thread.name.split('(')[0] for thread in alive}
            weights = clock.deltas(alive) if clock else None
            sample(sys._current_frames(), names, me, busy, leaves, stacks, threads, weights)
            snapshots += 1
            time.sleep(interval)
    finally:
        if clock:
            clock.close()
    unit = 'cpu_ms' if clock else 'samples'
    top = lambda counts, n: [dict(where=k, samples=round(v)) for k, v in sorted(counts.items(), key=lambda kv: -kv[1])[:n]]
    atomic_json(path, dict(version=1, unit=unit, started_at=started, duration_s=duration,
        interval_ms=round(interval * 1000), snapshots=snapshots, busy_samples=round(busy[0]),
        process_cpu_s=round(time.process_time() - cpu, 2),
        threads={k: round(v) for k, v in sorted(threads.items(), key=lambda kv: -kv[1])},
        top_functions=top(leaves, 40), top_stacks=top(stacks, 25)))


def start(root, *, duration=180, interval=.02):
    """Profile the first `duration` seconds in a daemon thread; opt out with
    CODEX_MANAGER_STARTUP_PROFILE=0. Returns the thread, or None when disabled."""
    if os.environ.get('CODEX_MANAGER_STARTUP_PROFILE', '1') == '0':
        return None
    path = Path(root) / 'work/control-center/logs/backend-startup-profile.json'
    def run():
        try:
            _run(path, duration, interval)
        except Exception as error:  # Diagnostics never disturb the backend, but say why they stopped.
            try:
                atomic_json(path, dict(version=1, error=type(error).__name__,
                                       where=_location(error.__traceback__.tb_frame) if error.__traceback__ else None))
            except OSError:
                pass
    worker = threading.Thread(target=run, daemon=True, name='codex-startup-profile')
    worker.start()
    return worker
