import json
import sqlite3
from contextlib import closing
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from manager_core.history_recovery import prepare, exclusive_rollout, repair, TABLES


class HistoryRecoveryTests(unittest.TestCase):
    def test_preserves_unknown_payloads_and_all_messages_in_physical_order(self):
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder);tid=str(uuid4());source=folder/'source';target=folder/'target'
            rows=[dict(timestamp='2026-09-17T00:00:00Z',ordinal=o,type='future',payload={'text':f'한글 {n}'})
                  for n,o in enumerate([0,1,1,2,0,1])]
            rows[0].update(type='session_meta',payload={'id':tid,'history_mode':'paginated'})
            original=b''.join(json.dumps(r,ensure_ascii=False).encode()+b'\n' for r in rows)
            source.write_bytes(original)
            report=prepare(source,target,tid)
            repaired=[json.loads(line) for line in target.read_bytes().splitlines()]
            self.assertEqual([r.pop('ordinal') for r in repaired],list(range(6)))
            self.assertEqual(repaired,[{k:v for k,v in r.items() if k!='ordinal'} for r in rows])
            self.assertEqual(source.read_bytes(),original)
            self.assertEqual(report['changed_ordinals'],4)

    def test_unfinished_line_is_not_repaired(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'source';source.write_bytes(b'{"ordinal":2')
            with self.assertRaises(ValueError):prepare(source,Path(folder)/'candidate',str(uuid4()))
            self.assertEqual(source.read_bytes(),b'{"ordinal":2')

    @unittest.skipUnless(sys.platform=='win32','Windows share-denial test')
    def test_open_file_is_rejected_before_any_edit(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'rollout';path.write_bytes(b'keep')
            with path.open('rb'):
                with self.assertRaises(ValueError):
                    with exclusive_rollout(path):self.fail('open rollout must remain untouched')
            self.assertEqual(path.read_bytes(),b'keep')
            with exclusive_rollout(path) as file:self.assertEqual(file.read(),b'keep')

    @unittest.skipUnless(sys.platform=='win32','Windows recovery test')
    def test_repair_backs_up_payloads_and_clears_only_selected_derived_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); home=root/'home'; sessions=home/'sessions';sessions.mkdir(parents=True)
            tid=str(uuid4());other=str(uuid4());rollout=sessions/'record.jsonl'
            rows=[dict(timestamp='2026-09-17T00:00:00Z',ordinal=0,type='session_meta',
                       payload=dict(id=tid,history_mode='paginated')),
                  dict(timestamp='2026-09-17T00:00:01Z',ordinal=0,type='future',payload={'message':'preserve'})]
            original=''.join(json.dumps(row)+'\n' for row in rows).encode();rollout.write_bytes(original)
            with closing(sqlite3.connect(home/'state_5.sqlite')) as db:
                db.execute('CREATE TABLE threads (id TEXT, rollout_path TEXT)')
                # Native Codex saves the verbatim long-path form on Windows.
                db.execute('INSERT INTO threads VALUES (?,?)',(tid,'\\\\?\\'+str(rollout)));db.commit()
            with closing(sqlite3.connect(home/'thread_history_1.sqlite')) as db:
                for table in TABLES:
                    db.execute(f'CREATE TABLE {table} (thread_id TEXT, data TEXT)')
                    db.executemany(f'INSERT INTO {table} VALUES (?,?)',[(tid,'selected'),(other,'unrelated')])
                db.commit()
            report=repair(root,home,tid)
            self.assertEqual(report['state'],'repaired')
            self.assertEqual(Path(report['backup']).read_bytes(),original)
            self.assertEqual([json.loads(line)['ordinal'] for line in rollout.read_bytes().splitlines()],[0,1])
            with closing(sqlite3.connect(home/'thread_history_1.sqlite')) as db, closing(sqlite3.connect(Path(report['backup']).parent/'index-0.sqlite')) as saved:
                for table in TABLES:
                    self.assertEqual(db.execute(f'SELECT * FROM {table}').fetchall(),[(other,'unrelated')])
                    self.assertEqual(saved.execute(f'SELECT * FROM {table}').fetchall(),[(tid,'selected')])
            self.assertEqual(repair(root,home,tid)['state'],'unchanged')

if __name__=='__main__':unittest.main()
