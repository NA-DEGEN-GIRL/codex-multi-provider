"""Synthetic-fixture tests for scripts/repair_history_ordinals.py (no user data)."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import repair_history_ordinals as tool  # noqa: E402

SCHEMA = [
    'CREATE TABLE thread_items (thread_id TEXT NOT NULL, turn_id TEXT NOT NULL, item_id TEXT NOT NULL, '
    'rollout_ordinal INTEGER NOT NULL, created_at_ms INTEGER NOT NULL, item_json TEXT NOT NULL, '
    "item_type TEXT NOT NULL DEFAULT '', updated_at_ordinal INTEGER NOT NULL DEFAULT 0, "
    'PRIMARY KEY (thread_id, turn_id, item_id))',
    'CREATE TABLE thread_turns (thread_id TEXT NOT NULL, turn_id TEXT NOT NULL, rollout_ordinal INTEGER NOT NULL, '
    'status TEXT NOT NULL, rollout_end_ordinal INTEGER, PRIMARY KEY (thread_id, turn_id))',
    'CREATE TABLE thread_realtime_items (thread_id TEXT NOT NULL, item_id TEXT NOT NULL, '
    'rollout_ordinal INTEGER NOT NULL, PRIMARY KEY (thread_id, item_id))',
    'CREATE TABLE thread_history_projection_state (thread_id TEXT PRIMARY KEY, '
    'next_rollout_byte_offset INTEGER NOT NULL, next_rollout_ordinal INTEGER NOT NULL)',
]


def line(ordinal, kind='event_msg', payload=None, seconds=0):
    row = dict(timestamp=f'2026-08-22T04:08:{seconds % 60:02d}.000Z', ordinal=ordinal, type=kind,
               payload=payload if payload is not None else dict(type='token_count', note='합성 데이터'))
    return (json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n').encode()


def meta(tid, ordinal=0, **extra):
    return line(ordinal, 'session_meta', dict(id=tid, history_mode='paginated', **extra))


class Store:
    """A minimal record store: state_5, thread_history_1 and one rollout per task."""

    def __init__(self, folder):
        self.home = Path(folder)/'codex'
        (self.home/'sessions/2026/08/22').mkdir(parents=True)
        with closing(sqlite3.connect(self.home/'state_5.sqlite')) as db:
            db.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, history_mode TEXT)')
            db.commit()
        with closing(sqlite3.connect(self.home/'thread_history_1.sqlite')) as db:
            for statement in SCHEMA:
                db.execute(statement)
            db.commit()

    def task(self, lines, tid=None, mode='paginated', long_path=True):
        tid = tid or str(uuid4())
        path = self.home/'sessions/2026/08/22'/f'rollout-2026-08-22T00-00-00-{tid}.jsonl'
        path.write_bytes(b''.join(lines))
        stored = '\\\\?\\' + str(path) if long_path else str(path)
        with closing(sqlite3.connect(self.home/'state_5.sqlite')) as db:
            db.execute('INSERT INTO threads VALUES (?,?,?)', (tid, stored, mode)); db.commit()
        return tid, path

    def rows(self, tid, ordinals, checkpoint=None):
        with closing(sqlite3.connect(self.home/'thread_history_1.sqlite')) as db:
            for n, ordinal in enumerate(ordinals):
                db.execute('INSERT INTO thread_items VALUES (?,?,?,?,?,?,?,?)',
                           (tid, 'turn', f'item-{n}', ordinal, 0, '{}', 'agentMessage', ordinal))
            if ordinals:
                db.execute('INSERT INTO thread_turns VALUES (?,?,?,?,?)', (tid, 'turn', ordinals[0], 'inProgress', None))
            if checkpoint:
                db.execute('INSERT INTO thread_history_projection_state VALUES (?,?,?)', (tid, *checkpoint))
            db.commit()

    def count(self, tid):
        with closing(sqlite3.connect(self.home/'thread_history_1.sqlite')) as db:
            return {t: db.execute(f'SELECT count(*) FROM {t} WHERE thread_id=?', (tid,)).fetchone()[0]
                    for t in tool.TABLES}


def duplicated(tid, total=14, duplicate_after=9):
    """Ordinals 0..9, 9 again (a second writer), then the writer continues 10, 11, ..."""
    lines = [meta(tid)]; ordinal = 1
    for n in range(1, total):
        lines.append(line(ordinal, payload=dict(type='item', n=n, text='보존할 내용')))
        if n == duplicate_after:
            lines.append(line(ordinal, payload=dict(type='thread_goal_updated', text='두 번째 작성자')))
        ordinal += 1
    return lines


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.store = Store(self.folder.name)

    def tearDown(self):
        self.folder.cleanup()

    def test_clean_rollout_needs_nothing(self):
        tid = str(uuid4()); tid, path = self.store.task([meta(tid), line(1), line(2)], tid)
        report = tool.repair(self.store.home, tid)
        self.assertEqual(report['state'], 'clean')
        self.assertEqual(report['rollout_scan']['changed_lines'], 0)

    def test_dry_run_reports_duplicate_and_changes_nothing(self):
        tid = str(uuid4()); tid, path = self.store.task(duplicated(tid), tid)
        self.store.rows(tid, [3, 5])
        before = path.read_bytes(); db_before = (self.store.home/'thread_history_1.sqlite').read_bytes()
        backups = Path(self.folder.name)/'backups'
        report = tool.repair(self.store.home, tid, backup_root=backups)
        self.assertEqual(report['state'], 'repair_available')
        self.assertFalse(report['applied'])
        self.assertEqual(report['rollout_scan']['anomaly_counts'], dict(duplicate=1, rewind=0))
        anomaly = report['rollout_scan']['anomalies'][0]
        self.assertEqual((anomaly['kind'], anomaly['line'], anomaly['previous_ordinal'], anomaly['ordinal']),
                         ('duplicate', 10, 9, 9))
        self.assertEqual(anomaly['line_kind']['payload_type'], 'thread_goal_updated')
        self.assertEqual(report['rollout_scan']['first_change']['repaired_ordinal'], 10)
        self.assertTrue(report['index']['orphan_rows'])
        self.assertEqual(report['index']['action'], 'none')
        self.assertNotIn('보존할', json.dumps(report, ensure_ascii=False))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual((self.store.home/'thread_history_1.sqlite').read_bytes(), db_before)
        self.assertFalse(backups.exists())

    def test_torn_tail_gap_and_foreign_session_are_refused(self):
        tid = str(uuid4())
        cases = {
            'incomplete': [meta(tid), line(1), line(2)[:-5]],
            'gap': [meta(tid), line(1), line(3)],
            'not valid JSON': [meta(tid), b'{"timestamp":\n', line(1)],
            'session metadata': [meta(str(uuid4())), line(1)],
        }
        for message, lines in cases.items():
            with self.subTest(message):
                task, path = self.store.task(lines, tid if message != 'session metadata' else None)
                if message == 'session metadata':
                    path.write_bytes(b''.join(lines))
                with self.assertRaisesRegex(tool.Refused, message):
                    tool.repair(self.store.home, task, apply=True, backup_root=Path(self.folder.name)/'b')
                with closing(sqlite3.connect(self.store.home/'state_5.sqlite')) as db:
                    db.execute('DELETE FROM threads WHERE id=?', (task,)); db.commit()

    def test_history_base_sets_the_first_ordinal(self):
        tid = str(uuid4())
        lines = [meta(tid, 5, history_base=dict(thread_id=str(uuid4()), end_ordinal_exclusive=5, end_byte_offset=10)),
                 line(6), line(6), line(7)]
        tid, path = self.store.task(lines, tid)
        report = tool.repair(self.store.home, tid, apply=True, backup_root=Path(self.folder.name)/'b')
        self.assertEqual(report['state'], 'repaired')
        self.assertEqual([json.loads(r)['ordinal'] for r in path.read_bytes().splitlines()], [5, 6, 7, 8])


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.store = Store(self.folder.name)
        self.backups = Path(self.folder.name)/'backups'
        patcher = mock.patch.object(tool, 'live_holders', return_value=[])
        patcher.start(); self.addCleanup(patcher.stop)

    def tearDown(self):
        self.folder.cleanup()

    def test_renumbers_suffix_and_keeps_every_other_byte(self):
        tid = str(uuid4()); tid, path = self.store.task(duplicated(tid), tid)
        original = path.read_bytes()
        report = tool.repair(self.store.home, tid, apply=True, backup_root=self.backups)
        self.assertEqual(report['state'], 'repaired')
        repaired = path.read_bytes()
        old_lines, new_lines = original.splitlines(keepends=True), repaired.splitlines(keepends=True)
        self.assertEqual(len(old_lines), len(new_lines))
        self.assertEqual([json.loads(r)['ordinal'] for r in new_lines], list(range(len(new_lines))))
        for old, new in zip(old_lines, new_lines):
            a, b = json.loads(old), json.loads(new)
            a.pop('ordinal'); b.pop('ordinal')
            self.assertEqual(a, b)
            self.assertEqual(old.split(b'"ordinal":')[1].split(b',', 1)[1], new.split(b'"ordinal":')[1].split(b',', 1)[1])
        # The digit count grows from 9 to 10: later byte offsets shift by one, earlier bytes are untouched.
        first = report['rollout_scan']['first_change']['byte_offset']
        self.assertEqual(repaired[:first], original[:first])
        self.assertEqual(len(repaired), len(original) + 1)
        backup = Path(report['backup_directory'])
        self.assertEqual((backup/'original.jsonl').read_bytes(), original)
        self.assertFalse((backup/'repaired.jsonl').exists())
        journal = json.loads((backup/'repair.json').read_text(encoding='utf-8'))
        self.assertEqual(journal['state'], 'repaired')
        self.assertEqual(journal['rollout_scan']['sha256'], hashlib.sha256(original).hexdigest())
        self.assertEqual(report['verification']['anomaly_counts'], dict(duplicate=0, rewind=0))
        self.assertEqual(tool.repair(self.store.home, tid, apply=True, backup_root=self.backups)['state'], 'clean')

    def test_rewind_from_two_interleaved_writers(self):
        tid = str(uuid4())
        lines = [meta(tid), line(1), line(2), line(3), line(2), line(3), line(4), line(5)]
        tid, path = self.store.task(lines, tid)
        report = tool.repair(self.store.home, tid, apply=True, backup_root=self.backups)
        self.assertEqual(report['rollout_scan']['anomaly_counts'], dict(duplicate=0, rewind=1))
        self.assertEqual([json.loads(r)['ordinal'] for r in path.read_bytes().splitlines()], list(range(8)))

    def test_prefix_rows_are_kept_and_suffix_rows_cleared_with_backups(self):
        tid = str(uuid4()); tid, path = self.store.task(duplicated(tid), tid)
        other, _ = self.store.task([meta('x')])  # unrelated task, never touched
        self.store.rows(tid, [2, 4, 12]); self.store.rows(other, [1, 2])
        report = tool.repair(self.store.home, tid, apply=True, backup_root=self.backups)
        self.assertEqual(report['index']['action'], 'clear')
        self.assertEqual(report['cleared_rows']['thread_items'], 3)
        self.assertEqual(self.store.count(tid), dict.fromkeys(tool.TABLES, 0))
        self.assertEqual(self.store.count(other)['thread_items'], 2)
        backup = Path(report['backups']['index']['full'])
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM thread_items WHERE thread_id=?', (tid,)).fetchone()[0], 3)
        with closing(sqlite3.connect(report['backups']['index']['task_rows'])) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM thread_items').fetchone()[0], 3)

    def test_prefix_checkpoint_is_kept(self):
        tid = str(uuid4()); lines = duplicated(tid); tid, path = self.store.task(lines, tid)
        prefix = sum(len(x) for x in lines[:10])
        self.store.rows(tid, [3, 9], checkpoint=(prefix, 10))
        report = tool.repair(self.store.home, tid, apply=True, backup_root=self.backups)
        self.assertEqual(report['index']['action'], 'none')
        self.assertNotIn('index', report['backups'])
        self.assertEqual(self.store.count(tid)['thread_history_projection_state'], 1)
        report = tool.repair(self.store.home, tid, apply=True, backup_root=self.backups, rebuild_index=True)
        self.assertEqual(report['state'], 'clean')  # already repaired; nothing else to do

    def test_rebuild_index_clears_prefix_rows_too(self):
        tid = str(uuid4()); tid, path = self.store.task(duplicated(tid), tid)
        self.store.rows(tid, [3])
        report = tool.repair(self.store.home, tid, apply=True, backup_root=self.backups, rebuild_index=True)
        self.assertEqual(report['index']['action'], 'clear')
        self.assertEqual(self.store.count(tid)['thread_items'], 0)

    def test_refuses_while_runtime_holds_store_unless_allowed(self):
        tid = str(uuid4()); tid, path = self.store.task(duplicated(tid), tid); before = path.read_bytes()
        with mock.patch.object(tool, 'live_holders', return_value=[(4242, 'codex.exe')]):
            with self.assertRaisesRegex(tool.Refused, 'codex.exe'):
                tool.repair(self.store.home, tid, apply=True, backup_root=self.backups)
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(self.backups.exists())
            report = tool.repair(self.store.home, tid, apply=True, allow_live=True, backup_root=self.backups)
        self.assertEqual(report['state'], 'repaired')

    def test_refuses_when_a_runtime_holds_the_task_lock(self):
        tid = str(uuid4()); tid, path = self.store.task(duplicated(tid), tid); before = path.read_bytes()
        profile = self.store.home/'thread-writer-locks'/str(uuid4())
        for held in (profile/f'{tid}.lock', self.store.home/'thread-projection-locks'/f'{tid}.lock',
                     path.with_suffix('.manager-append.lock')):
            with self.subTest(held.parent.name):
                with tool._try_lock(held):
                    with self.assertRaises(tool.Refused):
                        tool.repair(self.store.home, tid, apply=True, backup_root=self.backups)
                self.assertEqual(path.read_bytes(), before)
        self.assertEqual(tool.repair(self.store.home, tid, apply=True, backup_root=self.backups)['state'], 'repaired')

    @unittest.skipUnless(sys.platform == 'win32', 'Windows share-denial test')
    def test_open_rollout_is_never_rewritten_even_with_allow_live(self):
        tid = str(uuid4()); tid, path = self.store.task(duplicated(tid), tid); before = path.read_bytes()
        with path.open('rb'):
            with self.assertRaisesRegex(tool.Refused, 'open in another process'):
                tool.repair(self.store.home, tid, apply=True, allow_live=True, backup_root=self.backups)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.backups.glob('*')), [])

    def test_positional_descendants_block_only_past_the_duplicate(self):
        tid = str(uuid4()); tid, path = self.store.task(duplicated(tid), tid); before = path.read_bytes()
        child = str(uuid4())
        self.store.task([meta(child, forked_from_id=tid, forked_from_ordinal_exclusive=12)], child)
        report = tool.repair(self.store.home, tid, apply=True, backup_root=self.backups)
        self.assertEqual(report['state'], 'blocked_by_references')
        self.assertEqual(report['references']['blocking'][0]['thread'], child)
        self.assertEqual(path.read_bytes(), before)
        with closing(sqlite3.connect(self.store.home/'state_5.sqlite')) as db:
            db.execute('DELETE FROM threads WHERE id=?', (child,)); db.commit()
        early = str(uuid4())
        self.store.task([meta(early, forked_from_id=tid, forked_from_ordinal_exclusive=5)], early)
        base = str(uuid4())
        self.store.task([meta(base, 3, history_base=dict(thread_id=tid, end_ordinal_exclusive=3, end_byte_offset=10))], base)
        self.assertEqual(tool.repair(self.store.home, tid, apply=True, backup_root=self.backups)['state'], 'repaired')

    def test_failed_index_update_restores_the_rollout(self):
        tid = str(uuid4()); tid, path = self.store.task(duplicated(tid), tid); before = path.read_bytes()
        self.store.rows(tid, [12])
        real_connect = sqlite3.connect

        def failing(target, *args, **kwargs):
            if isinstance(target, str) and target.endswith('?mode=rw'):
                raise sqlite3.OperationalError('disk I/O error')
            return real_connect(target, *args, **kwargs)
        with mock.patch.object(tool.sqlite3, 'connect', side_effect=failing):
            with self.assertRaises(sqlite3.OperationalError):
                tool.repair(self.store.home, tid, apply=True, backup_root=self.backups)
        self.assertEqual(path.read_bytes(), before)
        journal = json.loads(next(self.backups.glob('*/repair.json')).read_text(encoding='utf-8'))
        self.assertEqual(journal['state'], 'failed_original_restored')
        self.assertEqual(self.store.count(tid)['thread_items'], 1)


class StoreScanTests(unittest.TestCase):
    def test_scan_reports_duplicates_and_benign_orphans(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            bad = str(uuid4()); store.task(duplicated(bad), bad)
            orphan = str(uuid4()); store.task([meta(orphan), line(1)], orphan); store.rows(orphan, [1])
            clean = str(uuid4()); _, path = store.task([meta(clean), line(1)], clean)
            store.rows(clean, [1], checkpoint=(path.stat().st_size, 2))
            store.task([line(0, 'session_meta', dict(id='legacy'))], mode='legacy')
            report = tool.scan_store(store.home)
            found = {f['thread']: f for f in report['findings']}
            self.assertEqual(found[bad]['issue'], 'ordinal_anomaly')
            self.assertEqual(found[bad]['anomalies']['duplicate'], 1)
            self.assertEqual(found[orphan]['issue'], 'rows_without_checkpoint')
            self.assertNotIn(clean, found)
            self.assertEqual((report['summary']['current'], report['summary']['legacy']), (1, 1))

    def test_cli_is_read_only_without_apply(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder); tid = str(uuid4()); _, path = store.task(duplicated(tid), tid)
            before = path.read_bytes()
            result = subprocess.run([sys.executable, str(Path(tool.__file__)), '--home', str(store.home), '--thread', tid],
                                    capture_output=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['state'], 'repair_available')
            self.assertEqual(path.read_bytes(), before)


@unittest.skipUnless(sys.platform == 'win32', 'Windows open-file query')
class HolderTests(unittest.TestCase):
    def test_names_another_process_holding_a_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'held.sqlite'; path.write_bytes(b'x')
            child = subprocess.Popen([sys.executable, '-c', 'import sys,time; f=open(sys.argv[1],"rb"); '
                                      'print("ready", flush=True); time.sleep(30)', str(path)],
                                     stdout=subprocess.PIPE)
            try:
                self.assertEqual(child.stdout.readline().strip(), b'ready')
                holders = tool.live_holders([path])
                self.assertIn(child.pid, [pid for pid, _ in holders])
                self.assertTrue(dict(holders)[child.pid].lower().startswith('python'))
            finally:
                child.kill(); child.wait(); child.stdout.close()
            self.assertEqual(tool.live_holders([path]), [])


if __name__ == '__main__':
    unittest.main()
