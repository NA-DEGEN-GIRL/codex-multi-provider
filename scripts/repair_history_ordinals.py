"""Repair duplicate envelope ordinals in one paginated rollout. Dry-run by default.

Symptom: the desktop cannot open a task and thread/turns/list fails with
``thread history projection for <id> expected ordinal N+1, got N``. The
projector (thread-store thread_history_materialization.rs) requires the
top-level ``ordinal`` of each JSONL line to increase by exactly one.

Likely cause: a writer reused an ordinal. A writer that opens a rollout for
append derives its next ordinal from a reverse scan of the file. Older runtimes
decoded the final record as a typed line and skipped it when decoding failed,
for example a token_count whose float rate limits the flattened decode rejects,
or an item from a newer runtime, so the ordinal of that record was used again.
Current runtimes read only the top-level ordinal on every append path
(rollout/src/ordinal.rs final_durable_ordinal) and refuse to append after a
complete final record without one, so this tool repairs rollouts written before
that fix.

Repair: renumber the lines after the first duplicate so that line k carries
``base + k``. Only the ordinal digits change, and every payload byte is kept.
Nothing is dropped or synthesized. SQLite rows derived from the renumbered suffix
are cleared so that the runtime rebuilds them from the rollout. The runtime
already rebuilds a thread without a checkpoint on its next open. Rows derived from
the untouched prefix are kept unless --rebuild-index is given.

  python scripts/repair_history_ordinals.py --thread <uuid>            # inspect only
  python scripts/repair_history_ordinals.py --thread <uuid> --apply    # repair
  python scripts/repair_history_ordinals.py --scan                     # whole store, read-only

--apply refuses while any process holds the store's databases or the rollout.
Close every Codex window first. --allow-live relaxes only the database check.
A rollout that is open in any process is never rewritten, and a thread whose
writer, projection or append lock is held is never repaired. Before any change,
the tool backs up every file it modifies with a UTC timestamp: the full rollout
and, when rows are cleared, the full thread_history_1.sqlite. It may create empty
per-thread lock files, which the runtime also creates. No model calls are made,
and no conversation text is printed.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
from manager_core.history_recovery import ORDINAL, canonical_path  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TABLES = ('thread_items', 'thread_turns', 'thread_realtime_items', 'thread_history_projection_state')
ZSTD_MAGIC = b'\x28\xb5\x2f\xfd'
ROLLOUT_ID = re.compile(r'([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl$')
MAX_REPORTED_ANOMALIES = 50


class Refused(Exception):
    """The repair cannot run safely right now, or this damage is outside its scope."""


def _ro(path):
    return sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True, timeout=10)


def _line_kind(value):
    payload = value.get('payload') if isinstance(value, dict) else None
    return dict(type=value.get('type') if isinstance(value, dict) else None,
                payload_type=payload.get('type') if isinstance(payload, dict) else None,
                timestamp=value.get('timestamp') if isinstance(value, dict) else None)


# --------------------------------------------------------------------------- rollout

def _renumber(raw, ordinal, desired):
    match = ORDINAL.match(raw)
    if not match or int(match[2]) != ordinal:
        raise Refused('A line to renumber does not start with {"timestamp":...,"ordinal":N}; '
                      'the ordinal cannot be changed without rewriting other bytes.')
    repaired = raw[:match.start(2)] + str(desired).encode() + raw[match.end(2):]
    if json.loads(repaired).get('ordinal') != desired:
        raise Refused('A line carries more than one ordinal field; no bytes were changed.')
    return repaired


def scan_rollout(stream, thread_id, *, backup=None, candidate=None):
    """Content-free pass over a rollout; optionally writes the backup and repaired copy.

    Returns the anomaly plan. Raises Refused for damage this tool must not guess about:
    torn or unparsable lines, missing ordinals, gaps, or a mismatched session.
    """
    digest = hashlib.sha256(); repaired_digest = hashlib.sha256()
    offset = lines = blank = changed = 0
    previous = desired_next = None
    anomalies = []; counts = dict(duplicate=0, rewind=0)
    first_change = None; last_kind = None
    for raw in iter(stream.readline, b''):
        digest.update(raw)
        if backup is not None:
            backup.write(raw)
        start = offset; offset += len(raw); lines += 1
        if not raw.endswith(b'\n'):
            raise Refused(f'The final line is incomplete ({len(raw)} bytes at byte {start}); '
                          'a writer may still be active or crashed. This tool does not trim rollouts.')
        output = raw
        if raw.strip():
            try:
                value = json.loads(raw)
            except (ValueError, UnicodeError):
                raise Refused(f'Complete line {lines - 1} at byte {start} is not valid JSON; manual recovery is required.') from None
            if not isinstance(value, dict):
                raise Refused(f'Line {lines - 1} is not a JSON object.')
            ordinal = value.get('ordinal')
            if desired_next is None:
                payload = value.get('payload') if isinstance(value.get('payload'), dict) else {}
                if value.get('type') != 'session_meta' or payload.get('id') != thread_id:
                    raise Refused('The rollout does not start with this task\'s session metadata.')
                if payload.get('history_mode') != 'paginated':
                    raise Refused('Only paginated rollouts carry envelope ordinals.')
                base = payload.get('history_base')
                desired_next = base.get('end_ordinal_exclusive') if isinstance(base, dict) else 0
                if type(desired_next) is not int or desired_next < 0:
                    raise Refused('The session history base has no usable end ordinal.')
            if type(ordinal) is not int or ordinal < 0:
                raise Refused(f'Line {lines - 1} at byte {start} has no envelope ordinal.')
            kind = _line_kind(value)
            if previous is None and ordinal != desired_next:
                raise Refused(f'The first ordinal is {ordinal}; the runtime expects {desired_next}.')
            if previous is not None and ordinal > previous + 1:
                raise Refused(f'Ordinal gap at line {lines - 1} ({previous} -> {ordinal}); '
                              'missing records are outside this repair.')
            if previous is not None and ordinal <= previous:
                name = 'duplicate' if ordinal == previous else 'rewind'
                counts[name] += 1
                if len(anomalies) < MAX_REPORTED_ANOMALIES:
                    anomalies.append(dict(kind=name, line=lines - 1, byte_offset=start,
                                          previous_ordinal=previous, ordinal=ordinal,
                                          line_kind=kind, previous_line_kind=last_kind))
            desired = desired_next
            if desired != ordinal:
                output = _renumber(raw, ordinal, desired)
                changed += 1
                if first_change is None:
                    first_change = dict(line=lines - 1, byte_offset=start, ordinal=ordinal,
                                        repaired_ordinal=desired)
            previous = ordinal; desired_next = desired + 1; last_kind = kind
        else:
            blank += 1
        if candidate is not None:
            candidate.write(output)
        repaired_digest.update(output)
    if desired_next is None:
        raise Refused('The rollout has no records.')
    return dict(lines=lines, blank_lines=blank, bytes=offset, sha256=digest.hexdigest(),
                last_ordinal=previous, anomaly_counts=counts, anomalies=anomalies,
                changed_lines=changed, first_change=first_change,
                repaired_last_ordinal=desired_next - 1, repaired_sha256=repaired_digest.hexdigest())


# --------------------------------------------------------------------------- store

def resolve_thread(home, thread_id):
    home = canonical_path(home); thread_id = str(UUID(thread_id))
    with closing(_ro(home/'state_5.sqlite')) as db:
        row = db.execute('SELECT rollout_path FROM threads WHERE id=?', (thread_id,)).fetchone()
    if not row:
        raise Refused('The task was not found in this store\'s state_5.sqlite.')
    rollout = canonical_path(row[0])
    if not any(rollout.is_relative_to(home/part) for part in ('sessions', 'archived_sessions')):
        raise Refused('The task rollout is outside this store; no records were changed.')
    match = ROLLOUT_ID.search(rollout.name)
    if not match:
        raise Refused('The rollout file name has no immutable rollout ID.')
    if not rollout.is_file():
        raise Refused('The rollout is compressed or missing. Only plain JSONL rollouts are rewritten.')
    with rollout.open('rb') as stream:
        if stream.read(4) == ZSTD_MAGIC:
            raise Refused('The rollout holds compressed bytes under a .jsonl name. Only plain JSONL is rewritten.')
    return dict(home=home, thread_id=thread_id, rollout=rollout, rollout_id=str(UUID(match[1])))


def inspect_index(home, rollout_id, first_change, *, rebuild=False):
    """Projection checkpoint and derived-row bounds; decides whether rows must be cleared."""
    path = Path(home)/'thread_history_1.sqlite'
    if not path.exists():
        return dict(path=str(path), exists=False, action='none')
    with closing(_ro(path)) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not set(TABLES) <= tables:
            raise Refused('thread_history_1.sqlite has an unexpected schema.')
        checkpoint = db.execute('SELECT next_rollout_byte_offset,next_rollout_ordinal FROM '
                                'thread_history_projection_state WHERE thread_id=?', (rollout_id,)).fetchone()
        items = db.execute('SELECT count(*),max(rollout_ordinal),max(updated_at_ordinal) FROM thread_items '
                           'WHERE thread_id=?', (rollout_id,)).fetchone()
        turns = db.execute('SELECT count(*),max(rollout_ordinal),max(rollout_end_ordinal) FROM thread_turns '
                           'WHERE thread_id=?', (rollout_id,)).fetchone()
        realtime = db.execute('SELECT count(*),max(rollout_ordinal) FROM thread_realtime_items '
                              'WHERE thread_id=?', (rollout_id,)).fetchone()
    rows = dict(thread_items=items[0], thread_turns=turns[0], thread_realtime_items=realtime[0],
                thread_history_projection_state=int(checkpoint is not None))
    highest = max([v for v in (items[1], items[2], turns[1], turns[2], realtime[1]) if v is not None], default=None)
    stale = False
    if first_change:
        boundary = first_change['repaired_ordinal']
        stale = (highest is not None and highest >= boundary) or (checkpoint is not None and (
            checkpoint[0] > first_change['byte_offset'] or checkpoint[1] > boundary))
    action = 'clear' if first_change and (stale or rebuild) else 'none'
    return dict(path=str(path), exists=True, rows=rows, highest_derived_ordinal=highest,
                checkpoint=dict(next_byte_offset=checkpoint[0], next_ordinal=checkpoint[1]) if checkpoint else None,
                orphan_rows=checkpoint is None and any(rows[t] for t in TABLES[:3]),
                action=action, stale_rows=stale)


_NODE_HEADS = r'''
const fs=require('fs'),zlib=require('zlib');
const paths=JSON.parse(fs.readFileSync(0,'utf8'));const out={};
(async()=>{for(const p of paths){out[p]=await new Promise(res=>{let buf=Buffer.alloc(0),done=false;
const rs=fs.createReadStream(p),z=zlib.createZstdDecompress();rs.pipe(z);
const finish=v=>{if(!done){done=true;res(v);rs.destroy();z.destroy();}};
z.on('data',b=>{buf=Buffer.concat([buf,b]);const i=buf.indexOf(10);if(i>=0)finish(pick(buf.subarray(0,i)));});
z.on('end',()=>finish(pick(buf)));z.on('error',()=>finish(null));rs.on('error',()=>finish(null));});}
process.stdout.write(JSON.stringify(out));})();
function pick(b){try{const p=JSON.parse(b.toString('utf8')).payload||{};return {id:p.id,history_mode:p.history_mode,
history_base:p.history_base??null,forked_from_id:p.forked_from_id??null,
forked_from_ordinal_exclusive:p.forked_from_ordinal_exclusive??null};}catch(e){return null;}}
'''


def _pick(line):
    try:
        value = json.loads(line)
    except (ValueError, UnicodeError):
        return None
    payload = value.get('payload') if isinstance(value, dict) else None
    if not isinstance(payload, dict):
        return None
    return {k: payload.get(k) for k in ('id', 'history_mode', 'history_base', 'forked_from_id',
                                        'forked_from_ordinal_exclusive')}


def _zstd_first_line(path):
    """First line of a zstd file with a Python decoder; ImportError when none is installed."""
    try:
        from compression import zstd  # Python 3.14+
    except ImportError:
        import zstandard  # raises ImportError when absent
        with path.open('rb') as raw, zstandard.ZstdDecompressor().stream_reader(raw) as reader:
            return io.BufferedReader(reader).readline()
    with zstd.open(path, 'rb') as stream:
        return stream.readline()


def _compressed_heads(paths):
    """Session metadata fields of zstd rollouts, via a Python zstd module or Node 22+."""
    if not paths:
        return {}
    try:
        heads = {}
        for path in paths:
            try:
                heads[str(path)] = _pick(_zstd_first_line(path))
            except ImportError:
                raise
            except Exception:  # noqa: BLE001 - an unreadable head is reported, not guessed
                heads[str(path)] = None
        return heads
    except ImportError:
        pass
    node = shutil.which('node')
    if not node:
        raise Refused('Compressed rollouts must be checked for fork references, but no zstd decoder '
                      '(Python 3.14, the zstandard module or Node 22+) is available.')
    result = subprocess.run([node, '-e', _NODE_HEADS], input=json.dumps([str(p) for p in paths]).encode(),
                            capture_output=True, timeout=900,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise Refused('Compressed rollout references could not be read with Node.')
    return json.loads(result.stdout)


def session_heads(home):
    """{thread_id: selected session_meta fields or None} for every task in the store."""
    home = canonical_path(home); heads = {}; compressed = {}
    with closing(_ro(home/'state_5.sqlite')) as db:
        records = list(db.execute('SELECT id, rollout_path FROM threads'))
    for tid, stored in records:
        path = canonical_path(stored)
        target = path if path.is_file() else path.with_name(path.name + '.zst')
        if not target.is_file():
            continue
        with target.open('rb') as stream:
            if stream.read(4) == ZSTD_MAGIC:
                compressed[str(target)] = tid
                continue
            stream.seek(0)
            heads[tid] = _pick(stream.readline())
    for path, head in _compressed_heads([Path(p) for p in compressed]).items():
        heads[compressed[path]] = head
    return heads


def positional_references(heads, thread_id, rollout_id, first_change):
    """Other tasks whose frozen history points into the renumbered suffix."""
    blocking = []; unreadable = 0
    ids = {thread_id, rollout_id}
    for tid, head in heads.items():
        if tid in ids:
            continue
        if head is None:
            unreadable += 1
            continue
        base = head.get('history_base') if isinstance(head.get('history_base'), dict) else None
        if base and base.get('thread_id') in ids:
            end = base.get('end_ordinal_exclusive'); end_byte = base.get('end_byte_offset')
            if not isinstance(end, int) or not isinstance(end_byte, int) or \
                    end > first_change['repaired_ordinal'] or end_byte > first_change['byte_offset']:
                blocking.append(dict(thread=tid, kind='history_base', end_ordinal_exclusive=end))
        # A fork cutoff above the duplicated ordinal would include a different set of records.
        boundary = head.get('forked_from_ordinal_exclusive')
        if head.get('forked_from_id') in ids and isinstance(boundary, int) and boundary > first_change['ordinal']:
            blocking.append(dict(thread=tid, kind='fork_boundary', forked_from_ordinal_exclusive=boundary))
    return dict(checked=len(heads), unreadable_heads=unreadable, blocking=blocking)


# --------------------------------------------------------------------------- liveness and locks

if os.name == 'nt':
    import ctypes
    from ctypes import wintypes
    import msvcrt

    class _Overlapped(ctypes.Structure):
        _fields_ = [('Internal', ctypes.c_void_p), ('InternalHigh', ctypes.c_void_p),
                    ('Offset', wintypes.DWORD), ('OffsetHigh', wintypes.DWORD), ('hEvent', wintypes.HANDLE)]

    _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _kernel32.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.DWORD, ctypes.POINTER(_Overlapped)]
    _kernel32.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                       ctypes.POINTER(_Overlapped)]
    _kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                      wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                     ctypes.POINTER(wintypes.DWORD)]

    class _IoStatusBlock(ctypes.Structure):
        _fields_ = [('Status', ctypes.c_void_p), ('Information', ctypes.c_void_p)]

    _ntdll = ctypes.WinDLL('ntdll')
    _ntdll.NtQueryInformationFile.argtypes = [wintypes.HANDLE, ctypes.POINTER(_IoStatusBlock), ctypes.c_void_p,
                                              wintypes.ULONG, ctypes.c_int]
    _ntdll.NtQueryInformationFile.restype = ctypes.c_long


def live_holders(paths):
    """[(pid, name)] of other processes that currently hold any of these files open."""
    paths = [str(Path(p)) for p in paths if Path(p).exists()]
    if not paths:
        return []
    if os.name == 'nt':
        return _windows_file_users(paths)
    proc = Path('/proc')
    if not proc.is_dir():
        raise Refused('Open-file inspection is unavailable on this platform; pass --allow-live only if Codex is closed.')
    targets = {Path(p).resolve() for p in paths}; found = []
    for entry in proc.iterdir():
        if not entry.name.isdigit() or entry.name == str(os.getpid()):
            continue
        try:
            for fd in (entry/'fd').iterdir():
                try:
                    if fd.resolve() in targets:
                        found.append((int(entry.name), (entry/'comm').read_text().strip()))
                        break
                except OSError:
                    continue
        except OSError:
            continue
    return found


def _windows_file_users(paths):
    """PIDs using each file, via FileProcessIdsUsingFileInformation (one kernel query per file)."""
    found = {}
    for path in paths:
        handle = _kernel32.CreateFileW(path, 0x80, 0x7, None, 3, 0x02000000, None)  # read attributes, share all
        if handle == wintypes.HANDLE(-1).value:
            raise Refused(f'Cannot inspect which processes use {path}.')
        try:
            size = 4096
            while True:
                buffer = ctypes.create_string_buffer(size); status_block = _IoStatusBlock()
                status = _ntdll.NtQueryInformationFile(handle, ctypes.byref(status_block), buffer, size, 47)
                if status == -1073741820 and size < 1 << 22:  # STATUS_INFO_LENGTH_MISMATCH
                    size *= 4
                    continue
                if status < 0:
                    raise Refused(f'Cannot inspect which processes use {path}.')
                count = ctypes.c_ulong.from_buffer(buffer).value
                width = ctypes.sizeof(ctypes.c_size_t)
                for n in range(count):
                    pid = ctypes.c_size_t.from_buffer(buffer, width * (n + 1)).value
                    if pid != os.getpid():
                        found.setdefault(pid, _process_name(pid))
                break
        finally:
            _kernel32.CloseHandle(handle)
    return sorted(found.items())


def _process_name(pid):
    process = _kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not process:
        return '?'
    try:
        name = ctypes.create_unicode_buffer(1024); length = wintypes.DWORD(1024)
        return Path(name.value).name if _kernel32.QueryFullProcessImageNameW(process, 0, name, ctypes.byref(length)) else '?'
    finally:
        _kernel32.CloseHandle(process)


class _FileLock:
    """Exclusive whole-file lock, compatible with Rust std File::lock/try_lock on each platform."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = open(self.path, 'a+b')

    def try_lock(self):
        if os.name == 'nt':
            # LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY over the whole range, like Rust std.
            return bool(_kernel32.LockFileEx(msvcrt.get_osfhandle(self.file.fileno()), 0x3, 0,
                                             0xFFFFFFFF, 0xFFFFFFFF, ctypes.byref(_Overlapped())))
        import fcntl
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def close(self):
        if os.name == 'nt':
            _kernel32.UnlockFileEx(msvcrt.get_osfhandle(self.file.fileno()), 0, 0xFFFFFFFF, 0xFFFFFFFF,
                                   ctypes.byref(_Overlapped()))
        self.file.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _try_lock(path, *, attempts=1, delay=0.2):
    """Return a held _FileLock, or raise BlockingIOError when another handle holds it."""
    lock = _FileLock(path)
    for attempt in range(attempts):
        if lock.try_lock():
            return lock
        if attempt + 1 < attempts:
            time.sleep(delay)
    lock.file.close()
    raise BlockingIOError(f'{path} is locked')


