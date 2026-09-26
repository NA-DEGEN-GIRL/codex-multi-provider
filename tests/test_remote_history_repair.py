from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from remote_helpers import history_repair as repair


class RemoteHistoryRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.user = Path(self.tmp.name)
        self.home = self.user/'.local/share/codex-control-center/profiles'/str(uuid4())/'codex'
        self.home.mkdir(parents=True)
        self.tid, self.other = str(uuid4()), str(uuid4())
        self.rollout = self.home/'sessions/task.jsonl'
        self.rollout.parent.mkdir()
        self.prefix = self.line(0, 'session_meta', {'id':self.tid}) + self.line(1, 'event_msg', {'type':'user_message','message':'fixture'})
        self.original = self.prefix + b'{"ordinal":2,"payload":'
        self.rollout.write_bytes(self.original)
        with closing(sqlite3.connect(self.home/'state_5.sqlite')) as db, db:
            db.execute('CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT)')
            db.execute('INSERT INTO threads VALUES(?,?)', (self.tid,str(self.rollout)))
        with closing(sqlite3.connect(self.home/'thread_history_1.sqlite')) as db, db:
            db.execute('CREATE TABLE thread_history_projection_state(thread_id TEXT PRIMARY KEY,next_rollout_byte_offset INTEGER,next_rollout_ordinal INTEGER)')
            db.execute('INSERT INTO thread_history_projection_state VALUES(?,?,?)',(self.tid,len(self.original)+100,4))
            db.execute('INSERT INTO thread_history_projection_state VALUES(?,?,?)',(self.other,50,2))
            for table in repair.TABLES[:-1]:
                db.execute(f'CREATE TABLE {table}(thread_id TEXT,payload TEXT)')
                db.executemany(f'INSERT INTO {table} VALUES(?,?)',[(self.tid,'recoverable cached tail'),(self.other,'unrelated')])
        patcher=patch.object(Path,'home',return_value=self.user);patcher.start();self.addCleanup(patcher.stop)
        # Unrelated protected host processes must not affect fixture DB tests.
        patcher=patch.object(repair,'reject_open_rollout');self.proc_probe=patcher.start();self.addCleanup(patcher.stop)

    def line(self, ordinal, kind, payload):
        return (json.dumps(dict(ordinal=ordinal,type=kind,payload=payload))+'\n').encode()

    def test_inspect_changes_no_record_or_database(self):
        before=(self.home/'thread_history_1.sqlite').read_bytes()
        result=repair.repair(self.home,self.tid)
        self.assertEqual(result['state'],'repair_available')
        self.assertEqual(result['complete_lines'],2)
        self.assertEqual(result['incomplete_tail_bytes'],len(self.original)-len(self.prefix))
        self.assertEqual(self.rollout.read_bytes(),self.original)
        self.assertEqual((self.home/'thread_history_1.sqlite').read_bytes(),before)

    def test_reject_complete_line_damage_and_ordinal_gap(self):
        for raw in [self.prefix+b'broken\n',self.prefix+self.line(4,'event_msg',{})]:
            self.rollout.write_bytes(raw)
            with self.assertRaises(ValueError):repair.repair(self.home,self.tid)
            self.assertEqual(self.rollout.read_bytes(),raw)

    def test_never_drop_valid_json_without_newline(self):
        raw=self.prefix+self.line(2,'event_msg',{})[:-1];self.rollout.write_bytes(raw)
        with self.assertRaisesRegex(ValueError,'valid final JSON'):repair.repair(self.home,self.tid)
        self.assertEqual(self.rollout.read_bytes(),raw)

    def test_reject_identity_mismatch_and_frozen_descendants(self):
        with self.assertRaises(ValueError):repair.scan_rollout(self.rollout,self.other)
        child=self.home/'sessions/fork.jsonl'
        child.write_bytes(self.line(0,'session_meta',{'id':self.other,'history_base':{'thread_id':self.tid}}))
        with closing(sqlite3.connect(self.home/'state_5.sqlite')) as db, db:db.execute('INSERT INTO threads VALUES(?,?)',(self.other,str(child)))
        with self.assertRaisesRegex(ValueError,'frozen-history'):repair.repair(self.home,self.tid)

    def test_only_excess_checkpoint_is_eligible(self):
        with closing(sqlite3.connect(self.home/'thread_history_1.sqlite')) as db, db:
            db.execute('UPDATE thread_history_projection_state SET next_rollout_byte_offset=1 WHERE thread_id=?',(self.tid,))
        with self.assertRaisesRegex(ValueError,'does not apply'):repair.repair(self.home,self.tid)

    @unittest.skipUnless(os.name=='posix','requires Linux flock')
    def test_unverified_processes_prevent_changes(self):
        self.proc_probe.side_effect=ValueError('Cannot verify task file users')
        with self.assertRaisesRegex(ValueError,'Cannot verify'):
            repair.repair(self.home,self.tid,apply=True)
        self.assertEqual(self.rollout.read_bytes(),self.original)

    @unittest.skipUnless(os.name=='posix','requires Linux flock and /proc')
    def test_apply_keeps_complete_bytes_backups_and_other_tasks(self):
        result=repair.repair(self.home,self.tid,apply=True)
        self.assertEqual(self.rollout.read_bytes(),self.prefix)
        self.assertEqual(Path(result['backup']).read_bytes(),self.original)
        self.assertEqual(Path(result['backup_directory']).stat().st_mode&0o777,0o700)
        with closing(sqlite3.connect(self.home/'thread_history_1.sqlite')) as db:
            for table in repair.TABLES:
                self.assertEqual(db.execute(f'SELECT count(*) FROM {table} WHERE thread_id=?',(self.tid,)).fetchone()[0],0)
                self.assertEqual(db.execute(f'SELECT count(*) FROM {table} WHERE thread_id=?',(self.other,)).fetchone()[0],1)
        with closing(sqlite3.connect(Path(result['backup_directory'])/'index-0.sqlite')) as db:
            self.assertEqual(db.execute('SELECT payload FROM thread_items').fetchone()[0],'recoverable cached tail')
        with self.assertRaisesRegex(ValueError,'does not apply'):repair.repair(self.home,self.tid,apply=True)

    @unittest.skipUnless(os.name=='posix','requires Linux flock and /proc')
    def test_active_writer_and_append_locks_prevent_changes(self):
        import fcntl
        for path in [self.home/'thread-writer-locks'/(self.tid+'.lock'), self.rollout.with_suffix('.manager-append.lock')]:
            path.parent.mkdir(parents=True,exist_ok=True)
            with path.open('w') as file:
                fcntl.flock(file,fcntl.LOCK_EX|fcntl.LOCK_NB)
                with self.assertRaisesRegex(ValueError,'active writer'):repair.repair(self.home,self.tid,apply=True)
                self.assertEqual(self.rollout.read_bytes(),self.original)

    @unittest.skipUnless(os.name=='posix','requires Linux flock and /proc')
    def test_failed_database_commit_restores_original(self):
        class FailCommit(sqlite3.Connection):
            def commit(self):raise sqlite3.OperationalError('fixture commit failure')
        original_open=repair.db_open
        def connect(path, *, write=False):
            return sqlite3.connect(path,factory=FailCommit) if write else original_open(path)
        with patch.object(repair,'db_open',side_effect=connect):
            with self.assertRaises(sqlite3.OperationalError):repair.repair(self.home,self.tid,apply=True)
        self.assertEqual(self.rollout.read_bytes(),self.original)
        with closing(sqlite3.connect(self.home/'thread_history_1.sqlite')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM thread_items WHERE thread_id=?',(self.tid,)).fetchone()[0],1)


if __name__=='__main__':unittest.main()
