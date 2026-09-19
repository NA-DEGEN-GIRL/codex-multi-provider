import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core import note_forks


class NoteForkTests(unittest.TestCase):
    parent = '00000000-0000-4000-8000-000000000001'
    child = '00000000-0000-4000-8000-000000000002'
    grandchild = '00000000-0000-4000-8000-000000000003'

    def setUp(self):
        note_forks._SEEN.clear()
        note_forks._META.clear()

    def _home(self, root, name, edges=()):
        home = root / name
        home.mkdir(parents=True)
        with closing(sqlite3.connect(home / 'state_5.sqlite')) as db, db:
            db.execute('CREATE TABLE thread_spawn_edges(parent_thread_id TEXT, child_thread_id TEXT, status TEXT)')
            db.executemany('INSERT INTO thread_spawn_edges VALUES(?,?,?)', edges)
            db.execute('CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT, source TEXT)')
        return home

    def _thread(self, home, child, parent, *, source='vscode', database=None):
        rollout = home / 'sessions' / f'rollout-{child}.jsonl'
        rollout.parent.mkdir(exist_ok=True)
        rollout.write_text(json.dumps({'type': 'session_meta', 'payload': {
            'id': child, 'forked_from_id': parent, 'source': source}}) + '\n' +
            'conversation contents are deliberately not valid JSON\n', encoding='utf-8')
        row = (child, str(rollout), json.dumps(source) if isinstance(source, dict) else source)
        if database is not None:
            database.execute('INSERT INTO threads VALUES(?,?,?)', row)
        else:
            with closing(sqlite3.connect(home / 'state_5.sqlite')) as db, db:
                db.execute('INSERT INTO threads VALUES(?,?,?)', row)
        return rollout

    def _state(self, home, *, managed=False):
        return {'sources': [] if managed else [{'host_id': 'local', 'home': str(home)}],
                'profiles': [{'home': str(home)}] if managed else []}

    def test_native_and_managed_forks_use_metadata_without_spawn_edges(self):
        for managed in (False, True):
            with self.subTest(managed=managed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                home = self._home(root, 'codex-home')
                self._thread(home, self.child, self.parent)
                self._thread(home, self.grandchild, self.child)
                state = self._state(home, managed=managed)
                expected = {self.child: self.parent, self.grandchild: self.child}
                self.assertEqual(note_forks.refresh(root, state), expected)
                target = root / 'work/control-center/note-forks.json'
                stamp = target.stat().st_mtime_ns
                self.assertEqual(json.loads(target.read_text(encoding='utf-8')), expected)
                self.assertEqual(note_forks.refresh(root, state), expected)
                self.assertEqual(target.stat().st_mtime_ns, stamp)

    def test_subagents_and_spawn_edges_are_not_conversation_forks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = self._home(root, 'codex-home', [(self.parent, self.child, 'completed')])
            self._thread(home, self.child, self.parent, source={'subagent': {'thread_spawn': {}}})
            self.assertIsNone(note_forks.refresh(root, self._state(home)))

    def test_new_forks_in_live_wal_are_visible_and_invalidate_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = self._home(root, 'codex-home')
            state = self._state(home)
            with closing(sqlite3.connect(home / 'state_5.sqlite')) as db:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('PRAGMA wal_autocheckpoint=0')
                self.assertIsNone(note_forks.refresh(root, state))
                original = note_forks._stamp(home / 'state_5.sqlite')
                self._thread(home, self.child, self.parent, database=db)
                db.commit()
                self.assertEqual(note_forks._stamp(home / 'state_5.sqlite'), original)
                self.assertEqual(note_forks.refresh(root, state), {self.child: self.parent})
                self._thread(home, self.grandchild, self.child, database=db)
                db.commit()
                self.assertEqual(note_forks.refresh(root, state),
                                 {self.child: self.parent, self.grandchild: self.child})

    def test_targeted_refresh_only_reads_requested_ancestry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = self._home(root, 'codex-home')
            self._thread(home, self.child, self.parent)
            self._thread(home, self.grandchild, self.child)
            with patch.object(note_forks, '_metadata_parent', wraps=note_forks._metadata_parent) as read:
                self.assertEqual(note_forks.refresh(root, self._state(home), self.child),
                                 {self.child: self.parent})
            self.assertEqual([call.args[0] for call in read.call_args_list], [self.child])

    def test_incomplete_metadata_is_retried_without_database_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = self._home(root, 'codex-home')
            rollout = self._thread(home, self.child, self.parent)
            complete = rollout.read_bytes()
            rollout.write_bytes(b'{"type":"session_meta"')
            self.assertIsNone(note_forks.refresh(root, self._state(home)))
            with self.assertRaisesRegex(RuntimeError, 'not ready'):
                note_forks.refresh(root, self._state(home), self.child)
            self.assertFalse((root / 'work/control-center/note-forks.json').exists())
            rollout.write_bytes(complete)
            self.assertEqual(note_forks.refresh(root, self._state(home)), {self.child: self.parent})

    def test_unchanged_database_does_not_rescan_completed_rollouts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = self._home(root, 'codex-home')
            self._thread(home, self.child, self.parent)
            state = self._state(home)
            expected = note_forks.refresh(root, state)
            with patch.object(note_forks, '_metadata_parent', side_effect=AssertionError('rescanned')):
                self.assertEqual(note_forks.refresh(root, state), expected)

    def test_unindexed_target_retries_without_publishing_empty_ancestry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = self._home(root, 'codex-home')
            with self.assertRaisesRegex(RuntimeError, 'not indexed'):
                note_forks.refresh(root, self._state(home), self.child)
            self.assertFalse((root / 'work/control-center/note-forks.json').exists())

    def test_mismatched_or_invalid_metadata_cannot_assign_a_parent(self):
        for changes in ({'id': self.parent}, {'forked_from_id': '../bad'}, {'forked_from_id': self.child}):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                home = self._home(root, 'codex-home')
                rollout = self._thread(home, self.child, self.parent)
                row = json.loads(rollout.read_text(encoding='utf-8').splitlines()[0])
                row['payload'].update(changes)
                rollout.write_text(json.dumps(row)+'\n', encoding='utf-8')
                self.assertIsNone(note_forks.refresh(root, self._state(home)))

    def test_full_refresh_removes_obsolete_spawn_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = self._home(root, 'codex-home', [(self.parent, self.child, 'completed')])
            target = root / 'work/control-center/note-forks.json'
            target.parent.mkdir(parents=True)
            target.write_text(json.dumps({self.child: self.parent}), encoding='utf-8')
            self.assertEqual(note_forks.refresh(root, self._state(home)), {})

    def test_missing_database_is_ignored_and_no_file_is_written(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = note_forks.refresh(root, {'sources': [], 'profiles': []})
            self.assertIsNone(document)
            self.assertFalse((root / 'work/control-center/note-forks.json').exists())


if __name__ == '__main__':
    unittest.main()
