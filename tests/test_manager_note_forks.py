import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core import note_forks


class NoteForkTests(unittest.TestCase):
    def setUp(self):
        note_forks._SEEN.clear()

    def _home(self, root, name, edges):
        home = root / name
        home.mkdir(parents=True)
        with closing(sqlite3.connect(home / 'state_5.sqlite')) as db, db:
            db.execute('CREATE TABLE thread_spawn_edges(parent_thread_id TEXT, child_thread_id TEXT, status TEXT)')
            db.executemany('INSERT INTO thread_spawn_edges VALUES(?,?,?)', edges)
        return home

    def test_spawn_edges_are_published_as_a_bounded_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent, child = '00000000-0000-4000-8000-000000000001', '00000000-0000-4000-8000-000000000002'
            home = self._home(root, 'codex-home', [(parent, child, 'completed'), (child, child, 'completed')])
            document = note_forks.refresh(root, {'sources': [{'host_id': 'local', 'home': str(home)}], 'profiles': []})
            self.assertEqual(document, {child: parent})
            self.assertEqual(json.loads((root / 'work/control-center/note-forks.json').read_text(encoding='utf-8')),
                             {child: parent})

    def test_missing_database_is_ignored_and_no_file_is_written(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = note_forks.refresh(root, {'sources': [], 'profiles': []})
            self.assertIsNone(document)
            self.assertFalse((root / 'work/control-center/note-forks.json').exists())


if __name__ == '__main__':
    unittest.main()
