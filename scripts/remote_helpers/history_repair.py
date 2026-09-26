"""Explicit Linux repair of a crash-shortened managed rollout, with private backups.

Run on the affected SSH host. Inspection is the default; --apply is required.
Never infer missing messages or rewrite ordinals. Loaded writers and forks whose
frozen history would be lost prevent repair. No model requests are made.
"""
from contextlib import ExitStack, closing
from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
from uuid import UUID, uuid4

TABLES = ('thread_items', 'thread_turns', 'thread_realtime_items', 'thread_history_projection_state')


def private_file(path):
    path = Path(path)
    if path.is_symlink() or path.resolve() != path.absolute() or not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError('Expected an ordinary file inside the selected record store.')
    return path


def db_open(path, *, write=False):
    return sqlite3.connect(private_file(path).as_uri() + ('?mode=rw' if write else '?mode=ro'), uri=True, timeout=5)


def scan_rollout(path, thread_id):
    """Accept a complete paginated prefix and at most one incomplete final line."""
    digest = hashlib.sha256(); complete = 0; expected = None; count = 0; tail = 0
    with private_file(path).open('rb') as stream:
        for raw in stream:
            digest.update(raw)
            if tail:
                raise ValueError('Damage is not limited to the final incomplete line.')
            try:
                value = json.loads(raw)
            except (ValueError, UnicodeError):
                if raw.endswith(b'\n') or not count:
                    raise ValueError('Complete rollout line is damaged; manual recovery is required.') from None
                tail = len(raw)
                continue
            if not raw.endswith(b'\n'):
                raise ValueError('A valid final JSON record lacks a newline; do not discard it.')
            ordinal = value.get('ordinal')
            if not count:
                if value.get('type') != 'session_meta' or value.get('payload', {}).get('id') != thread_id:
                    raise ValueError('Rollout identity does not match the selected task.')
                if value['payload'].get('history_base'):
                    raise ValueError('Repair of a task with frozen inherited history requires coordinated recovery.')
                expected = 0
            if type(ordinal) is not int or ordinal != expected:
                raise ValueError('Rollout ordinal sequence is not contiguous; no records were changed.')
            expected += 1; count += 1; complete += len(raw)
    if not count:
        raise ValueError('No complete task history is available.')
    return dict(bytes=complete+tail, complete_bytes=complete, incomplete_tail_bytes=tail,
                complete_lines=count, next_ordinal=expected, sha256=digest.hexdigest())