def acquire_thread_locks(stack, home, thread_id, rollout_id, rollout):
    """Hold every lock a runtime takes before writing or projecting this task."""
    home = Path(home); held = []
    writer_root = home/'thread-writer-locks'
    directories = [writer_root] + sorted(p for p in writer_root.glob('*') if p.is_dir() and _is_uuid(p.name))
    for directory in directories:
        try:
            coordination = _try_lock(directory/'.coordination.lock', attempts=50)
        except BlockingIOError:
            raise Refused('A Codex runtime keeps the writer coordination lock busy; retry shortly.') from None
        try:
            for name in {thread_id, rollout_id}:
                try:
                    stack.enter_context(_try_lock(directory/(name + '.lock')))
                except BlockingIOError:
                    raise Refused('This task is open in a Codex runtime (writer lock held). Close it first.') from None
                held.append(str(directory/(name + '.lock')))
        finally:
            coordination.close()
    for path in [*(home/'thread-projection-locks'/(name + '.lock') for name in {thread_id, rollout_id}),
                 rollout.with_suffix('.manager-append.lock')]:
        try:
            stack.enter_context(_try_lock(path))
        except BlockingIOError:
            raise Refused('A runtime is projecting or appending this task right now. Retry after it stops.') from None
        held.append(str(path))
    return held


