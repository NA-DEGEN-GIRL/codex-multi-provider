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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core import canonical_storage as storage
from manager_core.catalog import list_catalog
from manager_core.store import Store


class CanonicalStorageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / 'original'
        self.source = self.root / 'account'
        for home in (self.home, self.source):
            home.mkdir()
            (home / 'auth.json').write_text(home.name + '-auth-sentinel')
            (home / 'config.toml').write_text(home.name + '-config-sentinel')
            with closing(sqlite3.connect(home / 'state_5.sqlite')) as db, db:
                db.executescript('''
                CREATE TABLE threads(id TEXT PRIMARY KEY,rollout_path TEXT,title TEXT,project_id TEXT,
                    thread_section_id TEXT,archived INTEGER, section_position INTEGER);
                CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT,metadata TEXT,position INTEGER);
                CREATE TABLE project_roots(project_id TEXT,position INTEGER,path TEXT,PRIMARY KEY(project_id,position));
                CREATE TABLE thread_sections(id TEXT PRIMARY KEY,name TEXT,appearance TEXT);
                CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT PRIMARY KEY,status TEXT);
                ''')
        self.store = Store(self.root)
        self.store.mutate(lambda d: d.update(sources=[
            dict(id='original:local', home=str(self.home), host_id='local', alias='original'),
            dict(id='account', home=str(self.source), host_id='local', alias='account'),
            dict(id='ssh:a', home='/remote/home', host_id='ssh:host', alias='remote')]))
        guard = patch.object(storage, 'require_closed')
        guard.start(); self.addCleanup(guard.stop)

    def record(self, home, tid=None, text='payload'):
        tid = tid or str(uuid4())
        path = home / 'sessions' / (tid + '.jsonl')
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(dict(type='session_meta', payload=dict(id=tid))) + '\n' +
                        json.dumps(dict(type='unknown_future_event',payload=text)) + '\n')
        with closing(sqlite3.connect(home / 'state_5.sqlite')) as db, db:
            db.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?)', (tid,str(path),'name',None,None,0,1))
        return tid, path

    def test_new_missing_and_hardlinked_records_share_one_store_without_credentials(self):
        common, original = self.record(self.home)
        linked = self.source / 'sessions' / original.name
        linked.parent.mkdir()
        os.link(original, linked)
        with closing(sqlite3.connect(self.source / 'state_5.sqlite')) as db, db:
            db.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?)', (common,str(linked),'old-name',None,None,0,0))
        new, new_path = self.record(self.source)
        restored, restore_path = self.record(self.source)
        with closing(sqlite3.connect(self.home / 'state_5.sqlite')) as db, db:
            db.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?)', (restored,str(self.home/'sessions/missing.jsonl'),'keep-title',None,None,0,1))
            db.execute("INSERT INTO projects VALUES('original-project','project','{}',0)")
            db.execute("INSERT INTO project_roots VALUES('original-project',0,?)", (str(self.root/'workspace'),))
        with closing(sqlite3.connect(self.source / 'state_5.sqlite')) as db, db:
            db.execute("INSERT INTO projects VALUES('profile-project','same project','{}',0)")
            db.execute("INSERT INTO project_roots VALUES('profile-project',0,?)", (str(self.root/'workspace'),))
            db.execute("INSERT INTO thread_sections VALUES('section','My tasks',NULL)")
            db.execute("UPDATE threads SET project_id='profile-project',thread_section_id='section' WHERE id=?", (new,))
            db.execute('INSERT INTO thread_spawn_edges VALUES(?,?,?)', (common,new,'completed'))
        result = storage.migrate(self.root, self.home)
        self.assertEqual((result['copied_files'],result['same_files']), (2,1))
        with closing(sqlite3.connect(self.home/'state_5.sqlite')) as db, db:
            rows = {r[0]: r[1:] for r in db.execute('SELECT id,rollout_path,title,project_id,thread_section_id FROM threads')}
            self.assertEqual(rows[new][2:], ('original-project','section'))
            self.assertEqual(rows[restored][1], 'keep-title')
            self.assertEqual(db.execute('SELECT * FROM thread_spawn_edges').fetchall(), [(common,new,'completed')])
        self.assertEqual(Path(rows[new][0]).read_bytes(), new_path.read_bytes())
        self.assertEqual(Path(rows[restored][0]).read_bytes(), restore_path.read_bytes())
        self.assertEqual(storage.migrate(self.root,self.home), result)
        self.assertEqual(len(list_catalog(self.store.read()['sources'])['conversations']), 3)
        self.assertEqual(self.store.read()['sources'][2]['home'], '/remote/home')
        for home in (self.home,self.source):
            self.assertEqual((home/'auth.json').read_text(), home.name+'-auth-sentinel')
            self.assertEqual((home/'config.toml').read_text(), home.name+'-config-sentinel')

    def test_divergent_same_id_preserves_both_records_and_original_index(self):
        tid, original = self.record(self.home,text='original conversation')
        _, other = self.record(self.source,tid,text='different conversation')
        before = original.read_bytes(), other.read_bytes()
        with self.assertRaisesRegex(ValueError,'기록이 달라'):
            storage.migrate(self.root,self.home)
        self.assertEqual((original.read_bytes(),other.read_bytes()),before)
        self.assertFalse(storage.ready(self.root,self.home))

    @unittest.skipUnless(os.name == 'nt', 'Windows recorder handle sharing')
    def test_open_recorder_defers_without_mutating_original(self):
        tid, source = self.record(self.source)
        with source.open('ab'):
            with self.assertRaisesRegex(ValueError,'열려 있습니다'):
                storage.migrate(self.root,self.home)
        with closing(sqlite3.connect(self.home/'state_5.sqlite')) as db, db:
            self.assertEqual(db.execute('SELECT id FROM threads').fetchall(), [])
        self.assertFalse(storage.ready(self.root,self.home))

    def test_running_desktop_defers_before_backup_or_import(self):
        self.record(self.source)
        with patch.object(storage,'require_closed',side_effect=ValueError('desktop active')):
            with self.assertRaisesRegex(ValueError,'desktop active'):
                storage.migrate(self.root,self.home)
        self.assertFalse((self.root/'work/history-consolidation').exists())


if __name__ == '__main__': unittest.main()