def inventory(home, thread_id):
    home = Path(home).absolute()
    # Scope this helper to manager-created per-profile homes, never arbitrary DBs.
    UUID(thread_id)
    if home.name != 'codex' or home.parent.parent.name != 'profiles' or home.resolve() != home:
        raise ValueError('A canonical managed profile home is required.')
    UUID(home.parent.name)
    profiles = home.parent.parent
    if profiles.parent.name != 'codex-control-center':
        raise ValueError('Unexpected managed profile root.')
    with closing(db_open(home/'state_5.sqlite')) as db:
        row = db.execute('SELECT rollout_path FROM threads WHERE id=?', (thread_id,)).fetchone()
    if not row:
        raise ValueError('Task was not found in the selected source home.')
    rollout = private_file(row[0])
    if rollout.suffix != '.jsonl' or not any(rollout.is_relative_to(home/s) for s in ('sessions', 'archived_sessions')):
        raise ValueError('Task rollout is outside the selected source store.')
    scanned = scan_rollout(rollout, thread_id)
    homes = sorted({home, *profiles.glob('*/codex')})
    # Also inspect the native CLI store when it exists, without touching its auth.
    native = Path.home()/'.codex'
    if native.is_dir(): homes.append(native)
    indexes = []
    for folder in homes:
        state = folder/'state_5.sqlite'
        if not state.exists(): continue
        with closing(db_open(state)) as db:
            paths = list(db.execute('SELECT id,rollout_path FROM threads'))
        for child_id, path in paths:
            if child_id == thread_id:
                if Path(path).resolve() != rollout:
                    raise ValueError('Duplicate task identity points at a different rollout.')
                continue
            # Missing archived files cannot reference a usable frozen prefix.
            if not Path(path).exists(): continue
            with Path(path).open('rb') as file:
                try: meta = json.loads(file.readline()).get('payload', {})
                except (ValueError, UnicodeError):
                    raise ValueError('Cannot verify a possible fork reference.') from None
            if thread_id in json.dumps(meta.get('history_base')):
                raise ValueError('A frozen-history descendant references this task; coordinated recovery is required.')
            boundary = meta.get('forked_from_ordinal_exclusive')
            if meta.get('forked_from_id') == thread_id and isinstance(boundary, int) and boundary > scanned['next_ordinal']:
                raise ValueError('A fork references history beyond the surviving prefix.')
        index = folder/'thread_history_1.sqlite'
        if not index.exists(): continue
        with closing(db_open(index)) as db:
            row = db.execute('SELECT next_rollout_byte_offset,next_rollout_ordinal FROM thread_history_projection_state WHERE thread_id=?', (thread_id,)).fetchone()
            if not row: continue
            schemas = list(db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"))
            for name, schema in schemas:
                if name not in TABLES and 'thread_id' in (schema or ''):
                    raise ValueError('Unknown thread history table; do not partially reset the index.')
            indexes.append(dict(path=str(index), byte_offset=row[0], ordinal=row[1]))
    if not indexes or not any(i['byte_offset'] > scanned['bytes'] for i in indexes):
        raise ValueError('No projection checkpoint exceeds the durable rollout; this repair does not apply.')
    return dict(home=str(home), thread_id=thread_id, rollout=str(rollout), indexes=indexes, **scanned)


def lock_file(stack, path):
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.resolve() != path.parent or path.is_symlink():
        raise ValueError('Lock path is not private to the record store.')
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    file = stack.enter_context(os.fdopen(fd, 'r+b'))
    if os.fstat(fd).st_nlink != 1:
        raise ValueError('Lock file has multiple links.')
    try: fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise ValueError('This task has an active writer or reader projection; close that task first.') from None


def reject_open_rollout(path, *, privileged_inspection=False):
    if privileged_inspection:
        # Only /proc is inspected as root. Backups, locks and DB writes retain
        # the SSH user's ownership; sudo must already be configured noninteractive.
        probe = '''import os,sys
from pathlib import Path
target=Path(sys.argv[1]); owner=target.stat().st_uid
for proc in Path('/proc').iterdir():
 if not proc.name.isdigit(): continue
 try:
  if proc.stat().st_uid != owner: continue
  for fd in (proc/'fd').iterdir():
   try:
    if fd.resolve() == target: sys.exit(10)
   except FileNotFoundError: continue
 except FileNotFoundError: continue
 except PermissionError: sys.exit(11)
'''
        result = subprocess.run(['/usr/bin/sudo', '-n', '/usr/bin/python3', '-c', probe, str(path)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        if result.returncode == 10:
            raise ValueError('Another process still has this task open; record was preserved.')
        if result.returncode:
            raise ValueError('Privileged process inspection failed; no repair was performed.')
        return
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit() or proc.name == str(os.getpid()): continue
        try:
            if proc.stat().st_uid != os.getuid(): continue
            for fd in (proc/'fd').iterdir():
                try: target = fd.resolve()
                except FileNotFoundError: continue
                if target == path:
                    raise ValueError('Another process still has this task open; record was preserved.')
        except FileNotFoundError: continue
        except PermissionError:
            raise ValueError('Cannot verify task file users; no repair was performed.') from None


def write_journal(path, report):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as file:
        json.dump(report, file, indent=2); file.write('\n'); file.flush(); os.fsync(file.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def repair(home, thread_id, *, apply=False, privileged_inspection=False):
    before = inventory(home, thread_id)
    if not apply: return dict(state='repair_available', **before)
    if os.name != 'posix' or not Path('/proc/self/fd').is_dir():
        raise ValueError('Apply requires Linux file locks and process inspection.')
    home = Path(before['home']); profiles = home.parent.parent; rollout = Path(before['rollout'])
    with ExitStack() as stack:
        directories = {home/'thread-writer-locks'}
        directories.update(home/'thread-writer-locks'/p.name for p in profiles.iterdir() if p.is_dir())
        directories.update(p.parent for p in (home/'thread-writer-locks').glob('*/'+thread_id+'.lock'))
        for directory in sorted(directories):
            lock_file(stack, directory/'.coordination.lock')
            lock_file(stack, directory/(thread_id+'.lock'))
        lock_file(stack, rollout.with_suffix('.manager-append.lock'))
        for folder in sorted({home, *(Path(i['path']).parent for i in before['indexes'])}):
            lock_file(stack, folder/'thread-projection-locks'/(thread_id+'.lock'))
        reject_open_rollout(rollout, privileged_inspection=privileged_inspection)
        if inventory(home, thread_id) != before:
            raise ValueError('Task changed during inspection; retry after it stops.')
        output = profiles.parent/'history-recovery'/('crash-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')+'-'+uuid4().hex[:8])
        output.mkdir(parents=True, mode=0o700)
        os.chmod(output, 0o700)
        backup = output/'original.jsonl'
        with rollout.open('rb') as src, backup.open('xb') as dest:
            shutil.copyfileobj(src, dest); dest.flush(); os.fsync(dest.fileno())
        os.chmod(backup, 0o600)
        if hashlib.sha256(backup.read_bytes()).hexdigest() != before['sha256']:
            raise ValueError('Backup does not match the inspected file.')
        databases = []
        for n, index in enumerate(before['indexes']):
            db = stack.enter_context(closing(db_open(index['path'], write=True)))
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT next_rollout_byte_offset,next_rollout_ordinal FROM thread_history_projection_state WHERE thread_id=?', (thread_id,)).fetchone()
            if row != (index['byte_offset'], index['ordinal']):
                raise ValueError('Index changed before repair.')
            saved_path = output/f'index-{n}.sqlite'
            with closing(sqlite3.connect(saved_path)) as saved:
                for table in TABLES:
                    definition = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                    if not definition: continue
                    saved.execute(definition[0])
                    rows = db.execute(f'SELECT * FROM {table} WHERE thread_id=?', (thread_id,))
                    saved.executemany(f'INSERT INTO {table} VALUES ({",".join("?" for _ in rows.description)})', rows)
                saved.commit()
            os.chmod(saved_path, 0o600)
            with saved_path.open('rb') as saved: os.fsync(saved.fileno())
            databases.append(db)
        report = dict(before, state='prepared', backup=str(backup), backup_directory=str(output))
        write_journal(output/'repair.json', report)
        # No body is synthesized. Preserve the partial suffix in the full backup,
        # retain every complete line verbatim and invalidate only derived rows.
        stream = stack.enter_context(rollout.open('r+b'))
        if scan_rollout(rollout, thread_id)['sha256'] != before['sha256']:
            raise ValueError('Rollout changed before applying repair.')
        try:
            for db in databases:
                for table in TABLES:
                    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                        db.execute(f'DELETE FROM {table} WHERE thread_id=?', (thread_id,))
            stream.truncate(before['complete_bytes']); stream.flush(); os.fsync(stream.fileno())
            for db in databases: db.commit()
        except BaseException:
            for db in databases: db.rollback()
            stream.seek(0)
            with backup.open('rb') as original: shutil.copyfileobj(original, stream)
            stream.truncate(); stream.flush(); os.fsync(stream.fileno())
            write_journal(output/'repair.json', dict(report, state='failed_original_restored'))
            raise
        report.update(state='repaired', indexes_rebuild_on_open=len(databases))
        write_journal(output/'repair.json', report)
        return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', required=True)
    parser.add_argument('--thread', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--inspect-proc-with-sudo', action='store_true',
                        help='Use noninteractive sudo only for read-only process inspection.')
    args = parser.parse_args()
    print(json.dumps(repair(args.home, str(UUID(args.thread)), apply=args.apply,
                           privileged_inspection=args.inspect_proc_with_sudo), ensure_ascii=False))