def _is_uuid(text):
    try:
        UUID(text); return True
    except ValueError:
        return False


@contextmanager
def exclusive_rollout(path):
    """Windows: deny every other open while held. POSIX: refuse if any process holds it."""
    if os.name == 'nt':
        from manager_core.history_recovery import exclusive_rollout as windows_exclusive
        manager = windows_exclusive(path)
        try:
            stream = manager.__enter__()
        except ValueError:
            raise Refused('The rollout is open in another process; it was not changed.') from None
        with ExitStack() as stack:
            stack.push(manager)
            yield stream
        return
    if live_holders([path]):
        raise Refused('The rollout is open in another process; it was not changed.')
    with open(path, 'r+b') as stream:
        yield stream


# --------------------------------------------------------------------------- inspection / repair

def inspect(home, thread_id, *, rebuild_index=False, check_references=True):
    target = resolve_thread(home, thread_id)
    with target['rollout'].open('rb') as stream:
        plan = scan_rollout(stream, target['thread_id'])
    report = dict(thread_id=target['thread_id'], rollout_id=target['rollout_id'],
                  rollout=str(target['rollout']), home=str(target['home']), rollout_scan=plan)
    first = plan['first_change']
    report['index'] = inspect_index(target['home'], target['rollout_id'], first, rebuild=rebuild_index)
    if not first:
        report['state'] = 'clean'
        return report, target
    report['references'] = positional_references(session_heads(target['home']), target['thread_id'],
                                                 target['rollout_id'], first) if check_references else None
    report['live_holders'] = [dict(pid=pid, name=name) for pid, name in live_holders(store_files(target))]
    blocking = (report['references'] or {}).get('blocking')
    report['state'] = 'blocked_by_references' if blocking else 'repair_available'
    return report, target


def store_files(target):
    home = Path(target['home'])
    return [target['rollout'], home/'thread_history_1.sqlite', home/'state_5.sqlite']


def _journal(path, report):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as file:
        json.dump(report, file, indent=2, ensure_ascii=False); file.write('\n'); file.flush(); os.fsync(file.fileno())
    os.replace(temporary, path)


def _backup_index(index_path, rollout_id, output):
    """Full online snapshot of the history DB plus this task's rows, before any row is deleted."""
    full = output/'thread_history_1.sqlite'
    with closing(_ro(index_path)) as source, closing(sqlite3.connect(full)) as copy:
        source.backup(copy)  # One step: a consistent snapshot even while WAL writers continue.
    rows = output/'task-rows.sqlite'
    with closing(_ro(index_path)) as source, closing(sqlite3.connect(rows)) as saved:
        for table in TABLES:
            (definition,) = source.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            saved.execute(definition)
            cursor = source.execute(f'SELECT * FROM {table} WHERE thread_id=?', (rollout_id,))
            saved.executemany(f'INSERT INTO {table} VALUES ({",".join("?" for _ in cursor.description)})', cursor)
        saved.commit()
    return dict(full=str(full), task_rows=str(rows))


def repair(home, thread_id, *, apply=False, allow_live=False, rebuild_index=False, backup_root=None):
    report, target = inspect(home, thread_id, rebuild_index=rebuild_index)
    if not apply or report['state'] != 'repair_available':
        report['applied'] = False
        return report
    if report['live_holders'] and not allow_live:
        raise Refused('Codex processes hold this store: ' + ', '.join(
            f"{h['name']} ({h['pid']})" for h in report['live_holders']) +
            '. Close every Codex window, or pass --allow-live to repair while other tasks stay open.')
    plan = report['rollout_scan']; first = plan['first_change']; index = report['index']
    rollout = target['rollout']
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    output = Path(backup_root or ROOT/'work/history-recovery')/f'ordinals-{stamp}-{target["rollout_id"][:8]}-{uuid4().hex[:6]}'
    with ExitStack() as stack:
        report['locks'] = acquire_thread_locks(stack, target['home'], target['thread_id'], target['rollout_id'], rollout)
        after_locks = inspect_index(target['home'], target['rollout_id'], first, rebuild=rebuild_index)
        if after_locks['rows'] != index['rows'] or after_locks['checkpoint'] != index['checkpoint']:
            raise Refused('The task history index changed during inspection; retry.')
        output.mkdir(parents=True)
        journal = output/'repair.json'
        backups = dict(rollout=str(output/'original.jsonl'))
        candidate_path = output/'repaired.jsonl'
        try:
            if index['action'] == 'clear':
                backups['index'] = _backup_index(index['path'], target['rollout_id'], output)
            stream = stack.enter_context(exclusive_rollout(rollout))
            with (output/'original.jsonl').open('xb') as backup, candidate_path.open('xb') as candidate:
                stream.seek(0)
                prepared = scan_rollout(stream, target['thread_id'], backup=backup, candidate=candidate)
                for file in (backup, candidate):
                    file.flush(); os.fsync(file.fileno())
            if prepared['sha256'] != plan['sha256'] or prepared['first_change'] != first:
                raise Refused('The rollout changed after inspection; nothing was rewritten. Retry.')
            with (output/'original.jsonl').open('rb') as check:
                if hashlib.file_digest(check, 'sha256').hexdigest() != plan['sha256']:
                    raise Refused('The rollout backup does not match the inspected file.')
        except BaseException:
            shutil.rmtree(output, ignore_errors=True)  # Nothing was modified yet.
            raise
        report.update(state='prepared', backup_directory=str(output), backups=backups)
        _journal(journal, report)
        try:
            # Bytes before the first renumbered line are identical; rewrite only the suffix.
            stream.seek(first['byte_offset'])
            with candidate_path.open('rb') as source:
                source.seek(first['byte_offset'])
                shutil.copyfileobj(source, stream, 1024 * 1024)
            stream.truncate(); stream.flush(); os.fsync(stream.fileno())
            stream.seek(0)
            if hashlib.file_digest(stream, 'sha256').hexdigest() != prepared['repaired_sha256']:
                raise OSError('The rewritten rollout does not match the repaired copy.')
            if index['action'] == 'clear':
                with closing(sqlite3.connect(Path(index['path']).as_uri() + '?mode=rw', uri=True, timeout=15)) as db:
                    db.execute('BEGIN IMMEDIATE')
                    removed = {t: db.execute(f'DELETE FROM {t} WHERE thread_id=?', (target['rollout_id'],)).rowcount
                               for t in TABLES}
                    db.commit()
                report['cleared_rows'] = removed
        except BaseException:
            with (output/'original.jsonl').open('rb') as original:
                stream.seek(first['byte_offset']); original.seek(first['byte_offset'])
                shutil.copyfileobj(original, stream, 1024 * 1024)
            stream.truncate(); stream.flush(); os.fsync(stream.fileno())
            report['state'] = 'failed_original_restored'
            _journal(journal, report)
            raise
        stream.seek(0)
        report['verification'] = {k: v for k, v in scan_rollout(stream, target['thread_id']).items()
                                  if k in ('lines', 'bytes', 'sha256', 'last_ordinal', 'anomaly_counts', 'changed_lines')}
        candidate_path.unlink()
        report.update(state='repaired', applied=True,
                      next_open='The runtime rebuilds the projection from the rollout when the task is next opened.')
        _journal(journal, report)
    return report


# --------------------------------------------------------------------------- store-wide scan

def _scan_ordinals(stream, start):
    lines = 0; previous = None; kinds = dict(duplicate=0, rewind=0, gap=0); first = None; torn = unparsable = 0
    offset = start
    for raw in iter(stream.readline, b''):
        lines += 1; line_start = offset; offset += len(raw)
        if not raw.endswith(b'\n'):
            torn = len(raw)
            continue
        match = ORDINAL.match(raw)
        if match:
            ordinal = int(match[2])
        elif raw.strip():
            try:
                ordinal = json.loads(raw).get('ordinal')
            except (ValueError, UnicodeError, AttributeError):
                unparsable += 1
                continue
        else:
            continue
        if isinstance(ordinal, int):
            if previous is not None and ordinal != previous + 1:
                kind = 'duplicate' if ordinal == previous else ('rewind' if ordinal < previous else 'gap')
                kinds[kind] += 1
                if first is None:
                    first = dict(kind=kind, byte_offset=line_start, previous_ordinal=previous, ordinal=ordinal)
            previous = ordinal
    return dict(lines=lines, bytes=offset, anomalies=kinds, first_anomaly=first, torn_tail_bytes=torn,
                unparsable_lines=unparsable)


def scan_store(home):
    """Read-only sweep for tasks that will fail to project, without reading conversation text.

    Plain rollouts are scanned from their projection checkpoint (or from the start when the
    checkpoint is missing). A checkpoint means the prefix was already accepted by the projector.
    """
    home = canonical_path(home)
    with closing(_ro(home/'state_5.sqlite')) as db:
        records = list(db.execute('SELECT id, rollout_path, history_mode FROM threads'))
    index = home/'thread_history_1.sqlite'
    checkpoints, derived = {}, set()
    if index.exists():
        with closing(_ro(index)) as db:
            checkpoints = {r[0]: (r[1], r[2]) for r in db.execute(
                'SELECT thread_id,next_rollout_byte_offset,next_rollout_ordinal FROM thread_history_projection_state')}
            derived = {r[0] for r in db.execute('SELECT DISTINCT thread_id FROM thread_turns')}
    summary = dict(tasks=len(records), legacy=0, missing_rollout=0, compressed_skipped=0, current=0, scanned=0)
    findings = []
    for tid, stored, mode in records:
        if mode != 'paginated':
            summary['legacy'] += 1
            continue
        path = canonical_path(stored)
        match = ROLLOUT_ID.search(path.name)
        rollout_id = str(UUID(match[1])) if match else tid
        checkpoint = checkpoints.get(rollout_id)
        if not path.is_file():
            if path.with_name(path.name + '.zst').is_file():
                summary['compressed_skipped'] += 1
            else:
                summary['missing_rollout'] += 1
                if checkpoint or rollout_id in derived:
                    findings.append(dict(thread=tid, issue='missing_rollout_with_index_rows'))
            continue
        size = path.stat().st_size
        with path.open('rb') as stream:
            if stream.read(4) == ZSTD_MAGIC:
                summary['compressed_skipped'] += 1
                continue
            if checkpoint and checkpoint[0] == size:
                summary['current'] += 1
                continue
            if checkpoint and checkpoint[0] > size:
                findings.append(dict(thread=tid, issue='checkpoint_beyond_rollout', rollout_bytes=size,
                                     checkpoint_bytes=checkpoint[0],
                                     hint='crash-shortened rollout; see docs/operations/ssh-crash-history-recovery.md'))
                continue
            start = checkpoint[0] if checkpoint else 0
            stream.seek(start)
            result = _scan_ordinals(stream, start)
        summary['scanned'] += 1
        issue = None
        if any(result['anomalies'].values()):
            issue = 'ordinal_anomaly'
        elif result['torn_tail_bytes'] or result['unparsable_lines']:
            issue = 'damaged_lines'
        orphan = checkpoint is None and rollout_id in derived
        if issue or orphan:
            findings.append(dict(thread=tid, issue=issue or 'rows_without_checkpoint', rows_without_checkpoint=orphan,
                                 scanned_from_byte=start, **result))
    summary['findings'] = len(findings)
    return dict(home=str(home), summary=summary, findings=findings,
                note='rows_without_checkpoint alone is benign: the runtime re-projects from byte 0 on open.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--home', default=os.environ.get('CODEX_HOME') or str(Path.home()/'.codex'),
                        help='Record store holding state_5.sqlite, thread_history_1.sqlite and sessions/.')
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--thread', help='Task (thread) UUID to inspect or repair.')
    target.add_argument('--scan', action='store_true', help='Read-only sweep of the whole store.')
    parser.add_argument('--apply', action='store_true', help='Rewrite the rollout. Without it nothing is changed.')
    parser.add_argument('--allow-live', action='store_true',
                        help='Repair even though Codex runtimes hold the store databases (the task itself must be closed).')
    parser.add_argument('--rebuild-index', action='store_true',
                        help='Also clear rows derived from the unchanged prefix (full DB backup first).')
    parser.add_argument('--backup-dir', help='Parent folder for timestamped backups (default: work/history-recovery).')
    args = parser.parse_args(argv)
    try:
        if args.scan:
            result = scan_store(args.home)
        else:
            result = repair(args.home, args.thread, apply=args.apply, allow_live=args.allow_live,
                            rebuild_index=args.rebuild_index, backup_root=args.backup_dir)
        blocked = not args.scan and args.apply and not result.get('applied') and result.get('state') != 'clean'
        code = 3 if blocked else 0
    except Refused as error:
        result = dict(state='refused', reason=str(error), applied=False); code = 2
    sys.stdout.write(json.dumps(result, indent=2, ensure_ascii=False) + '\n')
    return code


if __name__ == '__main__':
    sys.exit(main())
